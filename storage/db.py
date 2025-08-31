import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY,
  ts DATETIME,
  symbol TEXT, side TEXT,
  qty INT, price REAL,
  strategy TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS fills(
  order_id TEXT, fill_ts DATETIME,
  price REAL, qty INT
);
CREATE TABLE IF NOT EXISTS pnl(
  day DATE PRIMARY KEY,
  realized REAL, unrealized REAL, max_drawdown REAL
);
"""

class DB:
    def __init__(self, path: str = "storage/trade.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)

    def save_order(self, row: Dict[str, Any]):
        cols = ",".join(row.keys())
        qs = ",".join(["?"] * len(row))
        self.conn.execute(
            f"INSERT OR REPLACE INTO orders({cols}) VALUES({qs})",
            tuple(row.values())
        )
        self.conn.commit()

    def bulk_save_fills(self, rows: Iterable[Dict[str, Any]]):
        self.conn.executemany(
            "INSERT INTO fills(order_id, fill_ts, price, qty) VALUES(?,?,?,?)",
            [(r["order_id"], r["fill_ts"], r["price"], r["qty"]) for r in rows]
        )
        self.conn.commit()
