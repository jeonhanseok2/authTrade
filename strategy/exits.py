# strategy/exits.py
"""
청산 조건 함수 모음.

완전청산 우선순위:
  1. hard_stop_gap_down   — 장 시작 갭락 > 손절폭 → 즉시 시장가 청산
  2. effective_stop_price — max(고정손절가, ATR손절가) 더 타이트한 쪽
  3. breakeven_stop_hit   — 고점 +15% 이후 현재가 진입가 이하 → 본전 스탑
  4. take_profit_hit      — 목표가 도달
  5. trailing_stop_active — 트레일링 스탑
  6. rsi_overbought_exit  — RSI 과매수 모멘텀 소진
  7. eod_exit             — 장 마감 전 인트라데이 강제청산
  8. bid_ask_spread_exit  — Spread 급확대 (버킷3 전용)

분할청산 (B3 squeeze 전용):
  +20% → 잔량 25% 매도 (stage 1)
  +40% → 잔량 33% 매도 (stage 2)
  +80% → 잔량 50% 매도 (stage 3)
  이후  → ATR 트레일링으로 잔량 추적
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import pandas as pd
import zoneinfo

from strategy.signals import latest_rsi, compute_indicators

ET = zoneinfo.ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────
# 기존 청산 함수
# ─────────────────────────────────────────────────────────────────────

def stop_loss_hit(entry_price: float, last_price: float, cfg: Dict[str, Any]) -> bool:
    sl = float(cfg.get("stop_loss_pct", 0.05))
    return last_price <= entry_price * (1.0 - sl)


def take_profit_hit(entry_price: float, last_price: float, cfg: Dict[str, Any]) -> bool:
    tp = float(cfg.get("take_profit_pct", 0.10))
    return last_price >= entry_price * (1.0 + tp)


def trailing_stop_active(
    entry_price: float,
    last_price: float,
    peak_price: float,
    cfg: Dict[str, Any],
) -> bool:
    trail_after = float(cfg.get("trail_after_profit_pct", 0.10))
    trailing    = float(cfg.get("trailing_stop_pct", 0.02))
    if peak_price >= entry_price * (1.0 + trail_after):
        return last_price <= peak_price * (1.0 - trailing)
    return False


def rsi_overbought_exit(df: pd.DataFrame, threshold: float = 80.0) -> bool:
    """RSI >= threshold → 모멘텀 소진 청산 신호."""
    df_ind = compute_indicators(df) if "rsi_14" not in df.columns else df
    return latest_rsi(df_ind) >= threshold


def eod_exit(now: datetime, minutes_before_close: int = 15) -> bool:
    """장 마감 N분 전 True → 인트라데이 포지션 강제 청산. 주말은 False."""
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    close_et      = et.replace(hour=16, minute=0, second=0, microsecond=0)
    mins_to_close = (close_et - et).total_seconds() / 60.0
    return 0 <= mins_to_close <= minutes_before_close


# ─────────────────────────────────────────────────────────────────────
# 신규: 하드 스탑 (갭락 대응)
# ─────────────────────────────────────────────────────────────────────

def hard_stop_gap_down(
    entry_price: float,
    open_price:  float,
    stop_pct:    float,
) -> bool:
    """
    장 시작 갭락이 설정 손절폭을 초과하면 즉시 시장가 청산 트리거.

    갭락 = (entry_price - open_price) / entry_price
    예) entry=100, open=91, stop_pct=0.08 → 갭락 9% > 8% → True

    Args:
        entry_price: 진입가
        open_price:  당일 시가 (갭락 감지용)
        stop_pct:    설정 손절 비율 (예: 0.08 = -8%)

    Returns:
        True → 하드 스탑 발동 (즉시 시장가 청산 필요)
    """
    if entry_price <= 0 or open_price <= 0:
        return False
    gap_down_pct = (entry_price - open_price) / entry_price
    return gap_down_pct > stop_pct


# ─────────────────────────────────────────────────────────────────────
# 신규: 고정손절 vs ATR손절 우선순위
# ─────────────────────────────────────────────────────────────────────

def effective_stop_price(
    entry_price:   float,
    fixed_stop_pct: float,
    peak_price:    float,
    atr:           float,
    atr_mult:      float = 2.0,
) -> float:
    """
    고정손절가 vs ATR손절가 중 더 타이트한(높은) 가격 반환.

    - 고정손절가  = entry × (1 - fixed_stop_pct)
    - ATR손절가   = peak  - (atr × atr_mult)
    - 더 높은 가격 = 더 빨리 손절 → 리스크 작음

    ATR=0 이면 고정손절가만 사용.

    Args:
        entry_price:    진입가
        fixed_stop_pct: 고정 손절 비율 (예: 0.08)
        peak_price:     최고가 (트레일링 기준)
        atr:            ATR 값 (0이면 고정손절만 사용)
        atr_mult:       ATR 배수

    Returns:
        float: 유효 손절가 (이 가격 이하로 내려오면 청산)
    """
    fixed_stop = entry_price * (1.0 - fixed_stop_pct)
    if atr <= 0:
        return fixed_stop
    atr_stop = peak_price - (atr * atr_mult)
    # max = 더 높은 가격 = 더 타이트한 손절
    return max(fixed_stop, atr_stop)


# ─────────────────────────────────────────────────────────────────────
# 신규: Bid-Ask Spread 탈출 (버킷 3 전용)
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# 신규: Breakeven Stop
# ─────────────────────────────────────────────────────────────────────

def breakeven_stop_hit(
    entry_price:  float,
    last_price:   float,
    peak_price:   float,
    trigger_pct:  float = 0.15,
) -> bool:
    """
    본전 스탑: 최고가가 +trigger_pct 이상 도달한 후 현재가가 진입가 이하로 내려오면 청산.

    목적: 한때 크게 올랐다가 원점 복귀하는 상황에서 손실 없이 탈출.
    예) 진입 $100 → 고점 $117(+17%) → 현재 $99 → True (본전 스탑 발동)
    """
    if entry_price <= 0 or peak_price < entry_price * (1.0 + trigger_pct):
        return False
    return last_price <= entry_price


# ─────────────────────────────────────────────────────────────────────
# 신규: 분할 청산 스케줄 (B3 squeeze 전용)
# ─────────────────────────────────────────────────────────────────────

# (수익률 트리거, 잔량 중 매도 비율)
PARTIAL_EXIT_SCHEDULE: list[tuple[float, float]] = [
    (0.20, 0.25),   # +20% → 잔량 25% 매도
    (0.40, 0.33),   # +40% → 잔량 33% 매도
    (0.80, 0.50),   # +80% → 잔량 50% 매도
]


def partial_exit_check(
    entry_price:   float,
    last_price:    float,
    current_stage: int,
) -> tuple[int, float]:
    """
    분할 청산 스케줄 확인.

    Args:
        entry_price:   진입가
        last_price:    현재가
        current_stage: 현재까지 완료된 분할 청산 단계 (0=없음, 1~3)

    Returns:
        (new_stage, sell_ratio)
        sell_ratio > 0  → 잔량 중 해당 비율 즉시 매도
        sell_ratio == 0 → 아직 분할 청산 시점 아님
    """
    if entry_price <= 0:
        return current_stage, 0.0
    for i, (trigger_pct, sell_ratio) in enumerate(PARTIAL_EXIT_SCHEDULE):
        stage = i + 1
        if current_stage >= stage:
            continue
        if last_price >= entry_price * (1.0 + trigger_pct):
            return stage, sell_ratio
    return current_stage, 0.0


def bid_ask_spread_exit(
    bid: float,
    ask: float,
    threshold_pct: float = 0.015,
) -> bool:
    """
    Bid-Ask Spread이 임계치를 초과하면 즉시 탈출 신호.

    유동성 고갈 or 세력 이탈 시 spread가 급격히 벌어짐.
    기본 임계치 1.5% — 급등주는 정상 spread 0.1~0.5%.

    Args:
        bid:           매수 호가
        ask:           매도 호가
        threshold_pct: spread 임계치 (기본 1.5%)

    Returns:
        True → spread 탈출 신호
    """
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid        = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid
    return spread_pct >= threshold_pct
