from dataclasses import dataclass
from settings import settings
@dataclass
class AccountState: equity: float; day_pnl: float
class TradingGuard:
    def can_enter(self, account: AccountState, open_positions: int, order_value: float) -> bool:
        if account.equity>0 and (account.day_pnl/account.equity)<=-settings.max_daily_loss: return False
        if order_value>settings.max_order_value: return False
        if open_positions>=settings.max_positions: return False
        return True
