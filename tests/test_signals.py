import pandas as pd
import numpy as np
from strategy.signals import compute_indicators, latest_rsi, atr_for_sizing


def _make_ohlcv(n=50, start_price=100.0):
    prices = [start_price + i * 0.5 for i in range(n)]
    idx    = pd.date_range("2024-01-02 09:30", periods=n, freq="1min")
    return pd.DataFrame({
        "open":   prices,
        "high":   [p + 0.2 for p in prices],
        "low":    [p - 0.2 for p in prices],
        "close":  prices,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_compute_indicators_columns():
    df  = _make_ohlcv(50)
    out = compute_indicators(df)
    for col in ("rsi_14", "macd", "macd_signal", "macd_hist", "atr_14",
                "bb_upper", "bb_mid", "bb_lower", "sma_10", "sma_50"):
        assert col in out.columns, f"missing column: {col}"


def test_rsi_range():
    df  = _make_ohlcv(60)
    out = compute_indicators(df)
    rsi = out["rsi_14"].dropna()
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_latest_rsi_returns_float():
    df  = _make_ohlcv(50)
    val = latest_rsi(df)
    assert isinstance(val, float)
    assert 0 <= val <= 100


def test_atr_positive():
    df  = _make_ohlcv(50)
    val = atr_for_sizing(df)
    assert val >= 0.0


def test_short_df_rsi_fallback():
    df  = _make_ohlcv(5)  # too short for RSI
    val = latest_rsi(df)
    assert val == 50.0  # fallback value
