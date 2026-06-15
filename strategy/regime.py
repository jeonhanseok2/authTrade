# strategy/regime.py
"""
시장 레짐 필터: VIX 과공포 구간 + 점심 데드존 진입 차단.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")


def fetch_vix(timeout: int = 8) -> float:
    """
    yfinance ^VIX 최신 종가 반환.
    Alpaca는 VIX 거래 불가이므로 yfinance 사용.
    실패 시 0.0 (필터 비활성 효과).
    """
    try:
        import yfinance as yf
        t  = yf.Ticker("^VIX")
        df = t.history(period="1d", interval="1m", timeout=timeout)
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as exc:
        logging.debug("[regime] VIX fetch failed: %s", exc)
    return 0.0


def is_high_volatility(vix_value: float, threshold: float = 30.0) -> bool:
    """VIX >= threshold 이면 신규 진입 차단."""
    return vix_value >= threshold and vix_value > 0


def is_deadzone(
    now: datetime,
    start_hour: int = 11,
    start_min: int = 30,
    end_hour: int = 13,
    end_min: int = 0,
) -> bool:
    """
    11:30–13:00 ET 점심 데드존.
    거래량 감소 + 방향성 없는 구간이므로 신규 진입 자제.
    """
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    t = et.time()
    return dtime(start_hour, start_min) <= t <= dtime(end_hour, end_min)
