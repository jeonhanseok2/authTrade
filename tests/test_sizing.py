from strategy.sizing import atr_position_size, budget_cap_size


def test_normal_sizing():
    qty = atr_position_size(atr=2.0, account_equity=100_000, risk_per_trade_pct=0.01,
                            price=50.0, atr_multiplier=2.0)
    # risk = 100000 * 0.01 = 1000, stop = 2*2 = 4, shares = 1000/4 = 250
    assert qty == 250


def test_zero_atr_returns_zero():
    qty = atr_position_size(atr=0.0, account_equity=100_000, risk_per_trade_pct=0.01,
                            price=50.0)
    assert qty == 0


def test_zero_price_returns_zero():
    qty = atr_position_size(atr=1.0, account_equity=100_000, risk_per_trade_pct=0.01,
                            price=0.0)
    assert qty == 0


def test_budget_cap_size():
    qty = budget_cap_size(budget_usd=10_000, price=55.0)
    assert qty == 181  # int(10000 // 55)


def test_budget_cap_zero_price():
    assert budget_cap_size(10_000, 0.0) == 0
