"""
storage/db_manager.py — 사계절 퀀트 엔진 전용 DB 관리 모듈

스키마:
  trades        — 매매 기록 (매수/매도 가격, 모드, 수익률)
  market_log    — 일별 시장 상태 (나스닥 MA20, 레짐, 스캐너 점수)
  system_state  — 봇 재시작용 상태 저장소 (ACTIVE_GROUP, CURRENT_MODE 등)

DB 파일 경로: <project_root>/storage/db/trading_data.db
"""
import os
import sqlite3
import logging
from typing import Dict, List, Optional

# ── 경로 설정 ─────────────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # = .../storage/
DB_PATH   = os.path.join(_THIS_DIR, "db", "trading_data.db")   # = .../storage/db/trading_data.db

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# ── DDL ──────────────────────────────────────────────────────────────

_DDL = """
-- 1. 매매 기록
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT           NOT NULL,
    buy_price  DECIMAL(18, 8) NOT NULL,
    sell_price DECIMAL(18, 8),
    quantity   DECIMAL(18, 8) NOT NULL,
    mode       TEXT           NOT NULL,
    result     DECIMAL(18, 8),
    timestamp  DATETIME       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);

-- 2. 시장 상태 로그
CREATE TABLE IF NOT EXISTS market_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          DATE           NOT NULL,
    nasdaq_ma20   DECIMAL(18, 8),
    regime        TEXT           NOT NULL,
    scanner_score INTEGER,
    timestamp     DATETIME       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_market_log_date ON market_log(date);

-- 3. 봇 재시작용 시스템 상태
CREATE TABLE IF NOT EXISTS system_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 4. B4 스나이퍼 모드 거래 기록
CREATE TABLE IF NOT EXISTS b4_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT           NOT NULL,
    buy_price   DECIMAL(18, 8) NOT NULL,
    sell_price  DECIMAL(18, 8) NOT NULL,
    qty         INTEGER        NOT NULL,
    result_pct  DECIMAL(18, 8) NOT NULL,   -- 손익률 (0.05 = +5%)
    exit_reason TEXT           DEFAULT '',
    trade_date  TEXT           NOT NULL,   -- YYYY-MM-DD ET (연패 계산 기준)
    timestamp   DATETIME       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_b4_trades_date ON b4_trades(trade_date);

-- 5. B4 쿨다운 상태 (3거래일 연속 손절 → 2거래일 중지)
CREATE TABLE IF NOT EXISTS b4_cooldown (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date  TEXT    NOT NULL,
    end_date    TEXT    NOT NULL,   -- 이 날(포함)까지 B4 중지
    reason      TEXT    DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ── 초기화 ────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    DB 파일 생성 + 테이블/인덱스 초기화.
    애플리케이션 시작 시 1회 호출.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_DDL)
        conn.commit()
    logging.info("[db_manager] 초기화 완료: %s", DB_PATH)


# ── 매매 기록 ─────────────────────────────────────────────────────────

def save_trade(
    symbol:     str,
    buy_price:  float,
    sell_price: Optional[float],
    quantity:   float,
    mode:       str,
    result:     Optional[float],
) -> int:
    """
    매매 결과 저장.

    Args:
        symbol:     종목 코드
        buy_price:  매수 가격
        sell_price: 매도 가격 (미청산이면 None)
        quantity:   수량
        mode:       'B3' 또는 'B2'
        result:     수익률 (0.05 = +5%, -0.03 = -3%, 미청산이면 None)

    Returns:
        생성된 row id
    """
    sql = """
        INSERT INTO trades (symbol, buy_price, sell_price, quantity, mode, result)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(sql, (symbol, buy_price, sell_price, quantity, mode, result))
        conn.commit()
    row_id = cur.lastrowid
    logging.debug("[db_manager] save_trade: %s mode=%s result=%s", symbol, mode, result)
    return row_id


def get_trades_today() -> List[Dict]:
    """오늘 날짜 매매 기록 반환 (timestamp 기준)."""
    sql = """
        SELECT symbol, buy_price, sell_price, quantity, mode, result, timestamp
        FROM trades
        WHERE DATE(timestamp) = DATE('now')
        ORDER BY timestamp DESC
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_trades(days: int = 30) -> List[Dict]:
    """최근 N일 매매 기록 전체 반환."""
    sql = """
        SELECT symbol, buy_price, sell_price, quantity, mode, result, timestamp
        FROM trades
        WHERE timestamp >= DATETIME('now', ?)
        ORDER BY timestamp DESC
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


# ── 시장 상태 로그 ────────────────────────────────────────────────────

