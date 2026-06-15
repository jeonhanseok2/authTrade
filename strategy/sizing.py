# strategy/sizing.py
"""
ATR 기반 포지션 사이징.
risk_amount = account_equity * risk_per_trade_pct
stop_distance = atr * atr_multiplier
shares = risk_amount / stop_distance
"""
from __future__ import annotations


def atr_position_size(
    atr: float,
    account_equity: float,
    risk_per_trade_pct: float,
    price: float,
    atr_multiplier: float = 2.0,
) -> int:
    """
    ATR 기반 주문 수량 산출.

    Args:
        atr: 14기간 ATR 값
        account_equity: 전체 포트폴리오 평가액
        risk_per_trade_pct: 거래당 리스크 비율 (예: 0.01 = 1%)
        price: 현재 종목 가격
        atr_multiplier: 손절 거리 = ATR × multiplier

    Returns:
        int: 주문 수량 (최소 0)
    """
    if atr <= 0 or price <= 0 or account_equity <= 0:
        return 0
    stop_distance = atr * atr_multiplier
    dollar_risk   = account_equity * risk_per_trade_pct
    shares        = int(dollar_risk / stop_distance)
    return max(0, shares)


def budget_cap_size(budget_usd: float, price: float) -> int:
    """고정 예산 기반 수량 산출 (ATR 미사용 fallback)."""
    if price <= 0:
        return 0
    return max(0, int(budget_usd // price))
