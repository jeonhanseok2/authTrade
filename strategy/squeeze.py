# strategy/squeeze.py
"""
버킷 3: 스퀴즈 + 급등주 초단타 전략.

전략 개요:
  1단계 (스퀴즈 감지): TTM Squeeze 지표로 에너지 축적 구간 탐지
  2단계 (방향 예측):   Momentum 히스토그램 방향 + RSI + 거래량으로 상승/하락 예측
  3단계 (진입):        스퀴즈 발사(squeeze_off=True) 후 상승 돌파 확인 시 매수
  4단계 (수익실현):    1차 목표가(+5%) 도달 시 절반 청산, 나머지는 트레일링
  5단계 (스캘핑 재진입): 가격 하락 시 거래량 확인 후 재진입(초단타 스캘핑)
  6단계 (종료):        재진입 후 목표 달성 또는 거래량 감소 시 완전 청산

핵심 지표:
  - TTM Squeeze (squeeze_on / squeeze_off / squeeze_mom / squeeze_rising)
  - RSI: 방향 확인 (> 50 = 상승 모멘텀)
  - 거래량: 브레이크아웃 후 vol > vol_ma20 × 2 = 강한 돌파
  - ATR: 손절 거리 계산
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from strategy.signals import compute_indicators, is_squeeze_fired, latest_rsi


# ─────────────────────────────────────────────────────────────────────
# 스퀴즈 상태 정보 (포지션 관리에 필요)
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SqueezePosition:
    """스퀴즈 포지션 상태 추적."""
    symbol:       str   = ""
    phase:        str   = "none"    # none / entered / partial_exit / scalp_reentry
    entry_price:  float = 0.0
    first_tp:     float = 0.0      # 1차 목표가 (+5%)
    scalp_entry:  float = 0.0      # 스캘핑 재진입가
    peak_price:   float = 0.0
    qty_full:     int   = 0        # 초기 수량
    qty_remaining: int  = 0        # 절반 청산 후 남은 수량


# ─────────────────────────────────────────────────────────────────────
# 1단계: 스퀴즈 발사 감지 및 진입 판단
# ─────────────────────────────────────────────────────────────────────
def squeeze_entry(
    symbol: str,
    df:     pd.DataFrame,
    regime: str = "bull",
    min_volume_ratio: float = 1.5,  # 거래량 비율 최소 기준 (평균 대비)
) -> tuple[bool, str]:
    """
    스퀴즈 진입 여부 판단.

    진입 조건:
      1. squeeze_off=True (스퀴즈 발사 감지)
      2. squeeze_rising=True + squeeze_mom > 0 (상승 방향 확인)
      3. RSI > 50 (상승 모멘텀)
      4. 거래량 > 20일 평균 × 1.5 (확인 거래량)
      5. 레짐 bear/panic 아닐 것

    Returns:
        (should_enter: bool, reason: str)
    """
    # bear/panic 레짐에서 스퀴즈 롱 진입 금지
    if regime in ("bear", "panic"):
        return False, f"레짐={regime}: 스퀴즈 롱 진입 금지"

    # ── 지표 계산 ─────────────────────────────────────────────────────
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
    if rsi > 80:
        return False, f"RSI 과매수 ({rsi:.1f} > 80) — 진입 위험"

    # ── 거래량 확인 ───────────────────────────────────────────────────
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)
    if vol_ma > 0 and vol < vol_ma * min_volume_ratio:
        return False, f"거래량 부족 ({vol/vol_ma:.1f}x < {min_volume_ratio}x 평균)"

    # ── 모멘텀 강도 ───────────────────────────────────────────────────
    mom = float(last.get("squeeze_mom", 0) or 0)
    reason = (
        f"스퀴즈 진입 — 모멘텀:{mom:.3f}, RSI:{rsi:.1f}, "
        f"거래량:{vol/vol_ma:.1f}x"
    )
    return True, reason


# ─────────────────────────────────────────────────────────────────────
# 4단계: 분할 청산 + 트레일링 스탑 (급등주 50~300% 대응)
#
# 급등주는 스퀴즈 발사 후 50~300% 갈 수 있으므로
# 고정 목표가(5%)로 전량 청산하면 대부분의 수익을 놓침.
#
# 전략:
#   +10% 도달 → 손절선을 손익분기점으로 올림 (리스크 0 구간 확보)
#   +20% 도달 → 25% 분할 청산 (1차), 나머지 계속 보유
#   +40% 도달 → 추가 25% 청산 (2차)
#   이후      → 거래량 소진 / 모멘텀 역전 / 트레일링 스탑으로 나머지 청산
# ─────────────────────────────────────────────────────────────────────

# 분할 청산 레벨 정의 (pnl_pct 기준)
SCALE_OUT_LEVELS = [
    {"pnl_pct": 0.20, "sell_ratio": 0.25, "label": "1차 분할 청산"},  # +20%에서 25%
    {"pnl_pct": 0.40, "sell_ratio": 0.33, "label": "2차 분할 청산"},  # +40%에서 잔량의 33%
    {"pnl_pct": 0.80, "sell_ratio": 0.50, "label": "3차 분할 청산"},  # +80%에서 잔량의 50%
]


def squeeze_scale_out(
    entry_price:   float,
    current_price: float,
    sold_levels:   list,  # 이미 청산된 레벨 인덱스 목록
) -> tuple[bool, str, int, float]:
    """
    분할 청산 레벨 도달 확인.

    Returns:
        (should_partial: bool, reason: str, level_idx: int, sell_ratio: float)
        sell_ratio: 현재 잔량 대비 청산 비율 (0.25 = 25%)
    """
    if entry_price <= 0:
        return False, "", -1, 0.0
    pnl_pct = (current_price - entry_price) / entry_price

    for idx, level in enumerate(SCALE_OUT_LEVELS):
        if idx in sold_levels:
            continue  # 이미 청산된 레벨 스킵
        if pnl_pct >= level["pnl_pct"]:
            return (
                True,
                f"{level['label']} ({pnl_pct*100:.1f}% >= +{level['pnl_pct']*100:.0f}%)",
                idx,
                level["sell_ratio"],
            )
    return False, "", -1, 0.0


def squeeze_breakeven_stop(
    entry_price:   float,
    current_price: float,
    breakeven_trigger_pct: float = 0.10,  # +10% 도달 시 손익분기점으로 손절 이동
) -> float:
    """
    수익 +10% 도달 시 손절가를 손익분기점(진입가)으로 올림.
    Returns: 적용할 손절가 (0.0이면 변경 없음)
    """
    if entry_price <= 0:
        return 0.0
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct >= breakeven_trigger_pct:
        return entry_price  # 손절을 진입가로 이동 (손실 없음 보장)
    return 0.0


# 하위 호환성을 위한 래퍼 (기존 _exit_squeeze 코드에서 호출 가능)
def squeeze_partial_exit(
    entry_price:   float,
    current_price: float,
    first_tp_pct:  float = 0.20,  # 1차 분할 청산 기준 (기존 5% → 20%로 수정)
) -> tuple[bool, str]:
    """
    1차 분할 청산 레벨 도달 확인 (단순 버전).
    급등주 대응: 5% 고정 익절 대신 20% 도달 시 25% 분할 청산.

    Returns:
        (should_partial_exit: bool, reason: str)
    """
    if entry_price <= 0:
        return False, ""
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct >= first_tp_pct:
        return True, f"분할 청산 ({pnl_pct*100:.1f}% >= +{first_tp_pct*100:.0f}%, 잔량 25% 매도)"
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 5단계: 스캘핑 재진입 판단 (익절 후 눌림목)
# ─────────────────────────────────────────────────────────────────────
def scalp_reentry(
    df:            pd.DataFrame,
    partial_exit_price: float,     # 1차 익절가
    current_price: float,
    pullback_pct:  float = 0.02,   # 눌림목 기준 (익절가 대비 -2%)
    min_volume_ratio: float = 1.2, # 재진입 거래량 기준 (낮춤)
) -> tuple[bool, str]:
    """
    1차 익절 후 가격 눌림목에서 스캘핑 재진입 판단.

    재진입 조건:
      1. 현재가 < 익절가 × (1 - pullback_pct) (충분히 눌림)
      2. 거래량 > vol_ma × 1.2 (거래량 유지)
      3. RSI > 45 (모멘텀이 완전히 꺾이지 않음)
      4. squeeze_mom > 0 (상승 모멘텀 유지)

    Returns:
        (should_reenter: bool, reason: str)
    """
    if partial_exit_price <= 0:
        return False, ""

    # 눌림목 기준 확인
    pullback_threshold = partial_exit_price * (1 - pullback_pct)
    if current_price > pullback_threshold:
        return False, f"눌림목 불충분 ({current_price:.2f} > {pullback_threshold:.2f})"

    if "squeeze_mom" not in df.columns:
        df = compute_indicators(df)

    last = df.iloc[-1]

    # ── 거래량 확인 ───────────────────────────────────────────────────
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)
    if vol_ma > 0 and vol < vol_ma * min_volume_ratio:
        return False, f"재진입 거래량 부족 ({vol/vol_ma:.1f}x)"

    # ── RSI + 모멘텀 확인 ────────────────────────────────────────────
    rsi = latest_rsi(df)
    if rsi < 45:
        return False, f"RSI 하락 ({rsi:.1f} < 45) — 모멘텀 소진"

    mom = float(last.get("squeeze_mom", 0) or 0)
    if mom <= 0:
        return False, "스퀴즈 모멘텀 음전환 — 재진입 위험"

    reason = (
        f"스캘핑 재진입 — 눌림목:{(current_price/partial_exit_price-1)*100:.1f}%, "
        f"RSI:{rsi:.1f}, 거래량:{vol/vol_ma:.1f}x"
    )
    return True, reason


# ─────────────────────────────────────────────────────────────────────
# 6단계: 모멘텀 소진 청산 (급등 후 잔량 포지션)
#
# 급등주 포지션은 거래량 소진 + 모멘텀 역전 신호가 나올 때까지 보유.
# 트레일링 스탑으로 수익을 최대한 보호하면서 끝까지 끌고 간다.
# ─────────────────────────────────────────────────────────────────────
def scalp_exit(
    df:            pd.DataFrame,
    entry_price:   float,
    current_price: float,
    peak_price:    float,
    tp_pct:        float = 0.15,    # 눌림목 스캘핑 목표 +15% (기존 3% → 15%)
    sl_pct:        float = 0.03,    # 손절 -3% (기존 1.5% → 3%, 변동성 고려)
    trail_pct:     float = 0.08,    # 트레일링 8% (기존 1.5% → 8%, 큰 움직임 허용)
) -> tuple[bool, str]:
    """
    급등 후 잔량 포지션 / 스캘핑 재진입 포지션 청산 판단.

    청산 조건 (우선순위 순):
      1. 손절 (-3%)
      2. 트레일링 스탑 (고점 대비 -8%) — 넓은 여유로 큰 상승 허용
      3. 거래량 소진 (vol < vol_ma × 0.5) + 수익 중 — 모멘텀 소진 신호
      4. RSI 하락 전환 (70 → 50 이하) — 모멘텀 소진
      5. 목표가 +15% (스캘핑 재진입 전용 — 잔량 포지션은 적용 안 함)

    Returns:
        (should_exit: bool, reason: str)
    """
    if entry_price <= 0:
        return False, ""

    pnl_pct = (current_price - entry_price) / entry_price

    # ── 손절 ─────────────────────────────────────────────────────────
    if pnl_pct <= -sl_pct:
        return True, f"손절 ({pnl_pct*100:.1f}% <= -{sl_pct*100:.0f}%)"

    # ── 트레일링 스탑 (넓게 — 큰 급등 허용) ─────────────────────────
    if peak_price > entry_price * 1.10:  # 최소 10% 수익 구간에서만 트레일링 가동
        drawdown = (current_price - peak_price) / peak_price
        if drawdown <= -trail_pct:
            return True, f"트레일링 스탑 (고점 {peak_price:.2f} 대비 {drawdown*100:.1f}%)"

    if "vol_ma20" not in df.columns:
        df = compute_indicators(df)
    last   = df.iloc[-1]
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)

    # ── 거래량 소진 + 수익 중 ──────────────────────────────────────
    if vol_ma > 0 and vol < vol_ma * 0.5 and pnl_pct > 0.10:
        return True, f"거래량 소진 ({vol/vol_ma:.1f}x < 0.5x) — 모멘텀 끝, 수익 실현"

    # ── RSI 모멘텀 역전 ───────────────────────────────────────────────
    rsi_series = df.get("rsi_14", pd.Series(dtype=float))
    if len(rsi_series.dropna()) >= 3:
        rsi_vals = rsi_series.dropna().tail(3).values
        # RSI가 70 이상이었다가 50 이하로 떨어지면 모멘텀 소진
        if rsi_vals[-3] > 70 and rsi_vals[-1] < 50 and pnl_pct > 0.05:
            return True, f"RSI 모멘텀 역전 ({rsi_vals[-3]:.0f} → {rsi_vals[-1]:.0f}) — 청산"

    # ── 스캘핑 재진입 목표가 ─────────────────────────────────────────
    if pnl_pct >= tp_pct:
        return True, f"스캘핑 목표가 ({pnl_pct*100:.1f}% >= +{tp_pct*100:.0f}%)"

    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 공통 손절 (스퀴즈 초기 포지션 전체)
# ─────────────────────────────────────────────────────────────────────
def squeeze_stop_loss(
    entry_price:   float,
    current_price: float,
    atr:           float,
    atr_mult:      float = 1.5,
) -> tuple[bool, str]:
    """
    ATR 기반 손절 (스퀴즈 진입 직후 전체 포지션 보호).

    손절가 = 진입가 - (ATR × atr_mult)
    """
    if entry_price <= 0 or atr <= 0:
        return False, ""
    stop_price = entry_price - atr * atr_mult
    if current_price <= stop_price:
        loss_pct = (current_price - entry_price) / entry_price * 100
        return True, f"ATR 손절 (현재 {current_price:.2f} <= 손절가 {stop_price:.2f}, {loss_pct:.1f}%)"
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 스퀴즈 후보 종목 스캔
# ─────────────────────────────────────────────────────────────────────
def scan_squeeze_candidates(
    symbol_dfs: dict[str, pd.DataFrame],
    regime:     str = "bull",
) -> list[tuple[str, float]]:
    """
    종목 데이터프레임 딕셔너리에서 스퀴즈 상태인 종목 반환.

    Returns:
        [(symbol, squeeze_mom), ...] — 모멘텀 강도 내림차순
    """
    candidates = []
    for sym, df in symbol_dfs.items():
        try:
            if "squeeze_mom" not in df.columns:
                df = compute_indicators(df)
            if df.empty:
                continue
            last     = df.iloc[-1]
            sq_on    = bool(last.get("squeeze_on", False))
            sq_off   = bool(last.get("squeeze_off", False))
            sq_mom   = float(last.get("squeeze_mom", 0) or 0)
            sq_rise  = bool(last.get("squeeze_rising", False))

            # 발사 직전(sq_on) 또는 발사 직후(sq_off) 종목 선별
            if (sq_on or sq_off) and sq_mom > 0 and sq_rise:
                candidates.append((sym, sq_mom))
                logging.info("[squeeze] 후보: %s mom=%.4f fired=%s", sym, sq_mom, sq_off)
        except Exception as exc:
            logging.debug("[squeeze] %s 스캔 실패: %s", sym, exc)

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates
