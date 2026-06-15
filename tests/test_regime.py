from datetime import datetime, timezone
from strategy.regime import is_deadzone, is_high_volatility


def test_deadzone_at_noon():
    dt = datetime(2024, 7, 1, 16, 30, tzinfo=timezone.utc)  # 12:30 EDT
    assert is_deadzone(dt) is True


def test_not_deadzone_morning():
    dt = datetime(2024, 7, 1, 14, 0, tzinfo=timezone.utc)  # 10:00 EDT
    assert is_deadzone(dt) is False


def test_not_deadzone_weekend():
    dt = datetime(2024, 7, 6, 16, 30, tzinfo=timezone.utc)  # Saturday 12:30 EDT
    assert is_deadzone(dt) is False


def test_high_vix():
    assert is_high_volatility(35.0, 30.0) is True


def test_low_vix():
    assert is_high_volatility(20.0, 30.0) is False


def test_zero_vix_not_high():
    assert is_high_volatility(0.0, 30.0) is False
