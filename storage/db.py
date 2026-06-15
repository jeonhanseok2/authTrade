# storage/db.py
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    strategy    TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts    TEXT NOT NULL,
    peak_price  REAL NOT NULL,
    qty         INTEGER NOT NULL,
    sector      TEXT DEFAULT '',
    status      TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    price       REAL NOT NULL,
    strategy    TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date        TEXT PRIMARY KEY,
    realized    REAL DEFAULT 0.0,
    unrealized  REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0
);

-- 청산 완료 거래 (매수-매도 쌍 매칭 + PnL 계산)
CREATE TABLE IF NOT EXISTS closed_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    sector        TEXT DEFAULT '',
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    qty           INTEGER NOT NULL,
    entry_ts      TEXT NOT NULL,
    exit_ts       TEXT NOT NULL,
    hold_minutes  INTEGER DEFAULT 0,
    pnl           REAL DEFAULT 0.0,    -- 손익 ($)
    pnl_pct       REAL DEFAULT 0.0,    -- 손익률 (%)
    exit_reason   TEXT DEFAULT '',
    date          TEXT NOT NULL        -- YYYY-MM-DD (ET 기준)
);

-- 일일 매매 일지
CREATE TABLE IF NOT EXISTS daily_journal (
    date          TEXT PRIMARY KEY,    -- YYYY-MM-DD
    trades_cnt    INTEGER DEFAULT 0,
    win_cnt       INTEGER DEFAULT 0,
    lose_cnt      INTEGER DEFAULT 0,
    realized_pnl  REAL DEFAULT 0.0,
    win_rate      REAL DEFAULT 0.0,
    avg_win_pct   REAL DEFAULT 0.0,
    avg_loss_pct  REAL DEFAULT 0.0,
    profit_factor REAL DEFAULT 0.0,
    best_trade    TEXT DEFAULT '',     -- JSON
    worst_trade   TEXT DEFAULT '',     -- JSON
    bucket_stats  TEXT DEFAULT '{}',   -- JSON: 버킷별 통계
    ai_analysis   TEXT DEFAULT '',     -- Gemini Pro 분석
    created_at    TEXT NOT NULL
);

