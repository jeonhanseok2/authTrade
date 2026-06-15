from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from settings import settings


@dataclass
class AccountState:
    equity: float
    day_pnl: float  # realized + unrealized today


class TradingGuard:
    def __init__(self, get_index_pct=lambda: 0.0):
        self.get_index_pct = get_index_pct
        self._cooldown_until: datetime | None = None

    def market_halt(self) -> bool:
        if self.get_index_pct() <= -7.0:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            return True
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            return True
        return False

    def can_enter(
        self,
        account: AccountState,
        open_positions: int,
        order_value: float,
    ) -> bool:
        if account.equity > 0 and (account.day_pnl / account.equity) <= -settings.max_daily_loss:
            return False
        if order_value > settings.max_order_value:
            return False
        if open_positions >= settings.max_positions:
            return False
        return True
