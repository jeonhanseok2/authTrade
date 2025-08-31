# strategy/exits.py
from __future__ import annotations
import math

def stop_loss_hit(entry_price: float, last_price: float, cfg) -> bool:
    sl = float(cfg.get("stop_loss_pct", 0.05))
    return last_price <= entry_price * (1 - sl)

def take_profit_hit(entry_price: float, last_price: float, cfg) -> bool:
    tp = float(cfg.get("take_profit_pct", 0.10))
    return last_price >= entry_price * (1 + tp)

def trailing_stop_active(entry_price: float, last_price: float, peak_price: float, cfg) -> bool:
    # +trail_after_profit_pct 도달 후 trailing_stop_pct 조건 이탈 시
    trail_after = float(cfg.get("trail_after_profit_pct", 0.10))
    trailing = float(cfg.get("trailing_stop_pct", 0.02))
    if peak_price >= entry_price * (1 + trail_after):
        return last_price <= peak_price * (1 - trailing)
    return False
