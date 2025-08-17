import math, pandas as pd
from typing import Dict, List

def top_momentum(symbol_dfs: Dict[str, pd.DataFrame], min_change_pct: float, vol_spike_ratio: float, limit: int=3) -> List[str]:
    scored = []
    for sym, df in symbol_dfs.items():
        if df is None or df.empty: 
            continue
        df = df.tail(120)
        if df.empty or not {"close","volume"}.issubset(df.columns): 
            continue
        first = float(df.iloc[0]["close"]); last  = float(df.iloc[-1]["close"])
        change_pct = (last - first) / (first + 1e-9) * 100.0
        avg_vol = df["volume"].rolling(20).mean().iloc[-1] if len(df)>=20 else df["volume"].mean()
        vol_ok = bool(df["volume"].iloc[-1] >= vol_spike_ratio * (avg_vol or 1.0))
        if change_pct >= min_change_pct and vol_ok:
            score = change_pct * math.log10( (df["volume"].iloc[-1] or 1.0) + 10 )
            scored.append((sym, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in scored[:limit]]
