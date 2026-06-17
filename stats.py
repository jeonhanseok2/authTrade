#!/usr/bin/env python3
"""
승률/수익 통계 분석기.

사용법:
    python stats.py                         # 전체 요약
    python stats.py --bucket squeeze        # B3만
    python stats.py --days 30               # 최근 30일
    python stats.py --bucket etf_swing --days 14
    python stats.py --csv results/stats.csv # CSV 저장
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("ERROR: pip install pandas 필요")
    sys.exit(1)


DB_PATH = "storage/trade.db"

STRATEGY_LABEL = {
    "squeeze":   "B3 급등스퀴즈",
    "etf_swing": "B2 ETF스윙",
    "value_long":"B1 가치주",
}

# ── 핵심 지표 계산 ────────────────────────────────────────────────────

def profit_factor(wins: pd.Series, losses: pd.Series) -> float:
    """Profit Factor = 총 수익 / |총 손실|. 1.5+ 이면 우위 전략."""
    total_win  = wins[wins > 0].sum()
    total_loss = abs(losses[losses < 0].sum())
    return total_win / total_loss if total_loss > 0 else float("inf")


def expectancy_per_trade(df: pd.DataFrame) -> float:
    """기대값 = (승률 × 평균수익) - (패률 × 평균손실). 양수여야 수익 전략."""
    wins   = df[df["pnl"] > 0]["pnl"]
    losses = df[df["pnl"] < 0]["pnl"]
    win_rate  = len(wins) / len(df) if len(df) > 0 else 0
    avg_win   = wins.mean()  if len(wins)   > 0 else 0
    avg_loss  = losses.mean() if len(losses) > 0 else 0
    return win_rate * avg_win + (1 - win_rate) * avg_loss


def bucket_report(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {}

    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]

    win_rate   = len(wins) / len(df) * 100
    pf         = profit_factor(df["pnl"], df["pnl"])
    expectancy = expectancy_per_trade(df)
    avg_hold   = df["hold_minutes"].mean() if "hold_minutes" in df.columns else 0

    return {
        "label":       label,
        "trades":      len(df),
        "win_rate":    round(win_rate, 1),
        "avg_win_pct": round(wins["pnl_pct"].mean(), 2)   if len(wins)   > 0 else 0,
        "avg_loss_pct":round(losses["pnl_pct"].mean(), 2) if len(losses) > 0 else 0,
        "profit_factor": round(pf, 2),
        "expectancy":  round(expectancy, 2),
        "total_pnl":   round(df["pnl"].sum(), 2),
        "max_win":     round(wins["pnl_pct"].max(), 2)    if len(wins)   > 0 else 0,
        "max_loss":    round(losses["pnl_pct"].min(), 2)  if len(losses) > 0 else 0,
        "avg_hold_min":round(avg_hold, 0),
    }


# ── 보고서 출력 ───────────────────────────────────────────────────────

def print_bucket(r: dict) -> None:
    if not r:
        print("  (거래 없음)")
        return

    wv = r["win_rate"]
    pf = r["profit_factor"]
    ex = r["expectancy"]

    # 신호등: 승률/PF/기대값 기준
    if wv >= 55 and pf >= 1.5 and ex > 0:
        sig = "🟢 수익 전략"
    elif wv >= 45 and pf >= 1.2 and ex > 0:
        sig = "🟡 보통 (최적화 필요)"
    else:
        sig = "🔴 개선 필요"

    print(f"  {'─'*48}")
    print(f"  {r['label']} ({r['trades']}건)   {sig}")
    print(f"  {'─'*48}")
    print(f"  승률          : {wv:.1f}%  (목표 55%+)")
    print(f"  평균 수익     : +{r['avg_win_pct']:.2f}%   최대 +{r['max_win']:.2f}%")
    print(f"  평균 손실     : {r['avg_loss_pct']:.2f}%   최대 {r['max_loss']:.2f}%")
    print(f"  Profit Factor : {pf:.2f}  (1.5+ 권장)")
    print(f"  거래당 기대값 : ${ex:.2f}")
    print(f"  누적 손익     : ${r['total_pnl']:+,.2f}")
    print(f"  평균 보유     : {int(r['avg_hold_min'])}분")


def print_daily(df: pd.DataFrame) -> None:
    if df.empty or "date" not in df.columns:
        return
    daily = (
        df.groupby("date")["pnl"]
        .agg(["sum", "count"])
        .rename(columns={"sum": "pnl", "count": "trades"})
        .sort_index()
    )
    pos_days = (daily["pnl"] > 0).sum()
    neg_days = (daily["pnl"] < 0).sum()
    day_win  = pos_days / len(daily) * 100 if len(daily) > 0 else 0

    print(f"\n  [일별 승패]")
    print(f"  수익 날 {pos_days}일  손실 날 {neg_days}일  일간 승률 {day_win:.0f}%")
    print(f"  평균 일 손익: ${daily['pnl'].mean():+.2f}")
    print(f"  최고 하루   : ${daily['pnl'].max():+.2f}")
    print(f"  최악 하루   : ${daily['pnl'].min():+.2f}")

    print(f"\n  [최근 10거래일]")
    for date, row in daily.tail(10).iterrows():
        bar = "█" * min(int(abs(row["pnl"]) / 10), 20)
        sign = "+" if row["pnl"] >= 0 else ""
        print(f"  {date}  {sign}{row['pnl']:>7.2f}  {row['trades']}건  {bar}")


def verdict(total_trades: int, win_rate: float, pf: float) -> str:
    if total_trades < 30:
        needed = 30 - total_trades
        return f"⚠️  데이터 부족 ({total_trades}건) — {needed}건 더 필요해야 유효한 승률"
    if win_rate >= 55 and pf >= 1.5:
        return "✅ 실전 전환 검토 가능"
    elif win_rate >= 45 and pf >= 1.2:
        return "🔧 파라미터 조정 후 재검증 권장"
    else:
        return "🛑 전략 수정 필요 — 실전 전환 보류"


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="페이퍼 트레이딩 승률 분석")
    parser.add_argument("--bucket", choices=["squeeze", "etf_swing", "value_long"],
                        default=None, help="버킷 필터 (미지정=전체)")
    parser.add_argument("--days",   type=int, default=None,
                        help="최근 N일만 분석")
    parser.add_argument("--db",     default=DB_PATH, help="DB 경로")
    parser.add_argument("--csv",    default=None,    help="거래 내역 CSV 저장 경로")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB 없음 → {db_path}")
        print("       먼저 페이퍼 트레이딩을 실행하세요: python main.py")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # closed_trades 테이블 확인
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "closed_trades" in tables:
        df = pd.read_sql("SELECT * FROM closed_trades ORDER BY exit_ts", conn)
    elif "trades" in tables:
        # trades 테이블에서 buy/sell 매칭으로 구성
        df = _build_from_trades(conn)
    else:
        print("ERROR: 거래 데이터 없음 (아직 청산된 거래가 없습니다)")
        sys.exit(0)

    conn.close()

    if df.empty:
        print("\n거래 데이터 없음 — 페이퍼 트레이딩 후 다시 실행하세요.")
        sys.exit(0)

    # 날짜 필터
    if args.days and "date" in df.columns:
        cutoff = (pd.Timestamp.now() - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")
        df = df[df["date"] >= cutoff]

    # 버킷 필터
    if args.bucket and "strategy" in df.columns:
        df = df[df["strategy"] == args.bucket]

    print(f"\n{'='*52}")
    print(f"  승률 분석 리포트")
    if args.days:
        print(f"  기간: 최근 {args.days}일")
    if args.bucket:
        print(f"  버킷: {STRATEGY_LABEL.get(args.bucket, args.bucket)}")
    print(f"{'='*52}")

    if "strategy" in df.columns:
        for strat, label in STRATEGY_LABEL.items():
            sub = df[df["strategy"] == strat] if not args.bucket else df
            if not sub.empty:
                r = bucket_report(sub, label)
                print_bucket(r)
                if args.bucket:
                    break  # 특정 버킷이면 한 번만
    else:
        r = bucket_report(df, "전체")
        print_bucket(r)

    # 전체 합산
    print(f"\n  {'─'*48}")
    total_pnl  = df["pnl"].sum() if "pnl" in df.columns else 0
    total_cnt  = len(df)
    total_wins = (df["pnl"] > 0).sum() if "pnl" in df.columns else 0
    overall_wr = total_wins / total_cnt * 100 if total_cnt > 0 else 0
    overall_pf = profit_factor(df["pnl"], df["pnl"]) if "pnl" in df.columns else 0

    print(f"  전체 {total_cnt}건  승률 {overall_wr:.1f}%  PF {overall_pf:.2f}  누적 ${total_pnl:+,.2f}")
    print(f"\n  {verdict(total_cnt, overall_wr, overall_pf)}")
    print(f"{'='*52}")

    print_daily(df)

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n  CSV 저장: {args.csv}")


def _build_from_trades(conn) -> pd.DataFrame:
    """trades 테이블에서 buy/sell 쌍을 매칭해 closed_trades 구조로 변환."""
    rows = pd.read_sql(
        "SELECT * FROM trades ORDER BY symbol, ts", conn
    )
    if rows.empty:
        return pd.DataFrame()

    results = []
    for sym, grp in rows.groupby("symbol"):
        buys  = grp[grp["side"] == "buy"].to_dict("records")
        sells = grp[grp["side"] == "sell"].to_dict("records")
        for b, s in zip(buys, sells):
            pnl     = (s["price"] - b["price"]) * min(b["qty"], s["qty"])
            pnl_pct = (s["price"] - b["price"]) / b["price"] * 100 if b["price"] > 0 else 0
            results.append({
                "symbol":      sym,
                "strategy":    b.get("strategy", ""),
                "entry_price": b["price"],
                "exit_price":  s["price"],
                "qty":         min(b["qty"], s["qty"]),
                "entry_ts":    b["ts"],
                "exit_ts":     s["ts"],
                "pnl":         pnl,
                "pnl_pct":     pnl_pct,
                "exit_reason": s.get("reason", ""),
                "date":        b["ts"][:10],
                "hold_minutes":0,
            })
    return pd.DataFrame(results)


if __name__ == "__main__":
    main()
