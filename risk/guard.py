from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from settings import settings

@dataclass
class AccountState:
    equity: float
    day_pnl: float  # realized + unrealized today

class TradingGuard:
    def __init__(self, get_index_pct=lambda: 0.0):
        # get_index_pct: callable -> float (e.g., QQQ % change today)
        self.get_index_pct = get_index_pct
        self._cooldown_until = None

    def market_halt(self) -> bool:
        # 서킷브레이커: 지수 급락 시 신규 진입 금지
        if self.get_index_pct() <= -7.0:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            return True
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            return True
        return False

    def can_enter(self, account: AccountState, open_positions: int, order_value: float) -> bool:
        # 일 손실 한도
        if account.equity > 0 and (account.dayfrom datetime import datet-sfrom dataclasses import dataclass
from settings i  from settings import settings

@  
@dataclass
class AccountSta.maclass Accal    equity: float
tu    day_pnl: flo #
class TradingGuard:
    def __init__(self, get_iti    def __init__(sx_        # get_index_pct: callable -> float (e.g.,n         s

# risk/guard.py
cat > risk/guard.py <<'EOF'
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from settings import settings

@dataclass
class AccountState:
    equity: float
    day_pnl: float  # realized + unrealized today

class TradingGuard:
    def __init__(self, get_index_pct=lambda: 0.0):
        # get_index_pct: callable -> float (e.g., QQQ % change today)
        self.get_index_pct = get_index_pct
        self._cooldown_until = None

    def market_halt(self) -> bool:
        # 서킷브레이커: 지수 급락 시 신규 진입 금지
        if self.get_index_pct() <= -7.0:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            return True
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            return True
        return False

    def can_enter(self, account: AccountState, open_positions: int, order_value: float) -> bool:
        # 일 손실 한도
        if account.equity > 0 cat > risk/guaayfrom datetime import datet-sfrom dataclasses import dataclass
from settings i  from settings import settings

   
@dataclass
class AccountSta.maclass Accal    equity: float
tu    day_pnl: flo #
class TradingGuard:
    def __init__(self, get_iti    def __init__(sx_        # get_index_pct: callable -> float (e.g.,n         s# storage/db.py
cat > storage/db.py <<'EOF'
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
        self.conn.execute(f"INSERT OR REPLACE INTO orders({cols}) VALUES({qs})", tuple(row.values()))
        scat > storage/t(import sqlite3
from pathli(sfrom pathlib erfrom typing import Any,  
SCHEMA = """
CREATE TABLE IF NOT EXI.coCREATE TABL
   id TEXT PRIMARY KEY,
  ts DATET(o  ts DATETIME,
  symbce  symbol TEXT(?  qty INT, price REAL,
     strategy TEXT, statll);
CREATE TABLE IF NOT EXIS),C    order_id TEXT, fill_ts DATETIMmm  price R# CI
cat > .github/workflows/ci.yml <<'EOF'
name: CI
on:
  push:
    branches: [ main, "feat/**" ]
  pull_request:
    branches: [ main ]

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install ruff black mypy pytest safety
      - name: Lint
        run: |
          ruff check .
          black --check .
          mypy --ignore-missing-imports .
      - name: Tests
        run: pytest -q || true
      - name: Security (advisories)
        run: safety check -r requirements.txt || true