-- 주간 분석
CREATE TABLE IF NOT EXISTS weekly_analysis (
    week_start      TEXT PRIMARY KEY,  -- 해당 주 월요일 (YYYY-MM-DD)
    total_trades    INTEGER DEFAULT 0,
    win_rate        REAL DEFAULT 0.0,
    total_pnl       REAL DEFAULT 0.0,
    max_drawdown_pct REAL DEFAULT 0.0,
    best_strategy   TEXT DEFAULT '',
    worst_setup     TEXT DEFAULT '',
    ai_analysis     TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
"""


class PositionDB:
    def __init__(self, path: str = "storage/trade.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 포지션 관리 ────────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        strategy: str,
        entry_price: float,
        qty: int,
        sector: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, strategy, entry_price, entry_ts, peak_price, qty, sector, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (symbol, strategy, entry_price, ts, entry_price, qty, sector),
        )
        self._conn.commit()

    def update_peak(self, symbol: str, new_peak: float) -> None:
        self._conn.execute(
            "UPDATE positions SET peak_price = ? WHERE symbol = ? AND status = 'open' AND ? > peak_price",
            (new_peak, symbol, new_peak),
        )
        self._conn.commit()

    def close_position(self, symbol: str) -> None:
        self._conn.execute(
            "UPDATE positions SET status = 'closed' WHERE symbol = ?",
            (symbol,),
        )
        self._conn.commit()

    def get_open_position(self, symbol: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None

    def list_open_positions(self) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_by_sector(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT sector, COUNT(*) as cnt FROM positions WHERE status = 'open' GROUP BY sector"
        ).fetchall()
        return {r["sector"]: r["cnt"] for r in rows if r["sector"]}

    # ── 거래 기록 ────────────────────────────────────────────────

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        strategy: str,
        reason: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, strategy, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, qty, price, strategy, reason, ts),
        )
        self._conn.commit()

    def get_trades(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        if symbol:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 청산 거래 기록 ───────────────────────────────────────────

    def record_closed_trade(
        self,
        symbol:       str,
        strategy:     str,
        entry_price:  float,
        exit_price:   float,
        qty:          int,
        entry_ts:     str,
        exit_ts:      str,
        exit_reason:  str = "",
        sector:       str = "",
    ) -> None:
        """매수-매도 쌍이 완성됐을 때 청산 기록 저장."""
        import json as _json
        from datetime import datetime, timezone, timedelta

        # 보유 시간 계산
        try:
            t0 = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
            hold_minutes = int((t1 - t0).total_seconds() / 60)
        except Exception:
            hold_minutes = 0

        pnl     = (exit_price - entry_price) * qty
        pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0

        # ET 기준 날짜
        try:
            from zoneinfo import ZoneInfo
            et = datetime.fromisoformat(exit_ts.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
            date_str = et.strftime("%Y-%m-%d")
        except Exception:
            date_str = exit_ts[:10]

        self._conn.execute(
            """INSERT INTO closed_trades
               (symbol, strategy, sector, entry_price, exit_price, qty,
                entry_ts, exit_ts, hold_minutes, pnl, pnl_pct, exit_reason, date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, strategy, sector, entry_price, exit_price, qty,
             entry_ts, exit_ts, hold_minutes, pnl, pnl_pct, exit_reason, date_str),
        )
        self._conn.commit()

    def get_closed_trades(self, date_str: Optional[str] = None, limit: int = 200) -> List[Dict]:
        """날짜별 또는 전체 청산 거래 조회."""
        if date_str:
            rows = self._conn.execute(
                "SELECT * FROM closed_trades WHERE date = ? ORDER BY exit_ts DESC",
                (date_str,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM closed_trades ORDER BY exit_ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_trades_range(self, from_date: str, to_date: str) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM closed_trades WHERE date BETWEEN ? AND ? ORDER BY exit_ts",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 일일 일지 ────────────────────────────────────────────────

    def save_daily_journal(
        self,
        date: str,
        trades_cnt: int,
        win_cnt: int,
        lose_cnt: int,
        realized_pnl: float,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        profit_factor: float,
        best_trade: str,
        worst_trade: str,
        bucket_stats: str,
        ai_analysis: str,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO daily_journal
               (date, trades_cnt, win_cnt, lose_cnt, realized_pnl,
                win_rate, avg_win_pct, avg_loss_pct, profit_factor,
                best_trade, worst_trade, bucket_stats, ai_analysis, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 trades_cnt=excluded.trades_cnt, win_cnt=excluded.win_cnt,
                 lose_cnt=excluded.lose_cnt, realized_pnl=excluded.realized_pnl,
                 win_rate=excluded.win_rate, avg_win_pct=excluded.avg_win_pct,
                 avg_loss_pct=excluded.avg_loss_pct, profit_factor=excluded.profit_factor,
                 best_trade=excluded.best_trade, worst_trade=excluded.worst_trade,
                 bucket_stats=excluded.bucket_stats, ai_analysis=excluded.ai_analysis,
                 created_at=excluded.created_at""",
            (date, trades_cnt, win_cnt, lose_cnt, realized_pnl,
             win_rate, avg_win_pct, avg_loss_pct, profit_factor,
             best_trade, worst_trade, bucket_stats, ai_analysis, ts),
        )
        self._conn.commit()

    def get_daily_journal(self, date: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM daily_journal WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None

    def get_recent_journals(self, days: int = 30) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM daily_journal ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 주간 분석 ────────────────────────────────────────────────

    def save_weekly_analysis(
        self,
        week_start: str,
        total_trades: int,
        win_rate: float,
        total_pnl: float,
        max_drawdown_pct: float,
        best_strategy: str,
        worst_setup: str,
        ai_analysis: str,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO weekly_analysis
               (week_start, total_trades, win_rate, total_pnl,
                max_drawdown_pct, best_strategy, worst_setup, ai_analysis, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(week_start) DO UPDATE SET
                 total_trades=excluded.total_trades, win_rate=excluded.win_rate,
                 total_pnl=excluded.total_pnl, max_drawdown_pct=excluded.max_drawdown_pct,
                 best_strategy=excluded.best_strategy, worst_setup=excluded.worst_setup,
                 ai_analysis=excluded.ai_analysis, created_at=excluded.created_at""",
            (week_start, total_trades, win_rate, total_pnl,
             max_drawdown_pct, best_strategy, worst_setup, ai_analysis, ts),
        )
        self._conn.commit()

    def get_weekly_analysis(self, week_start: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM weekly_analysis WHERE week_start = ?", (week_start,)
        ).fetchone()
        return dict(row) if row else None

    # ── 통계 조회 ────────────────────────────────────────────────

    def get_strategy_stats(self, days: int = 30) -> List[Dict]:
        """전략별 승률/수익률 집계 (최근 N일)."""
        rows = self._conn.execute(
            """SELECT strategy,
                      COUNT(*) as total,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                      ROUND(AVG(pnl_pct), 2) as avg_pnl_pct,
                      ROUND(SUM(pnl), 2) as total_pnl,
                      ROUND(AVG(hold_minutes), 0) as avg_hold_min
               FROM closed_trades
               WHERE date >= date('now', ?)
               GROUP BY strategy
               ORDER BY total_pnl DESC""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_exit_reason_stats(self, days: int = 30) -> List[Dict]:
        """청산 사유별 통계."""
        rows = self._conn.execute(
            """SELECT exit_reason,
                      COUNT(*) as cnt,
                      ROUND(AVG(pnl_pct), 2) as avg_pnl_pct,
                      ROUND(SUM(pnl), 2) as total_pnl
               FROM closed_trades
               WHERE date >= date('now', ?)
               GROUP BY exit_reason
               ORDER BY cnt DESC""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 일일 PnL ────────────────────────────────────────────────

    def upsert_daily_pnl(self, date_str: str, realized: float, unrealized: float) -> None:
        self._conn.execute(
            """INSERT INTO daily_pnl (date, realized, unrealized)
               VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET realized = excluded.realized,
                                               unrealized = excluded.unrealized""",
            (date_str, realized, unrealized),
        )
        self._conn.commit()
