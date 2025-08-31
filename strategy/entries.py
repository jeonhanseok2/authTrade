# strategy/entries.py
from __future__ import annotations
import pandas as pd
from typing import Dict, Any

def momentum_entry(df: pd.DataFrame, rules: Dict[str, Any]) -> bool:
    """급등주: 최근 변동률 + 거래량 스파이크 기준 진입"""
    if df is None or df.empty or not {"close","volume"}.issubset(df.columns):
        return False
    look = int(rules.get("lookback_minutes", 120))
    df = df.tail(max(look, 30))
    if df.empty: return False

    first = float(df.iloc[0]["close"]); last = float(df.iloc[-1]["close"])
    if first <= 0: return False
    change_pct = (last - first) / first * 100.0
    min_change = float(rules.get("min_intraday_change_pct", 5.0))
    if change_pct < min_change: return False

    if len(df) >= 20:
        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    else:
        avg_vol = df["volume"].mean()
    vol_ok = bool(df["volume"].iloc[-1] >= float(rules.get("vol_spike_ratio", 2.0)) * (avg_vol or 1.0))
    if not vol_ok: return False

    # 저가 펌프 방지
    if last < float(rules.get("min_price_usd", 3.0)): return False
    return True

def value_entry(info: Dict[str, Any], rules: Dict[str, Any]) -> bool:
    """저평가 소형주: 시총/PE/EPS 성장/유동성 체크"""
    mcap = float(info.get("marketCap") or 0)
    if mcap <= 0 or mcap >= float(rules.get("max_market_cap_usd", 5e9)):
        return False
    pe = info.get("trailingPE")
    if not pe or pe <= 0: return False
    group_pe = float(info.get("groupPe") or pe*2)
    if pe >= float(rules.get("max_per_vs_group", 0.7)) * group_pe:
        return False
    epsg = float(info.get("epsGrowth") or 0.0)
    if epsg < float(rules.get("min_eps_growth", 0.10)):
        return False
    liq = float(info.get("avgDollarVolume") or 0.0)
    if liq < float(rules.get("min_liquidity_usd", 1_000_000)):
        return False
    return True