def save_market_log(
    date:          str,
    nasdaq_ma20:   Optional[float],
    regime:        str,
    scanner_score: Optional[int] = None,
) -> int:
    """
    일별 시장 상태 기록.

    Args:
        date:          날짜 문자열 (예: '2026-06-16')
        nasdaq_ma20:   나스닥 20일 이동평균 가격
        regime:        'B3' 또는 'B2'
        scanner_score: 신뢰도 ≥70점 후보 종목 수
    """
    sql = """
        INSERT INTO market_log (date, nasdaq_ma20, regime, scanner_score)
        VALUES (?, ?, ?, ?)
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(sql, (date, nasdaq_ma20, regime, scanner_score))
        conn.commit()
    logging.debug("[db_manager] save_market_log: %s regime=%s score=%s", date, regime, scanner_score)
    return cur.lastrowid


def get_latest_market_log() -> Optional[Dict]:
    """가장 최근 시장 상태 로그 반환."""
    sql = "SELECT * FROM market_log ORDER BY timestamp DESC LIMIT 1"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql).fetchone()
    return dict(row) if row else None


# ── 시스템 상태 ───────────────────────────────────────────────────────

def update_system_state(key: str, value: str) -> None:
    """
    봇 상태 키-값 저장 (재시작 시 복원용).

    예:
        update_system_state('CURRENT_MODE', 'B3_AGGRESSIVE')
        update_system_state('ACTIVE_GROUP', 'A')

    기존 키면 덮어씀 (UPSERT).
    """
    sql = "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(sql, (key, value))
        conn.commit()
    logging.debug("[db_manager] system_state[%s] = %s", key, value)


def get_system_state(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    봇 재시작 시 이전 상태 불러오기.

    예:
        mode  = get_system_state('CURRENT_MODE', 'B3_AGGRESSIVE')
        group = get_system_state('ACTIVE_GROUP', 'A')
    """
    sql = "SELECT value FROM system_state WHERE key = ?"
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(sql, (key,)).fetchone()
    return row[0] if row else default


def get_all_system_states() -> Dict[str, str]:
    """모든 시스템 상태 키-값 딕셔너리 반환."""
    sql = "SELECT key, value FROM system_state"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql).fetchall()
    return {r[0]: r[1] for r in rows}


# ── B4 스나이퍼 모드 (옵션 버전) ─────────────────────────────────────

def save_b4_trade(
    symbol:      str,
    buy_price:   float,
    sell_price:  float,
    qty:         int,
    result_pct:  float,
    exit_reason: str,
    trade_date:  str,
) -> int:
    """
    B4 거래 결과 저장 (진입+청산 완료 시 1회 호출).

    Args:
        symbol:      옵션 OCC 심볼 (예: "QQQ240621C00490000")
        buy_price:   계약당 진입 프리미엄
        sell_price:  계약당 청산 프리미엄
        qty:         계약 수
        result_pct:  손익률 (0.50 = +50%, -0.20 = -20%)
        exit_reason: 청산 사유
        trade_date:  거래일 (YYYY-MM-DD ET, 쿨다운 계산 기준)
    """
    sql = """
        INSERT INTO b4_trades
        (symbol, buy_price, sell_price, qty, result_pct, exit_reason, trade_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(sql, (symbol, buy_price, sell_price, qty,
                                 result_pct, exit_reason, trade_date))
        conn.commit()
    logging.debug("[db_manager] save_b4_trade: %s result=%.2f%% qty=%d",
                  symbol, result_pct * 100, qty)
    return cur.lastrowid


def get_b4_consecutive_loss_days() -> int:
    """
    최근 B4 거래일 중 연속 손절(일간 합산 손익 < 0) 일수 반환.

    3 이상 반환 → 쿨다운 트리거.
    """
    sql = """
        SELECT trade_date, SUM(result_pct) AS daily_pnl
        FROM b4_trades
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 10
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql).fetchall()

    consecutive = 0
    for _, daily_pnl in rows:
        if daily_pnl is not None and float(daily_pnl) < 0:
            consecutive += 1
        else:
            break
    return consecutive


def is_b4_cooldown_active() -> bool:
    """현재 B4 쿨다운 상태 여부 (end_date >= 오늘이면 활성)."""
    sql = "SELECT COUNT(*) FROM b4_cooldown WHERE end_date >= DATE('now')"
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(sql).fetchone()[0]
    return count > 0


def set_b4_cooldown(reason: str = "", trading_days: int = 2) -> None:
    """쿨다운 설정 (N 거래일 후까지 B4 중지)."""
    from datetime import date
    today = date.today()
    end   = _next_n_trading_days(today, trading_days)
    sql   = "INSERT INTO b4_cooldown (start_date, end_date, reason) VALUES (?, ?, ?)"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(sql, (str(today), str(end), reason))
        conn.commit()
    logging.warning("[db_manager] B4 쿨다운: %s ~ %s | %s", today, end, reason)


def _next_n_trading_days(from_date, n: int):
    """from_date 로부터 n 거래일(월~금) 이후 날짜."""
    from datetime import timedelta
    d = from_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:   # Mon–Fri
            added += 1
    return d
