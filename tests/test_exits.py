from datetime import datetime, timezone
import pandas as pd
import numpy as np
from strategy.exits import (
    stop_loss_hit, take_profit_hit, trailing_stop_active,
    rsi_overbought_exit, eod_exit,
)

CFG = {"stop_loss_pct": 0.05, "take_profit_pct": 0.10,
       "trail_after_profit_pct": 0.10, "trailing_stop_pct": 0.02}


def test_stop_loss_hit():
    assert stop_loss_hit(100.0, 94.0, CFG) is True


def test_stop_loss_not_hit():
    assert stop_loss_hit(100.0, 96.0, CFG) is False


def test_take_profit_hit():
    assert take_profit_hit(100.0, 111.0, CFG) is True


def test_take_profit_not_hit():
    assert take_profit_hit(100.0, 109.0, CFG) is False


def test_trailing_stop_active():
    assert trailing_stop_active(100.0, 108.0, 112.0, CFG) is True  # peak=112, last=108 < 112*0.98


def test_trailing_stop_not_activated_yet():
    assert trailing_stop_active(100.0, 105.0, 108.0, CFG) is False  # peak < entry*1.10


def test_rsi_overbought_exit_false_on_normal():
    close = pd.Series([100.0 + i * 0.1 for i in range(50)])
    idx   = pd.date_range("2024-01-02", periods=50, freq="1min")
    df    = pd.DataFrame({"close": close, "open": close, "high": close, "low": close,
                          "volume": [1_000_000] * 50}, index=idx)
    # gentle uptrend → RSI around 60–70, not >=80
    assert rsi_overbought_exit(df, threshold=80.0) is False


def test_eod_exit_before_close():
    # 15:50 ET on a weekday
    dt = datetime(2024, 7, 1, 19, 50, tzinfo=timezone.utc)  # 15:50 EDT
    assert eod_exit(dt, minutes_before_close=15) is True


def test_eod_exit_not_near_close():
    dt = datetime(2024, 7, 1, 17, 0, tzinfo=timezone.utc)  # 13:00 EDT
    assert eod_exit(dt, minutes_before_close=15) is False


def test_eod_exit_weekend():
    dt = datetime(2024, 7, 6, 19, 50, tzinfo=timezone.utc)  # Saturday
    assert eod_exit(dt, minutes_before_close=15) is False
