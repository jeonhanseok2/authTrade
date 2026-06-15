from datetime import datetime
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")


def within_trade_window(now: datetime, start_after_min: int, end_before_min: int) -> bool:
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    open_et  = et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_et = et.replace(hour=16, minute=0,  second=0, microsecond=0)
    if et < open_et or et > close_et:
        return False
    mins_since_open = (et - open_et).total_seconds() / 60.0
    mins_to_close   = (close_et - et).total_seconds() / 60.0
    return mins_since_open >= start_after_min and mins_to_close >= end_before_min


def market_circuit_breaker_triggered(spy_df) -> bool:
    if spy_df is None or spy_df.empty:
        return False
    first = float(spy_df.iloc[0]["close"])
    last  = float(spy_df.iloc[-1]["close"])
    return (last - first) / first * 100.0 <= -7.0
