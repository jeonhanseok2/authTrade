# backtest/engine.py
"""
단순 바-단위 이벤트 드리븐 백테스트.
동일한 strategy/ 함수들을 재사용하므로 전략 코드와 백테스트가 일치.
주의: 체결가 = close (슬리피지 미반영 v1)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.entries import momentum_entry
from strategy.exits import (
    eod_exit,
    rsi_overbought_exit,
    stop_loss_hit,
    take_profit_hit,
    trailing_stop_active,
)
from strategy.signals import compute_indicators


@dataclass
class BacktestTrade:
    symbol:      str
    strategy:    str
    entry_ts:    pd.Timestamp
    entry_price: float
    qty:         int
    exit_ts:     Optional[pd.Timestamp] = None
    exit_price:  Optional[float] = None
    exit_reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100.0 if self.exit_price else 0.0


@dataclass
class _OpenPos:
    trade: BacktestTrade
    peak_price: float


def run_backtest(
    symbol_dfs: Dict[str, pd.DataFrame],
    mom_cfg: dict,
    risk_cfg: dict,
    initial_cash: float = 100_000.0,
    lookback_bars: int = 30,
    max_positions: int = 3,
    risk_per_trade_pct: float = 0.01,
    atr_multiplier: float = 2.0,
) -> dict:
    """
    Args:
        symbol_dfs: {symbol: df} — df는 [open, high, low, close, volume] 컬럼,
                    DatetimeIndex 오름차순.
        mom_cfg:    config.yaml momentum_rules 딕셔너리.
        risk_cfg:   config.yaml risk 딕셔너리.
        initial_cash: 초기 자본 (USD).
        lookback_bars: 전략 평가 시작 전 최소 바 수.
        max_positions: 동시 보유 최대 종목 수.
        risk_per_trade_pct: 거래당 리스크 비율.
        atr_multiplier: ATR 기반 손절 배수.

    Returns:
        dict with keys: trades, total_pnl, win_rate, max_drawdown_pct,
                        sharpe_approx, summary_df, equity_curve
    """
    cash      = initial_cash
    open_pos: Dict[str, _OpenPos] = {}
    all_trades: List[BacktestTrade] = []
    equity_history: List[float] = []

    # 모든 심볼의 타임스탬프 합집합으로 공통 시계열 구성
    all_ts = sorted(
        set(ts for df in symbol_dfs.values() if df is not None for ts in df.index)
    )

    for i, ts in enumerate(all_ts):
        # ── 기존 포지션 종료 검사 ─────────────────────────────────
        for sym in list(open_pos.keys()):
            df = symbol_dfs.get(sym)
            if df is None or ts not in df.index:
                continue

            pos  = open_pos[sym]
            last = float(df.loc[ts, "close"])
            pos.peak_price = max(pos.peak_price, last)

            entry = pos.trade.entry_price
            peak  = pos.peak_price

            reason = ""
            if stop_loss_hit(entry, last, risk_cfg):
                reason = "stop_loss"
            elif take_profit_hit(entry, last, risk_cfg) and trailing_stop_active(entry, last, peak, risk_cfg):
                reason = "trailing_stop"
            elif rsi_overbought_exit(df.loc[:ts], float(risk_cfg.get("rsi_overbought_exit", 80.0))):
                reason = "rsi_overbought"
            elif eod_exit(ts.to_pydatetime(), int(risk_cfg.get("eod_exit_minutes_before_close", 15))):
                reason = "eod"

            if reason:
                t = pos.trade
                t.exit_ts    = ts
                t.exit_price = last
                t.exit_reason = reason
                cash += last * t.qty
                all_trades.append(t)
                del open_pos[sym]

        # ── 신규 진입 검사 ────────────────────────────────────────
        if len(open_pos) < max_positions:
            for sym, df in symbol_dfs.items():
                if sym in open_pos or df is None:
                    continue
                idx = df.index.get_loc(ts) if ts in df.index else -1
                if idx < lookback_bars:
                    continue
                slice_df = df.iloc[: idx + 1]
                if not momentum_entry(slice_df, mom_cfg):
                    continue

                price  = float(df.loc[ts, "close"])
                df_ind = compute_indicators(slice_df)
                from strategy.signals import atr_for_sizing
                from strategy.sizing import atr_position_size, budget_cap_size
                atr   = atr_for_sizing(df_ind)
                equity = cash + sum(
                    p.trade.qty * symbol_dfs[s].loc[ts, "close"]
                    for s, p in open_pos.items()
                    if ts in symbol_dfs[s].index
                )
                qty = atr_position_size(atr, equity, risk_per_trade_pct, price, atr_multiplier)
                if qty == 0:
                    qty = budget_cap_size(equity * risk_per_trade_pct * 10, price)
                if qty <= 0 or price * qty > cash:
                    continue

                trade = BacktestTrade(
                    symbol=sym, strategy="momentum",
                    entry_ts=ts, entry_price=price, qty=qty,
                )
                cash -= price * qty
                open_pos[sym] = _OpenPos(trade=trade, peak_price=price)

                if len(open_pos) >= max_positions:
                    break

        # ── 자산 평가 기록 ────────────────────────────────────────
        unrealized = sum(
            p.trade.qty * symbol_dfs[s].loc[ts, "close"]
            for s, p in open_pos.items()
            if ts in symbol_dfs[s].index
        )
        equity_history.append(cash + unrealized)

    # ── 미청산 포지션 강제 종료 (마지막 봉) ───────────────────────
    if all_ts:
        last_ts = all_ts[-1]
        for sym, pos in list(open_pos.items()):
            df = symbol_dfs.get(sym)
            if df is None:
                continue
            last_bar = df.index[-1]
            price = float(df.loc[last_bar, "close"])
            t = pos.trade
            t.exit_ts    = last_bar
            t.exit_price = price
            t.exit_reason = "end_of_data"
            cash += price * t.qty
            all_trades.append(t)

    # ── 통계 계산 ─────────────────────────────────────────────────
    closed = [t for t in all_trades if t.exit_price is not None]
    total_pnl = sum(t.pnl for t in closed)

    wins    = [t for t in closed if t.pnl > 0]
    win_rate = len(wins) / len(closed) if closed else 0.0

    eq = np.array(equity_history, dtype=float)
    if len(eq) > 1:
        peak       = np.maximum.accumulate(eq)
        drawdown   = (eq - peak) / np.where(peak > 0, peak, 1.0) * 100.0
        max_dd_pct = float(drawdown.min())
        daily_ret  = np.diff(eq) / np.where(eq[:-1] > 0, eq[:-1], 1.0)
        sharpe     = float(np.mean(daily_ret) / (np.std(daily_ret) + 1e-9) * math.sqrt(252))
    else:
        max_dd_pct = 0.0
        sharpe     = 0.0

    summary_rows = [
        {
            "symbol":      t.symbol,
            "entry_ts":    t.entry_ts,
            "exit_ts":     t.exit_ts,
            "entry_price": t.entry_price,
            "exit_price":  t.exit_price,
            "qty":         t.qty,
            "pnl":         t.pnl,
            "pnl_pct":     t.pnl_pct,
            "exit_reason": t.exit_reason,
        }
        for t in closed
    ]

    return {
        "trades":           closed,
        "total_pnl":        total_pnl,
        "win_rate":         win_rate,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_approx":    sharpe,
        "summary_df":       pd.DataFrame(summary_rows),
        "equity_curve":     pd.Series(equity_history, index=all_ts[: len(equity_history)]),
    }
