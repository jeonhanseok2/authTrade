# strategy/exits.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import pandas as pd
import zoneinfo

from strategy.signals import latest_rsi, compute_indicators

ET = zoneinfo.ZoneInfo("America/New_York")


def stop_loss_hit(entry_price: float, last_price: float, cfg: Dict[str, Any]) -> bool:
    sl = float(cfg.get("stop_loss_pct", 0.05))
    return last_price <= entry_price * (1.0 - sl)


def take_profit_hit(entry_price: float, last_price: float, cfg: Dict[str, Any]) -> bool:
    tp = float(cfg.get("take_profit_pct", 0.10))
    return last_price >= entry_price * (1.0 + tp)


def trailing_stop_active(
    entry_price: float,
    last_price: float,
    peak_price: float,
    cfg: Dict[str, Any],
) -> bool:
    trail_after = float(cfg.get("trail_after_profit_pct", 0.10))
    trailing    = float(cfg.get("trailing_stop_pct", 0.02))
    if peak_price >= entry_price * (1.0 + trail_after):
        return last_price <= peak_price * (1.0 - trailing)
    return False


def rsi_overbought_exit(df: pd.DataFrame, threshold: float = 80.0) -> bool:
    """RSI >= threshold 이면 모멘텀 소진 → 청산 신호."""
    df_ind = compute_indicators(df) if "rsi_14" not in df.columns else df
    return latest_rsi(df_ind) >= threshold


def eod_exit(now: datetime, minutes_before_close: int = 15) -> bool:
    """
    장 마감 N분 전에 True 반환 → 인트라데이 포지션 강제 청산.
    주말은 False.
    """
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    close_et = et.replace(hour=16, minute=0, second=0, microsecond=0)
    mins_to_close = (close_et - et).total_seconds() / 60.0
    return 0 <= mins_to_close <= minutes_before_close
