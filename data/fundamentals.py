# data/fundamentals.py
from __future__ import annotations
from typing import Dict, Any, List
import yfinance as yf
import pandas as pd

def fetch_quick_fundamentals(symbols: List[str]) -> List[Dict[str, Any]]:
    out = []
    for s in symbols:
        try:
            t = yf.Ticker(s)
            finfo = getattr(t, "fast_info", None)
            pe = float(getattr(finfo, "trailing_pe", 0.0) or 0.0)
            mcap = float(getattr(finfo, "market_cap", 0.0) or 0.0)
            # 대략적 유동성(최근 20일 평균 거래대금) 추정
            hist = t.history(period="1mo", interval="1d", actions=False, auto_adjust=False)
            if hist is not None and not hist.empty:
                avg_dollar = float((hist["Close"] * hist["Volume"]).rolling(20).mean().iloc[-1])
            else:
                avg_dollar = 0.0
            # EPS 성장률은 간이치 (정확도 향상은 재무 API로 보완)
            epsg = 0.12
            out.append({"symbol": s, "trailingPE": pe, "marketCap": mcap, "avgDollarVolume": avg_dollar, "epsGrowth": epsg})
        except Exception:
            continue
    # 그룹 중앙 PER 계산해 넣기
    pes = [x["trailingPE"] for x in out if x["trailingPE"]>0]
    group_pe = float(pd.Series(pes).median()) if pes else None
    if group_pe:
        for x in out:
            x["groupPe"] = group_pe
    return out
