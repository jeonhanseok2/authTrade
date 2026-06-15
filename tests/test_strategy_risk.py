from datetime import datetime, timezone
from strategy.risk import within_trade_window, market_circuit_breaker_triggered
import pandas as pd


# EST (겨울): 2025-01-13 (월요일) — 뉴욕 개장 14:35 UTC = 9:35 EST (UTC-5)
def test_within_window_est():
    dt = datetime(2025, 1, 13, 14, 35, tzinfo=timezone.utc)  # 9:35 EST
    assert within_trade_window(dt, 5, 5) is True


# EDT (여름): 2024-07-01 — 뉴욕 개장 13:31 UTC
def test_within_window_edt():
    dt = datetime(2024, 7, 1, 13, 35, tzinfo=timezone.utc)  # 9:35 EDT
    assert within_trade_window(dt, 5, 5) is True


def test_before_open_rejected():
    dt = datetime(2024, 7, 1, 13, 20, tzinfo=timezone.utc)  # 9:20 EDT — before open
    assert within_trade_window(dt, 5, 5) is False


def test_weekend_rejected():
    dt = datetime(2024, 7, 6, 14, 0, tzinfo=timezone.utc)  # Saturday
    assert within_trade_window(dt, 5, 5) is False


def test_circuit_breaker_triggered():
    df = pd.DataFrame({"close": [100.0, 92.0]})
    assert market_circuit_breaker_triggered(df) is True


def test_circuit_breaker_not_triggered():
    df = pd.DataFrame({"close": [100.0, 97.0]})
    assert market_circuit_breaker_triggered(df) is False
