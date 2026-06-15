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
