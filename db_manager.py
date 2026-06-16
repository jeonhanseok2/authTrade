"""
db_manager.py — 사계절 퀀트 엔진 전용 DB 관리 모듈

스키마:
  trades       — 매매 기록 (매수/매도 가격, 수익률, 모드)
  market_log   — 일별 시장 상태 (나스닥 MA20, 레짐, 스캐너 점수)
  system_state — 봇 재시작용 상태 키-값 저장소 (A/B 그룹, 현재 모드 등)

DB 경로: <프로젝트 루트>/storage/db/trading_data.db
"""
import os
import sqlite3
import logging
from typing import Optional

# ── 경로 설정 ─────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "storage", "db", "trading_data.db")

# 폴더가 없으면 자동 생성
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# ── 스키마 ────────────────────────────────────────────────────────────

_DDL = """
-- 1. 매매 기록
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT        NOT NULL,
    buy_price  DECIMAL(18, 8) NOT NULL,
    sell_price DECIMAL(18, 8),
    quantity   DECIMAL(18, 8) NOT NULL,
    mode       TEXT        NOT NULL,
    result     DECIMAL(18, 8),
    timestamp  DATETIME    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);

-- 2. 시장 상태 로그
CREATE TABLE IF NOT EXISTS market_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          DATE        NOT NULL,
    nasdaq_ma20   DECIMAL(18, 8),
    regime        TEXT        NOT NULL,
    scanner_score INTEGER,
    timestamp     DATETIME    DEFAULT CURRENT_TIMESTAMP
);

-- 3. 봇 재시작용 시스템 상태
CREATE TABLE IF NOT EXISTS system_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    logging.info("[db_manager] DB 초기화 완료: %s", DB_PATH)


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
        result:     수익률 (예: 0.05 = +5%, -0.03 = -3%, 미청산이면 None)

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
    logging.debug("[db_manager] save_trade: %s mode=%s result=%s id=%d", symbol, mode, result, row_id)
    return row_id


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
        scanner_score: 해당 날 신뢰도 ≥70점 후보 수

    Returns:
        생성된 row id
    """
    sql = """
        INSERT INTO market_log (date, nasdaq_ma20, regime, scanner_score)
        VALUES (?, ?, ?, ?)
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(sql, (date, nasdaq_ma20, regime, scanner_score))
        conn.commit()
        row_id = cur.lastrowid
    logging.debug("[db_manager] save_market_log: %s regime=%s score=%s", date, regime, scanner_score)
    return row_id


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

    Returns:
        저장된 값 또는 default
    """
    sql = "SELECT value FROM system_state WHERE key = ?"
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(sql, (key,)).fetchone()
    return row[0] if row else default
