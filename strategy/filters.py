import pandas as pd
from typing import List, Dict, Any

def filter_value_smallcap(info_list: List[Dict[str, Any]], max_mcap: float, max_per_vs_group: float, min_eps_growth: float) -> List[str]:
    pes = [it.get("trailingPE") for it in info_list if it.get("trailingPE")]
    group_pe = float(pd.Series(pes).median()) if pes else None
    out = []
    for it in info_list:
        mcap = it.get("marketCap")
        pe = it.get("trailingPE")
        epsg = it.get("epsGrowth", 0.0)
        if not mcap or mcap >= max_mcap or not pe or pe <= 0:
            continue
        ref = group_pe if group_pe else pe*2
        if pe < max_per_vs_group * ref and epsg >= min_eps_growth:
            out.append(it["symbol"])
    return out
