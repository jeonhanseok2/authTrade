# risk/guard.py
from dataclasses import dataclass
from settings import settings

@dataclass
class AccountState:
    equity: float
    day_pnl: float  # realized + unrealized today

class TradingGuard:
    def __init__(self, get_index_pct=lambda: 0.0):
        # get_index_pct: callable -> float (e.g., QQQ % change today)
        self.get_index_pct = get_index_pct
        self._cooldown_until = None  # reserved

    def market_halt(self) -> bool:
        """지수 급락 등 시장 중단 트리거가 있으면 True"""
        return False

    def can_enter(self, account: AccountState, open_positions: int, order_value: float) -> bool:
        # 일 손실 한도
        if account.equity > 0 and (account.day_pnl / account.equity) <= -settings.max_daily_loss:
            return False
        # 주문 단건 상한
        if order_value > settings.max_order_value:
            return False
        # 보유 포지션 수 상한
        if open_positions >= settings.max_positions:
            return False
        return True
