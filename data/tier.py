# data/tier.py
"""
Alpaca 플랜 티어 설정 — 단일 진실의 원천.

모든 모듈은 여기서 플랜 설정을 가져옵니다.
환경변수 ALPACA_PLAN=free|unlimited 로 제어합니다.

[무료(free) 플랜 — 기본값, 페이퍼 트레이딩 테스트용]
  - API 동시 요청: 세마포어 10 (200 req/min 상한)
  - 프리마켓 봉 데이터: 없음 (9:30 ET 이전 1분봉 미제공)
  - 스캔 시각: 9:40 AM ET (장 개시 10분 후)
  - 데이터 소스: Alpaca 일반 bars + 뉴스 API + yfinance + EDGAR(무료)

[유료(unlimited) 플랜 — $9/월, 실전 전환 시]
  - API 동시 요청: 세마포어 50 (10,000 req/min 상한)
  - 프리마켓 봉 데이터: 있음 (4:00 AM~9:30 AM ET 1분봉)
  - 스캔 시각: 8:00 AM ET (장 열리기 1.5시간 전)
  - WebSocket 종목 수: 100개+ (무료: 30개)
  - 데이터 소스: 무료 소스 + 프리마켓 bars

전환 방법:
  .env 파일에 ALPACA_PLAN=unlimited 추가 후 재시작

무료 대체 데이터 소스 (플랜 무관):
  - SEC EDGAR 8-K: 공식 API, 완전 무료, 실적/FDA/M&A 공시 실시간
  - Finviz 스크리너: HTML 파싱, 갭업 종목 필터 (장 개시 후)
  - yfinance: 일봉/뉴스/펀더멘털 (already used)
"""
from __future__ import annotations

import os

PLAN: str = os.getenv("ALPACA_PLAN", "free").lower()

# ── 기능 플래그 ───────────────────────────────────────────────────────
PREMARKET_BARS_ENABLED: bool = PLAN == "unlimited"  # 4:00~9:30 ET 1분봉
EXTENDED_HOURS_FEED:    bool = PLAN == "unlimited"  # 시간외 봉 포함
HIGH_RATE_LIMIT:        bool = PLAN == "unlimited"  # API 한도 상향

# ── 스캔 시각 (ET) ────────────────────────────────────────────────────
SCAN_HOUR:   int = 8  if PREMARKET_BARS_ENABLED else 9   # 8:00 vs 9:40
SCAN_MINUTE: int = 0  if PREMARKET_BARS_ENABLED else 40

# ── API 동시 요청 수 ──────────────────────────────────────────────────
SEMAPHORE_SIZE: int = 50 if HIGH_RATE_LIMIT else 10

# ── 봉 조회 파라미터 ──────────────────────────────────────────────────
BAR_LIMIT_INTRADAY: int = 120 if PREMARKET_BARS_ENABLED else 390
# unlimited: 4:00~8:00 = 240분 중 최근 120봉 (4시간 프리마켓)
# free:      9:30~장마감 = 최대 390봉 (1거래일)


def describe() -> str:
    """현재 플랜 설정 요약 (로그/텔레그램용)."""
    return (
        f"Alpaca plan={PLAN.upper()} | "
        f"scan={SCAN_HOUR:02d}:{SCAN_MINUTE:02d} ET | "
        f"semaphore={SEMAPHORE_SIZE} | "
        f"premarket_bars={PREMARKET_BARS_ENABLED}"
    )
