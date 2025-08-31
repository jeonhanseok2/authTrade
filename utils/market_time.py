# utils/market_time.py
from datetime import datetime, time as dtime
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

def is_us_trading_session(now_utc: datetime) -> bool:
    et = now_utc.astimezone(ET)
    if et.weekday() >= 5:   # 토(5)/일(6)
        return False
    t = et.time()
    # 서머타임 기준 09:30–16:00 (필요하면 교환소 캘린더로 고도화)
    return dtime(9,30) <= t <= dtime(16,0)
