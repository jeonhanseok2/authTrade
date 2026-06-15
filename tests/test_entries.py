import pandas as pd
import numpy as np
from strategy.entries import momentum_entry, value_entry

MOM_RULES = {
    "lookback_minutes": 30,
    "min_intraday_change_pct": 5.0,
    "vol_spike_ratio": 2.0,
    "min_price_usd": 3.0,
    "rsi_entry_max": 75.0,
    "require_macd_positive": False,  # 단순 테스트 — MACD 비활성
}


def _make_df(prices, vols=None):
    n = len(prices)
    if vols is None:
        vols = [1_000_000] * n
    idx = pd.date_range("2024-01-02 09:30", periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices, "volume": vols}, index=idx)


def test_momentum_entry_passes():
    prices = [10.0] * 30 + [10.6]  # +6% on last bar
    vols   = [500_000] * 30 + [1_500_000]  # 3x spike
    df = _make_df(prices, vols)
    assert momentum_entry(df, MOM_RULES) is True


def test_momentum_entry_no_change():
    df = _make_df([10.0] * 31)
    assert momentum_entry(df, MOM_RULES) is False


def test_momentum_entry_no_volume_spike():
    prices = [10.0] * 30 + [10.6]
    vols   = [1_000_000] * 31  # no spike
    df = _make_df(prices, vols)
    assert momentum_entry(df, MOM_RULES) is False


def test_momentum_entry_penny_stock():
    prices = [2.0] * 30 + [2.2]  # below min_price_usd=3
    vols   = [500_000] * 30 + [1_500_000]
    df = _make_df(prices, vols)
    assert momentum_entry(df, MOM_RULES) is False


VAL_RULES = {
    "max_market_cap_usd": 5e9,
    "max_per_vs_group": 0.7,
    "min_eps_growth": 0.10,
    "min_liquidity_usd": 1_000_000,
    "rsi_entry_threshold": 99,  # disable RSI filter in test
}
GOOD_INFO = {
    "symbol":          "XYZ",
    "marketCap":       1e9,
    "trailingPE":      10.0,
    "groupPe":         20.0,
    "epsGrowth":       0.15,
    "avgDollarVolume": 2_000_000,
}


def test_value_entry_passes():
    assert value_entry(GOOD_INFO, VAL_RULES) is True


def test_value_entry_large_cap_fails():
    info = {**GOOD_INFO, "marketCap": 6e9}
    assert value_entry(info, VAL_RULES) is False


def test_value_entry_high_pe_fails():
    info = {**GOOD_INFO, "trailingPE": 16.0}  # 16 >= 0.7*20=14
    assert value_entry(info, VAL_RULES) is False


def test_value_entry_low_eps_fails():
    info = {**GOOD_INFO, "epsGrowth": 0.05}
    assert value_entry(info, VAL_RULES) is False
