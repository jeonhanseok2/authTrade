# strategy/regime.py
"""
시장 레짐 필터: VIX 과공포 구간 + 점심 데드존 진입 차단.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, time as dtime
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

# VIX 캐시 — 여러 루프(B1/B2/MONITOR)가 동시에 호출해도 1회만 실제 조회
_VIX_CACHE: dict = {}
_VIX_LOCK  = threading.Lock()
_VIX_TTL   = 60   # 60초 캐시


def fetch_vix(timeout: int = 8) -> float:
    """
    yfinance ^VIX 최신 종가 반환.
    동일 프로세스 내 중복 호출 방지: 60초 TTL 캐시 + threading.Lock.
    Alpaca는 VIX 거래 불가이므로 yfinance 사용.
    실패 시 0.0 (필터 비활성 효과).
    """
    now = _time.monotonic()

    # 빠른 경로: 락 없이 캐시 확인
    cached = _VIX_CACHE.get("entry")
    if cached and now - cached[0] < _VIX_TTL:
        return cached[1]

    # 느린 경로: 락 획득 후 이중 확인 (double-checked locking)
    with _VIX_LOCK:
        cached = _VIX_CACHE.get("entry")
        if cached and now - cached[0] < _VIX_TTL:
            return cached[1]

        val = 0.0
        try:
            import yfinance as yf
            t  = yf.Ticker("^VIX")
            df = t.history(period="1d", interval="1m", timeout=timeout)
            if df is not None and not df.empty:
                val = float(df["Close"].iloc[-1])
        except Exception as exc:
            logging.debug("[regime] VIX fetch failed: %s", exc)

        _VIX_CACHE["entry"] = (_time.monotonic(), val)
        return val


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
