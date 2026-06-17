# data/alpaca_bars.py
"""
Alpaca Historical Data API OHLCV 조회 유틸리티.

yfinance 대체 모듈 (지수/펀더멘털 데이터 제외).
환경변수(ALPACA_API_KEY / ALPACA_SECRET_KEY)로 싱글턴 클라이언트를 생성하여
여러 모듈에서 공유합니다.

사용:
    from data.alpaca_bars import fetch_bars
    df = fetch_bars("NVDA", "1Day", 60)   # 일봉 60개
    df = fetch_bars("QQQ",  "1Min", 390)  # 1분봉 1거래일
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import pandas as pd

_CLIENT      = None
_CLIENT_LOCK = threading.Lock()
# B1·B2·B3·뉴스 동시 호출 시 in-flight HTTP 요청 상한 (Alpaca free: 200 req/min)
_SEMAPHORE   = threading.Semaphore(10)


def _get_client():
    """싱글턴 StockHistoricalDataClient — 이중 확인 잠금으로 스레드 안전 초기화."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from alpaca.data.historical import StockHistoricalDataClient
            _CLIENT = StockHistoricalDataClient(
                os.getenv("ALPACA_API_KEY", ""),
                os.getenv("ALPACA_SECRET_KEY", ""),
            )
    return _CLIENT


def fetch_bars(
    symbol:    str,
    timeframe: str = "1Day",   # "1Min" | "5Min" | "1Day" | "1Week"
    limit:     int = 60,
) -> Optional[pd.DataFrame]:
    """
    Alpaca Historical Data API로 OHLCV 봉 조회.

    Args:
        symbol:    종목 코드 (예: "NVDA", "QQQ")
        timeframe: "1Min" | "5Min" | "1Day" | "1Week"
        limit:     조회할 최대 봉 수

    Returns:
        columns=[open, high, low, close, volume] DataFrame,
        실패 시 None (호출부에서 graceful 처리).
    """
    try:
        from alpaca.data.requests  import StockBarsRequest   # type: ignore
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore

        tf_map = {
            "1Min":  TimeFrame(1, TimeFrameUnit.Minute),
            "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
            "1Day":  TimeFrame(1, TimeFrameUnit.Day),
            "1Week": TimeFrame(1, TimeFrameUnit.Week),
        }
        tf  = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, limit=limit)

        with _SEMAPHORE:
            resp = _get_client().get_stock_bars(req)

        # .df: alpaca-py v0.35+에서 DataFrame 반환 (비어 있을 수 있음)
        bars = getattr(resp, "df", None)
        if bars is None or (hasattr(bars, "empty") and bars.empty):
            # fallback: dict-like 접근 (.get()은 미존재 — __getitem__ 사용)
            try:
                bar_list = resp[symbol]
            except (KeyError, TypeError, AttributeError):
                return None
            if not bar_list:
                return None
            rows = [
                {"open": b.open, "high": b.high, "low": b.low,
                 "close": b.close, "volume": b.volume}
                for b in bar_list
            ]
            return pd.DataFrame(rows) if rows else None

        if hasattr(bars, "reset_index"):
            bars = bars.reset_index(drop=True)

        bars.columns = [str(c).lower() for c in bars.columns]
        needed = [c for c in ("open", "high", "low", "close", "volume") if c in bars.columns]
        return bars[needed] if needed else None

    except Exception as exc:
        logging.debug("[alpaca_bars] %s/%s/%d 실패: %s", symbol, timeframe, limit, exc)
        return None
