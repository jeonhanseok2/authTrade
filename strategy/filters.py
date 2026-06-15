import pandas as pd
from typing import Any, Dict, List


def filter_value_smallcap(
    info_list: List[Dict[str, Any]],
    max_mcap: float,
    max_per_vs_group: float,
    min_eps_growth: float,
) -> List[str]:
    pes = [it.get("trailingPE") for it in info_list if it.get("trailingPE")]
    group_pe = float(pd.Series(pes).median()) if pes else None
    out = []
    for it in info_list:
        mcap = it.get("marketCap")
        pe   = it.get("trailingPE")
        epsg = it.get("epsGrowth", 0.0)
        if not mcap or mcap >= max_mcap or not pe or pe <= 0:
            continue
        ref = group_pe if group_pe else pe * 2
        if pe < max_per_vs_group * ref and epsg >= min_eps_growth:
            out.append(it["symbol"])
    return out


def sector_concentration_ok(
    symbol_sector: str,
    open_position_sectors: Dict[str, int],
    max_per_sector: int = 3,
) -> bool:
    """
    신규 진입 시 섹터 집중도 검사.
    섹터 정보가 없으면 (빈 문자열) 항상 통과.
    """
    if not symbol_sector:
        return True
    current_count = open_position_sectors.get(symbol_sector, 0)
    return current_count < max_per_sector
