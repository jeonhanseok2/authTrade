# backtest/report.py
from __future__ import annotations

import os
from typing import Any, Dict


def print_report(result: Dict[str, Any]) -> None:
    """백테스트 결과 요약 출력."""
    trades = result.get("trades", [])
    print("\n" + "=" * 55)
    print("  BACKTEST REPORT")
    print("=" * 55)
    print(f"  총 거래 수        : {len(trades)}")
    print(f"  총 손익           : ${result.get('total_pnl', 0):.2f}")
    print(f"  승률              : {result.get('win_rate', 0) * 100:.1f}%")
    print(f"  최대 낙폭         : {result.get('max_drawdown_pct', 0):.2f}%")
    print(f"  샤프 지수(근사)   : {result.get('sharpe_approx', 0):.2f}")
    print("-" * 55)

    df = result.get("summary_df")
    if df is not None and not df.empty:
        print("\n  [상위 5개 거래]")
        top = df.nlargest(5, "pnl")[["symbol", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]]
        print(top.to_string(index=False))
        print("\n  [하위 5개 거래]")
        bot = df.nsmallest(5, "pnl")[["symbol", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]]
        print(bot.to_string(index=False))

    print("=" * 55 + "\n")


def to_csv(result: Dict[str, Any], path: str = "backtest_trades.csv") -> None:
    """거래 로그를 CSV로 저장."""
    df = result.get("summary_df")
    if df is None or df.empty:
        print("[report] 거래 없음 — CSV 미저장")
        return
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[report] 저장 완료: {os.path.abspath(path)}")
