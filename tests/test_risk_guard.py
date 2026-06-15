from risk.guard import AccountState, TradingGuard


def test_can_enter_blocks_daily_loss():
    guard = TradingGuard()
    acct = AccountState(equity=100_000, day_pnl=-2_100)  # -2.1% > 2% limit
    assert guard.can_enter(acct, 0, 1_000) is False


def test_can_enter_blocks_max_positions():
    guard = TradingGuard()
    acct = AccountState(equity=100_000, day_pnl=0)
    assert guard.can_enter(acct, 8, 1_000) is False  # max_positions default = 8


def test_can_enter_allows_normal():
    guard = TradingGuard()
    acct = AccountState(equity=100_000, day_pnl=-500)  # -0.5%
    assert guard.can_enter(acct, 3, 10_000) is True


def test_market_halt_triggers_at_minus7():
    guard = TradingGuard(get_index_pct=lambda: -7.1)
    assert guard.market_halt() is True


def test_market_halt_not_triggered():
    guard = TradingGuard(get_index_pct=lambda: -3.0)
    assert guard.market_halt() is False
