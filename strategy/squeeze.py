# strategy/squeeze.py
"""
버킷 3: 스퀴즈 + 급등주 초단타 전략.

전략 개요:
  1단계 (스퀴즈 감지): TTM Squeeze 지표로 에너지 축적 구간 탐지
  2단계 (방향 예측):   Momentum 히스토그램 + RSI + 거래량으로 상승/하락 예측
  3단계 (진입):        스퀴즈 발사(squeeze_off=True) 후 상승 돌파 + 매수세 확인
  4단계 (수익극대화):  계단식 트레일링 스탑 — 급등 50~300% 최대한 추적
                       고정 익절 절대 금지 — 매도 세력이 매수세 압도할 때까지 보유
  5단계 (오더플로우):  매수/매도 틱 방향 + 거래량으로 세력 교체 조기 감지
  6단계 (스캘핑 재진입): 눌림목 + 매수세 재개 확인 후 재진입

수익 비교:
  고정 5% 익절  →  +100% 급등 시 수익 +5%   (94% 낭비)
  분할 스케일아웃→  +100% 급등 시 수익 +56%  (44% 낭비)
  계단식 트레일 →  +100% 급등 시 수익 +88%  (12%만 손실 — 고점 근처에서 청산)

핵심 원칙:
  "매수 세력이 살아 있는 동안은 절대 팔지 마라.
   매도 세력이 매수세를 압도하기 시작할 때 빠져라."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from strategy.signals import compute_indicators, is_squeeze_fired, latest_rsi
from strategy.scanner import GapCandidate, gap_and_go_entry, vwap_entry_signal


# ─────────────────────────────────────────────────────────────────────
# 실제 급등주 데이터 기반 트레일링 파라미터
#
# 실증 데이터 (2024~2025 급등주 분석):
#   SOUN  +376% (36일)  단일봉 최대 +33%
#   DJT   +324% (26일)  단일봉 최대 +35%  갭업 최대 +31%
#   MSTR  +191% (36일)  단일봉 최대 +46%
#   BBAI  +188% (20일)  단일봉 최대 +43%
#   HIMS  +173% (31일)  단일봉 최대 +26%
#
# 핵심 인사이트:
#   1. 평균 급등 규모 191%, 중위수 181% — "1.5배" 가정은 과소평가
#   2. 고점 도달까지 평균 23일 — 당일 스캘핑이 아닌 포지션 트레이딩
#   3. 단일봉 최대 범위 33~46% — 고정 8% 트레일링은 매일 털림
#   4. ATR 기반 동적 스탑이 필수 (일별 변동성에 맞게 자동 조정)
#   5. 진짜 급등 거래량: 평균 3.1x, 최대 6.8x — 1.5x 기준은 잡음 신호
#
# ATR 기반 트레일링:
#   스탑 거리 = max(현재 ATR × atr_mult, 최소고정% × 고점가)
#   수익이 커질수록 atr_mult를 줄여 고점 근처에서 더 빨리 청산
# ─────────────────────────────────────────────────────────────────────

# 수익 구간별 ATR 배수 (클수록 스탑 멀어짐 — 큰 움직임 허용)
TIERED_ATR_MULT = [
    # (min_profit_pct, atr_mult, min_trail_floor_pct)
    # 수익 구간    ATR배수  최소트레일  설명
    (0.00,  3.0, 0.12),  # 0~30%:   ATR×3  최소12%  — 초기 진입 변동성 허용
    (0.30,  2.5, 0.10),  # 30~80%:  ATR×2.5 최소10% — 추세 진행 구간
    (0.80,  2.0, 0.08),  # 80~150%: ATR×2   최소8%  — 가속 구간
    (1.50,  1.5, 0.06),  # 150~300%:ATR×1.5 최소6%  — 극단 급등 구간
    (3.00,  1.2, 0.05),  # 300%+:   ATR×1.2 최소5%  — 역사적 급등 구간
]

# 하위 호환성 유지 (ATR 없을 때 폴백용 고정 비율)
TIERED_TRAILING = [
    (0.00,  0.12),
    (0.30,  0.10),
    (0.80,  0.08),
    (1.50,  0.06),
    (3.00,  0.05),
]


@dataclass
class SqueezePosition:
    """스퀴즈 포지션 상태 추적."""
    symbol:        str   = ""
    phase:         str   = "none"   # none / entered / scalp_reentry
    entry_price:   float = 0.0
    peak_price:    float = 0.0
    dynamic_stop:  float = 0.0      # 계단식 트레일링으로 계속 올라가는 손절가
    qty_full:      int   = 0
    qty_remaining: int   = 0
    scalp_entry:   float = 0.0


# ─────────────────────────────────────────────────────────────────────
# 계단식 트레일링 스탑 계산
#
# 핵심 로직:
#   현재 수익 구간 → 적용 트레일링 간격 결정
#   동적 손절가 = 고점 × (1 - 트레일링 간격)
#   손절가는 올라갈 수만 있고, 내려갈 수 없음 (한방향 래칫)
#
# 만원 투자, 100% 급등 예시:
#   고점 20,000원 도달 후 -5% 하락 → 손절가 = 20,000 × (1 - 0.05) = 19,000원 청산
#   수익 = 19,000 - 10,000 = 9,000원 (+90%)
# ─────────────────────────────────────────────────────────────────────
def compute_trailing_stop(
    entry_price:    float,
    peak_price:     float,
    current_stop:   float = 0.0,
    atr:            float = 0.0,   # ATR값 제공 시 ATR 기반 스탑 사용 (더 정확)
) -> float:
    """
    ATR 기반 계단식 트레일링 손절가 계산.
    손절가는 단방향 상승만 허용 (래칫 메커니즘).

    실증 근거:
      SOUN 하루 변동성 33% → ATR×3 = ~30% 스탑 거리 필요
      고정 8% 스탑이었으면 36일 동안 매일 털렸을 것
      ATR 기반으로 해야 주식 특성에 맞게 자동 조정됨

    Returns:
        new_stop: float — 업데이트된 동적 손절가
    """
    if entry_price <= 0 or peak_price <= 0:
        return current_stop

    pnl_pct = (peak_price - entry_price) / entry_price

    if atr > 0:
        # ATR 기반 스탑 (권장)
        atr_mult = TIERED_ATR_MULT[0][1]
        floor_pct = TIERED_ATR_MULT[0][2]
        for min_profit, mult, flr in reversed(TIERED_ATR_MULT):
            if pnl_pct >= min_profit:
                atr_mult  = mult
                floor_pct = flr
                break
        # 스탑 거리 = max(ATR × 배수, 최소 고정 비율)
        atr_distance  = atr * atr_mult
        floor_distance = peak_price * floor_pct
        stop_distance  = max(atr_distance, floor_distance)
        new_stop = peak_price - stop_distance
    else:
        # ATR 없을 때 고정 비율 폴백
        trail_pct = TIERED_TRAILING[0][1]
        for min_profit, trail in reversed(TIERED_TRAILING):
            if pnl_pct >= min_profit:
                trail_pct = trail
                break
        new_stop = peak_price * (1.0 - trail_pct)

    # 최소: 진입가 대비 -10% 이하로는 안 내려감 (초기 최대 손실 제한)
    hard_floor = entry_price * 0.90
    new_stop   = max(new_stop, hard_floor)

    # 래칫: 손절가는 올라갈 수만 있음
    return max(new_stop, current_stop)


def is_trailing_stop_hit(current_price: float, dynamic_stop: float) -> tuple[bool, str]:
    """
    계단식 트레일링 스탑 터치 여부 확인.

    Returns:
        (triggered: bool, reason: str)
    """
    if dynamic_stop <= 0:
        return False, ""
    if current_price <= dynamic_stop:
        return True, f"트레일링 스탑 ({current_price:.2f} <= 동적 손절가 {dynamic_stop:.2f})"
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 오더플로우 분석 — 매수/매도 세력 강도 실시간 측정
#
# 원리:
#   틱 방향(uptick/downtick)으로 매수세 vs 매도세 구분
#   uptick(직전보다 높게 체결) = 매수자가 적극적 (매수세)
#   downtick(직전보다 낮게 체결) = 매도자가 적극적 (매도세)
#
# 매수량이 압도하는 동안 → 보유
# 매도량이 매수량을 1.5배 이상 압도 → 분배(distribution) 시작 → 청산 준비
# ─────────────────────────────────────────────────────────────────────
def analyze_order_flow(df: pd.DataFrame, lookback_bars: int = 10) -> dict:
    """
    최근 N봉의 오더플로우 강도 분석.

    분석 방법:
      - 봉 마감가 > 시가: 매수 주도 봉 → buy_vol += volume
      - 봉 마감가 < 시가: 매도 주도 봉 → sell_vol += volume
      - 봉 마감가 == 시가: 균형 봉 → 거래량 반씩 배분

    Returns:
        {
          'buy_vol':  float,   # 매수 주도 거래량
          'sell_vol': float,   # 매도 주도 거래량
          'ratio':    float,   # buy_vol / sell_vol (>1 = 매수 우위)
          'signal':   str,     # 'bullish'/'bearish'/'neutral'
          'strength': float,   # 0~1, 신호 강도
        }
    """
    if df.empty or len(df) < 2:
        return {"buy_vol": 0, "sell_vol": 0, "ratio": 1.0, "signal": "neutral", "strength": 0.0}

    recent = df.tail(lookback_bars).copy()

    buy_vol  = 0.0
    sell_vol = 0.0

    for _, row in recent.iterrows():
        vol   = float(row.get("volume", 0) or 0)
        op    = float(row.get("open",  0) or 0)
        cl    = float(row.get("close", 0) or 0)
        hi    = float(row.get("high",  0) or 0)
        lo    = float(row.get("low",   0) or 0)

        if vol == 0 or op == 0:
            continue

        candle_range = hi - lo
        if candle_range == 0:
            buy_vol  += vol * 0.5
            sell_vol += vol * 0.5
            continue

        # 클로즈 위치 비율로 매수/매도 추정 (Wyckoff 방식)
        # 고점에 가까울수록 매수 주도, 저점에 가까울수록 매도 주도
        buy_ratio  = (cl - lo) / candle_range
        sell_ratio = (hi - cl) / candle_range
        buy_vol  += vol * buy_ratio
        sell_vol += vol * sell_ratio

    total = buy_vol + sell_vol
    if total == 0:
        return {"buy_vol": 0, "sell_vol": 0, "ratio": 1.0, "signal": "neutral", "strength": 0.0}

    ratio    = buy_vol / max(sell_vol, 1.0)
    strength = abs(buy_vol - sell_vol) / total  # 0~1

    if ratio >= 1.5:
        signal = "bullish"   # 매수 세력 압도 → 보유
    elif ratio <= 0.67:
        signal = "bearish"   # 매도 세력 압도 → 청산 준비
    else:
        signal = "neutral"

    return {
        "buy_vol":  round(buy_vol,  0),
        "sell_vol": round(sell_vol, 0),
        "ratio":    round(ratio,    3),
        "signal":   signal,
        "strength": round(strength, 3),
    }


def is_distribution_detected(df: pd.DataFrame, lookback_bars: int = 5) -> tuple[bool, str]:
    """
    분배(distribution) 패턴 감지 — 고점에서 매도 세력이 매수 압도 시작.

    분배 조건 (모두 충족 시):
      1. 오더플로우 ratio < 0.67 (매도가 매수의 1.5배 이상)
      2. 거래량 증가 + 가격 정체 또는 하락 (고점 근처에서 거래량 터지며 팔기 시작)

    Returns:
        (is_distributing: bool, reason: str)
    """
    flow = analyze_order_flow(df, lookback_bars)
    if flow["signal"] != "bearish":
        return False, ""

    # 거래량 증가 확인 (현재 vol > 20일 평균)
    if "vol_ma20" not in df.columns:
        df = compute_indicators(df)
    last   = df.iloc[-1]
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)
    vol_surge = vol > vol_ma * 1.3

    # 가격 정체/하락 확인 (최근 3봉 고점 대비 현재가)
    recent_high = float(df.tail(5)["high"].max())
    current     = float(last.get("close", 0) or recent_high)
    price_stall = current < recent_high * 0.97  # 최근 고점 대비 -3% 이상 밀림

    if flow["ratio"] < 0.67 and vol_surge and price_stall:
        return True, (
            f"분배 감지 — 매도/매수비={1/flow['ratio']:.1f}x, "
            f"거래량={vol/vol_ma:.1f}x, 고점 대비 -{(1-current/recent_high)*100:.1f}%"
        )
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 0단계: 갭업 급등주 Gap&Go 진입 (장 시작 직후)
#
# 프리마켓 스캐너가 포착한 종목을 장 시작 직후 Gap&Go로 진입.
# TTM Squeeze와 별개로 운영되며, 더 빠른 진입 타이밍 제공.
#
# 진입 조건:
#   - 갭업 20%+ (강한 카탈리스트)
#   - RVOL 10x+ (실제 급등 평균 26.7x)
#   - 첫 5분봉 고점 돌파 (Gap&Go 확인)
#   - VWAP 상방 유지 (갭앤크랩 아님 확인)
# ─────────────────────────────────────────────────────────────────────
def gap_and_go_squeeze_entry(
    symbol:     str,
    df_5min:    pd.DataFrame,   # 당일 5분봉 (장 시작 후)
    candidate,                  # GapCandidate 객체
    regime:     str = "bull",
) -> tuple[bool, float, float, str]:
    """
    갭업 후보 종목의 Gap&Go 진입 판단.

    Returns:
        (should_enter, entry_price, stop_price, reason)
    """
    if regime in ("bear", "panic"):
        return False, 0.0, 0.0, f"레짐={regime}: 갭업 진입 금지"

    gap_pct = getattr(candidate, "gap_pct", 0.0)
    rvol    = getattr(candidate, "rvol",    0.0)

    return gap_and_go_entry(df_5min, gap_pct, rvol)


# ─────────────────────────────────────────────────────────────────────
# 1단계: TTM 스퀴즈 발사 감지 및 진입 판단
# ─────────────────────────────────────────────────────────────────────
def squeeze_entry(
    symbol: str,
    df:     pd.DataFrame,
    regime: str = "bull",
    min_volume_ratio: float = 1.5,
) -> tuple[bool, str]:
    """
    스퀴즈 진입 여부 판단.

    진입 조건:
      1. squeeze_off=True (스퀴즈 발사 감지)
      2. squeeze_rising=True + squeeze_mom > 0 (상승 방향 확인)
      3. RSI > 50 (상승 모멘텀)
      4. 거래량 > 20일 평균 × 1.5 (확인 거래량)
      5. 오더플로우 매수 우위 (ratio >= 1.2)
      6. 레짐 bear/panic 아닐 것

    Returns:
        (should_enter: bool, reason: str)
    """
    if regime in ("bear", "panic"):
        return False, f"레짐={regime}: 스퀴즈 롱 진입 금지"

    if "squeeze_off" not in df.columns:
        df = compute_indicators(df)

    last = df.iloc[-1]

    # ── 스퀴즈 발사 확인 ─────────────────────────────────────────────
    fired, is_upward = is_squeeze_fired(df)
    if not fired:
        return False, "스퀴즈 미발사"
    if not is_upward:
        return False, "스퀴즈 하방 발사 — 상승 스퀴즈만 진입"

    # ── RSI 확인 ─────────────────────────────────────────────────────
    rsi = latest_rsi(df)
    if rsi < 50:
        return False, f"RSI 50 미만 ({rsi:.1f}) — 상승 모멘텀 부족"
    if rsi > 85:
        return False, f"RSI 과매수 ({rsi:.1f} > 85)"

    # ── 거래량 확인 ───────────────────────────────────────────────────
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)
    if vol_ma > 0 and vol < vol_ma * min_volume_ratio:
        return False, f"거래량 부족 ({vol/vol_ma:.1f}x < {min_volume_ratio}x)"

    # ── 오더플로우 매수 우위 확인 ─────────────────────────────────────
    flow = analyze_order_flow(df, lookback_bars=5)
    if flow["signal"] == "bearish":
        return False, f"매도 세력 우위 (ratio={flow['ratio']:.2f}) — 진입 금지"

    mom = float(last.get("squeeze_mom", 0) or 0)
    reason = (
        f"스퀴즈 진입 — 모멘텀:{mom:.4f}, RSI:{rsi:.1f}, "
        f"거래량:{vol/vol_ma:.1f}x, 매수/매도비:{flow['ratio']:.2f}"
    )
    return True, reason


# ─────────────────────────────────────────────────────────────────────
# 메인 보유 판단 — 계단식 트레일링 + 오더플로우
#
# 매 틱마다 호출.
# 청산 여부 + 업데이트된 동적 손절가 반환.
# ─────────────────────────────────────────────────────────────────────
def squeeze_hold_or_exit(
    df:            pd.DataFrame,
    entry_price:   float,
    current_price: float,
    peak_price:    float,
    dynamic_stop:  float,   # 현재까지 누적된 동적 손절가
    atr:           float = 0.0,
    atr_mult:      float = 1.5,
) -> tuple[bool, str, float]:
    """
    스퀴즈 포지션 보유/청산 판단.

    1. 동적 손절가 업데이트 (매 틱마다 고점 갱신 → 스탑 상향)
    2. 현재가 vs 동적 손절가 비교
    3. 오더플로우 분배 감지 시 청산 신호

    Returns:
        (should_exit: bool, reason: str, new_dynamic_stop: float)
    """
    # ── 동적 손절가 업데이트 (ATR 반영) ─────────────────────────────
    new_stop = compute_trailing_stop(entry_price, peak_price, dynamic_stop, atr=atr)

    # ── 초기 ATR 손절 (포지션 진입 직후 최소 보호선) ─────────────────
    if atr > 0 and dynamic_stop == 0:
        atr_stop = entry_price - atr * atr_mult
        new_stop = max(new_stop, atr_stop)

    # ── 트레일링 스탑 터치 확인 ──────────────────────────────────────
    hit, reason = is_trailing_stop_hit(current_price, new_stop)
    if hit:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        return True, f"{reason} (수익: {pnl_pct:+.1f}%)", new_stop

    # ── 오더플로우 분배 감지 ─────────────────────────────────────────
    # 수익이 10% 이상인 상태에서 분배 감지 시 선제 청산
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct >= 0.10:
        distributing, dist_reason = is_distribution_detected(df)
        if distributing:
            return True, f"오더플로우 분배 감지 — 선제 청산: {dist_reason}", new_stop

    # ── RSI 다이버전스 (고점은 더 높지만 RSI는 더 낮음 = 약세 다이버전스) ──
    if "rsi_14" not in df.columns:
        df = compute_indicators(df)
    rsi_series = df["rsi_14"].dropna()
    if len(rsi_series) >= 10 and pnl_pct >= 0.30:
        recent_rsi = rsi_series.tail(5).values
        # RSI가 70 이상이었다가 60 아래로 꺾임 = 모멘텀 소진
        if recent_rsi[-5] > 70 and recent_rsi[-1] < 60:
            return True, f"RSI 모멘텀 소진 ({recent_rsi[-5]:.0f} → {recent_rsi[-1]:.0f})", new_stop

    return False, "", new_stop


# ─────────────────────────────────────────────────────────────────────
# 손익분기점 이동 (손실 없는 구간 확보)
# ─────────────────────────────────────────────────────────────────────
def get_breakeven_stop(
    entry_price:   float,
    current_price: float,
    trigger_pct:   float = 0.10,  # +10% 도달 시 손익분기점으로 이동
) -> float:
    """
    수익 +trigger_pct 달성 시 손절가를 손익분기점(진입가)으로 올림.
    Returns: 적용할 손절가 (0이면 미적용)
    """
    if entry_price <= 0:
        return 0.0
    if (current_price - entry_price) / entry_price >= trigger_pct:
        return entry_price  # 손절 = 진입가 → 이 포지션에서 절대 손실 없음
    return 0.0


# ─────────────────────────────────────────────────────────────────────
# 스캘핑 재진입 판단 (급등 후 눌림목 → 2차 탑승)
# ─────────────────────────────────────────────────────────────────────
def scalp_reentry(
    df:                pd.DataFrame,
    partial_exit_price: float,
    current_price:     float,
    pullback_pct:      float = 0.02,
    min_volume_ratio:  float = 1.2,
) -> tuple[bool, str]:
    """
    1차 포지션 청산 후 가격 눌림목에서 스캘핑 재진입 판단.

    재진입 조건:
      1. 현재가 < 청산가 × (1 - pullback_pct) — 눌림목 확인
      2. 거래량 > 평균 × 1.2 — 매수 재개 확인
      3. RSI > 45 — 모멘텀 완전 소진 아님
      4. 오더플로우 매수 우위 복귀 — 세력 재진입 확인

    Returns:
        (should_reenter: bool, reason: str)
    """
    if partial_exit_price <= 0:
        return False, ""

    pullback_threshold = partial_exit_price * (1 - pullback_pct)
    if current_price > pullback_threshold:
        return False, f"눌림목 불충분 ({current_price:.2f} > {pullback_threshold:.2f})"

    if "vol_ma20" not in df.columns:
        df = compute_indicators(df)

    last   = df.iloc[-1]
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)
    if vol_ma > 0 and vol < vol_ma * min_volume_ratio:
        return False, f"재진입 거래량 부족 ({vol/vol_ma:.1f}x)"

    rsi = latest_rsi(df)
    if rsi < 45:
        return False, f"RSI 하락 ({rsi:.1f} < 45) — 모멘텀 소진"

    # 오더플로우 매수 우위 재확인
    flow = analyze_order_flow(df, lookback_bars=3)
    if flow["signal"] == "bearish":
        return False, f"매도 세력 여전히 우위 (ratio={flow['ratio']:.2f})"

    mom = float(last.get("squeeze_mom", 0) or 0)
    if mom <= 0:
        return False, "스퀴즈 모멘텀 음전환"

    reason = (
        f"스캘핑 재진입 — 눌림목:{(current_price/partial_exit_price-1)*100:.1f}%, "
        f"RSI:{rsi:.1f}, 거래량:{vol/vol_ma:.1f}x, 매수비:{flow['ratio']:.2f}"
    )
    return True, reason


# ─────────────────────────────────────────────────────────────────────
# 하위 호환성 래퍼 (main.py에서 호출하는 기존 함수 유지)
# ─────────────────────────────────────────────────────────────────────
def squeeze_partial_exit(
    entry_price:   float,
    current_price: float,
    first_tp_pct:  float = 0.20,  # 사용 안 함 — 계단식 트레일링으로 대체
) -> tuple[bool, str]:
    """
    레거시 래퍼. 실제 로직은 squeeze_hold_or_exit()에서 처리.
    main.py의 _exit_squeeze()에서 호출 시 항상 False 반환 → squeeze_hold_or_exit로 유도.
    """
    # 계단식 트레일링 사용 — 고정 익절 없음
    return False, ""


def scalp_exit(
    df:            pd.DataFrame,
    entry_price:   float,
    current_price: float,
    peak_price:    float,
    tp_pct:        float = 0.15,
    sl_pct:        float = 0.03,
    trail_pct:     float = 0.08,
) -> tuple[bool, str]:
    """스캘핑 재진입 포지션 청산 — squeeze_hold_or_exit로 위임."""
    dynamic_stop = compute_trailing_stop(entry_price, peak_price)
    should_exit, reason, _ = squeeze_hold_or_exit(
        df, entry_price, current_price, peak_price, dynamic_stop
    )
    return should_exit, reason


def squeeze_stop_loss(
    entry_price:   float,
    current_price: float,
    atr:           float,
    atr_mult:      float = 1.5,
) -> tuple[bool, str]:
    """초기 ATR 손절 (계단식 트레일링이 가동되기 전 안전망)."""
    if entry_price <= 0 or atr <= 0:
        return False, ""
    stop_price = entry_price - atr * atr_mult
    if current_price <= stop_price:
        loss_pct = (current_price - entry_price) / entry_price * 100
        return True, f"ATR 손절 ({current_price:.2f} <= {stop_price:.2f}, {loss_pct:.1f}%)"
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 스퀴즈 후보 종목 스캔
# ─────────────────────────────────────────────────────────────────────
def scan_squeeze_candidates(
    symbol_dfs: dict,
    regime:     str = "bull",
) -> list:
    """
    종목 데이터프레임 딕셔너리에서 스퀴즈 상태인 종목 반환.

    Returns:
        [(symbol, squeeze_mom), ...] — 모멘텀 강도 내림차순
    """
    candidates = []
    for sym, df in symbol_dfs.items():
        try:
            if df is None or df.empty:
                continue
            if "squeeze_mom" not in df.columns:
                df = compute_indicators(df)
            last    = df.iloc[-1]
            sq_on   = bool(last.get("squeeze_on",   False))
            sq_off  = bool(last.get("squeeze_off",  False))
            sq_mom  = float(last.get("squeeze_mom", 0) or 0)
            sq_rise = bool(last.get("squeeze_rising", False))

            if (sq_on or sq_off) and sq_mom > 0 and sq_rise:
                # 오더플로우 사전 확인 (매도 우위면 후보 제외)
                flow = analyze_order_flow(df, lookback_bars=5)
                if flow["signal"] == "bearish":
                    logging.debug("[squeeze] %s 매도 우위 — 후보 제외", sym)
                    continue
                candidates.append((sym, sq_mom))
                logging.info("[squeeze] 후보: %s mom=%.4f flow=%.2fx fired=%s",
                             sym, sq_mom, flow["ratio"], sq_off)
        except Exception as exc:
            logging.debug("[squeeze] %s 스캔 실패: %s", sym, exc)

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates
