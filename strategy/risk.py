from datetime import datetime, timezone

def within_trade_window(now: datetime, start_after_min: int, end_before_min: int) -> bool:
    open_utc = now.replace(hour=13, minute=30, second=0, microsecond=0)   # 9:30 ET (DST)
    close_utc = now.replace(hour=20, minute=0, second=0, microsecond=0)   # 16:00 ET (DST)
    if now < open_utc or now > close_utc:
        return False
    mins_since_open = (now - open_utc).total_seconds()/60.0
    mins_to_close = (close_utc - now).total_seconds()/60.0
    return mins_since_open >= start_after_min and mins_to_close >= end_before_min

def market_circuit_breaker_triggered(spy_df):
    if spy_df is None or spy_df.empty:
        return False
    first = float(spy_df.iloc[0]["close"]); last = float(spy_df.iloc[-1]["close"])
    drop_pct = (last - first)/first * 100.0
    return drop_pct <= -7.0
