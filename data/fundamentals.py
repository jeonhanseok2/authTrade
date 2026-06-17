# data/fundamentals.py
from __future__ import annotations
import time
import threading
from typing import Dict, Any, List

import yfinance as yf
import pandas as pd

# 캐시: {symbol: (fetch_ts, data_dict)}
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 4 * 3600  # 4시간
_YF_TIMEOUT = 20.0     # yfinance 단일 종목 최대 대기 시간 (hang 방지)


def _compute_eps_growth(ticker: yf.Ticker) -> float:
    """
    yfinance 연간 손익계산서에서 BasicEPS 최근 2개 연도를 비교해 성장률 반환.
    데이터 없으면 0.0 반환.
    """
    try:
        stmt = ticker.get_income_stmt(freq="yearly")
        if stmt is None or stmt.empty:
            return 0.0
        for row_name in ("BasicEPS", "DilutedEPS", "Basic EPS", "Diluted EPS"):
            if row_name in stmt.index:
                row = stmt.loc[row_name].dropna()
                if len(row) >= 2:
                    eps_latest = float(row.iloc[0])
                    eps_prior  = float(row.iloc[1])
                    if eps_prior != 0 and eps_prior > 0:
                        return (eps_latest - eps_prior) / abs(eps_prior)
    except Exception:
        pass
    return 0.0


def _get_sector(ticker: yf.Ticker) -> str:
    try:
        return str(ticker.info.get("sector") or "")
    except Exception:
        return ""


def _fetch_one_raw(symbol: str) -> Dict[str, Any] | None:
    """실제 yfinance 조회 — _fetch_one 에서 타임아웃 래퍼로 호출."""
    now = time.time()
    t = yf.Ticker(symbol)
    finfo = getattr(t, "fast_info", None)
    pe    = float(getattr(finfo, "trailing_pe",  0.0) or 0.0)
    mcap  = float(getattr(finfo, "market_cap",   0.0) or 0.0)

    hist = t.history(period="1mo", interval="1d", actions=False, auto_adjust=False)
    avg_dollar = (
        float((hist["Close"] * hist["Volume"]).rolling(20).mean().iloc[-1])
        if hist is not None and not hist.empty
        else 0.0
    )

    epsg   = _compute_eps_growth(t)
    sector = _get_sector(t)

    result: Dict[str, Any] = {
        "symbol":        symbol,
        "trailingPE":    pe,
        "marketCap":     mcap,
        "avgDollarVolume": avg_dollar,
        "epsGrowth":     epsg,
        "sector":        sector,
    }
    _CACHE[symbol] = (now, result)
    return result


def _fetch_one(symbol: str) -> Dict[str, Any] | None:
    now = time.time()
    cached = _CACHE.get(symbol)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    result_holder: list = [None]
    exc_holder:    list = [None]

    def _worker():
        try:
            result_holder[0] = _fetch_one_raw(symbol)
        except Exception as e:
            exc_holder[0] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=_YF_TIMEOUT)

    if t.is_alive():
        # 스레드가 아직 실행 중 = yfinance hang → None 반환 (캐시 없음)
        import logging as _log
        _log.warning("[fundamentals] %s yfinance 타임아웃 (%ss) — 스킵", symbol, _YF_TIMEOUT)
        return None

    return result_holder[0]


def fetch_quick_fundamentals(symbols: List[str]) -> List[Dict[str, Any]]:
    out = []
    for s in symbols:
        item = _fetch_one(s)
        if item:
            out.append(item)

    # 그룹 중앙 PER 계산
    pes = [x["trailingPE"] for x in out if x["trailingPE"] > 0]
    group_pe = float(pd.Series(pes).median()) if pes else None
    if group_pe:
        for x in out:
            x["groupPe"] = group_pe

    return out
