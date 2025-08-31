# data/assets.py
# -*- coding: utf-8 -*-
import os
import csv
from typing import List, Dict

ASSET_CACHE: List[Dict] = []
CACHE_LOADED = False

def load_assets_from_csv(path: str) -> List[Dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append({
                "symbol": row.get("symbol","").upper(),
                "name": row.get("name",""),
                "exchange": row.get("exchange",""),
                "tradable": row.get("tradable","true").lower()=="true",
                "fractionable": row.get("fractionable","false").lower()=="true",
            })
    return rows

def fetch_assets_via_alpaca(alpaca_trading_client) -> List[Dict]:
    """
    Alpaca TradingClient.get_all_assets() 사용 (alpaca-py).
    키가 없거나 실패하면 빈 리스트.
    """
    try:
        assets = alpaca_trading_client.get_all_assets()  # active 기본
        out = []
        for a in assets:
            # pydantic 모델 호환
            d = a.model_dump() if hasattr(a, "model_dump") else (a.dict() if hasattr(a,"dict") else a.__dict__)
            out.append({
                "symbol": d.get("symbol","").upper(),
                "name": d.get("name") or "",
                "exchange": d.get("exchange") or d.get("exchange_short") or "",
                "tradable": bool(d.get("tradable", True)),
                "fractionable": bool(d.get("fractionable", False)),
            })
        return out
    except Exception:
        return []

def ensure_asset_cache(trading_client=None, csv_fallback="data/assets_us_equities.csv"):
    global CACHE_LOADED, ASSET_CACHE
    if CACHE_LOADED:
        return
    # 1) Alpaca로 시도
    if trading_client:
        ASSET_CACHE = fetch_assets_via_alpaca(trading_client)
    # 2) 실패 시 CSV 폴백 (직접 만들어두기)
    if not ASSET_CACHE:
        ASSET_CACHE = load_assets_from_csv(csv_fallback)
    CACHE_LOADED = True

def search_assets(query: str, limit: int = 20) -> List[Dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    res = []
    for a in ASSET_CACHE:
        s = a.get("symbol","").lower()
        n = (a.get("name") or "").lower()
        ex = (a.get("exchange") or "").lower()
        if q in s or q in n or q in ex:
            res.append(a)
        if len(res) >= limit:
            break
    return res
