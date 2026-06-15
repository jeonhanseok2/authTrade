# strategy/exits.py
"""
청산 조건 함수 모음.

우선순위:
  1. hard_stop_gap_down   — 장 시작 갭락 > 손절폭 → 즉시 시장가 청산
  2. effective_stop_price — min(고정손절가, ATR손절가) 더 타이트한 쪽
  3. take_profit_hit      — 목표가 도달
  4. trailing_stop_active — 트레일링 스탑
  5. rsi_overbought_exit  — RSI 과매수 모멘텀 소진
  6. eod_exit             — 장 마감 전 인트라데이 강제청산
  7. bid_ask_spread_exit  — Spread 급확대 (버킷3 전용)
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
