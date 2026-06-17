# core/regime_engine.py
"""
사계절 퀀트 엔진 — 시장 상태 진단 및 모드 자동 전환 (Regime Switching)

장 시작 10분 전 신뢰도 스캐너로 급등주 후보를 카운트하여:
  ≥ 5종목 → B3_AGGRESSIVE (A/B 로테이션 + 3분룰 + 개미털기 방어)
  < 5종목 → B2_SWING      (TQQQ/SOXL MA20+RSI30 스윙, ATR×1.5 트레일링)

모드 전환 시:
  1. 텔레그램 전략 상태 보고
  2. 이전 모드 포지션과의 충돌 방지를 위해 60초 동기화 대기
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date as _date
from enum import Enum
from typing import Callable, List, Optional

import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

# 모드 전환 임계치
B3_THRESHOLD = 5     # 신뢰도 ≥70점 후보 5개 이상 → B3
# 모드 전환 동기화 대기 시간
SYNC_WAIT_SECONDS = 60.0
# 스캔 시각 (ET): 장 개시 10분 후 — 실제 1분봉 확보 후 alpha 계산
PREMARKET_SCAN_HOUR   = 9
PREMARKET_SCAN_MINUTE = 40


class MarketMode(Enum):
    B3_AGGRESSIVE = "B3_AGGRESSIVE"   # 급등주 단타
    B2_SWING      = "B2_SWING"        # 지수 ETF 스윙


_MODE_LABELS = {
    MarketMode.B3_AGGRESSIVE: "B3 급등주 모드 (A/B 로테이션)",
    MarketMode.B2_SWING:      "B2 지수 스윙 모드 (TQQQ/SOXL)",
}

_MODE_EMOJIS = {
    MarketMode.B3_AGGRESSIVE: "🚀",
    MarketMode.B2_SWING:      "📊",
}


class RegimeEngine:
    """
    시장 모드 감지 + 전환 관리자.

    사용 예:
        engine = RegimeEngine(notify=orchestrator._notify)
        await engine.run_daily_premarket_scan(scan_symbols, conf_scanner)
        if engine.current_mode == MarketMode.B3_AGGRESSIVE:
            ...
    """

    def __init__(self, notify: Optional[Callable[[str], None]] = None) -> None:
        self._mode:         MarketMode = MarketMode.B3_AGGRESSIVE  # 기본값: B3
        self._notify        = notify or (lambda _: None)
        self._sync_until:   float      = 0.0    # time.monotonic() 기준 동기화 만료 시각
        self._last_scan_date: _date    = _date.min
        self._last_count:   int        = 0

    @property
    def current_mode(self) -> MarketMode:
        return self._mode

    @property
    def is_syncing(self) -> bool:
        """모드 전환 후 동기화 대기 중이면 True — 이 기간엔 신규 진입 차단."""
        return time.monotonic() < self._sync_until

    @property
    def sync_remaining(self) -> float:
        return max(0.0, self._sync_until - time.monotonic())

    def detect_mode(self, candidate_count: int) -> MarketMode:
        """후보 개수로 모드 결정."""
        return MarketMode.B3_AGGRESSIVE if candidate_count >= B3_THRESHOLD else MarketMode.B2_SWING

    def switch_mode(self, new_mode: MarketMode, candidate_count: int) -> bool:
        """
        모드 전환.

        Args:
            new_mode:        전환할 모드
            candidate_count: 스캔 결과 후보 수

        Returns:
            True if mode actually changed
        """
        if new_mode == self._mode:
            logging.info(
                "[RegimeEngine] 모드 유지: %s (후보 %d종목)",
                _MODE_LABELS[self._mode], candidate_count,
            )
            return False

        prev = self._mode
        self._mode = new_mode

        # 동기화 대기 시작 — 이전 모드 포지션과 충돌 방지
        self._sync_until = time.monotonic() + SYNC_WAIT_SECONDS

        emoji = _MODE_EMOJIS[new_mode]
        msg = (
            f"{emoji} [전략 모드 전환]\n"
            f"이전: {_MODE_LABELS[prev]}\n"
            f"현재: {_MODE_LABELS[new_mode]}\n"
            f"사유: 신뢰도 ≥70점 후보 {candidate_count}종목 "
            f"({'≥' if candidate_count >= B3_THRESHOLD else '<'}{B3_THRESHOLD}개)\n"
            f"⏳ {SYNC_WAIT_SECONDS:.0f}초 동기화 대기 시작"
        )
        self._notify(msg)
        logging.warning("[RegimeEngine] %s → %s (후보=%d)", prev.value, new_mode.value, candidate_count)
        return True

    async def run_daily_premarket_scan(
        self,
        scan_symbols: List[str],
        conf_scanner,          # ConfidenceScanner instance
        df_fetcher=None,       # async callable(symbol) -> pd.DataFrame | None
    ) -> MarketMode:
        """
        매일 장 시작 10분 전 (9:20 ET) 신뢰도 스캔 실행.

        scan_symbols:  스캔 대상 종목 목록
        conf_scanner:  ConfidenceScanner 인스턴스
        df_fetcher:    종목 분봉 조회 async callable (없으면 Alpaca 직접 조회)

        Returns:
            결정된 MarketMode
        """
        from data.alpaca_bars import fetch_bars

        qualified = 0
        for sym in scan_symbols[:50]:   # 최대 50종목만 프리마켓 스캔
            try:
                if df_fetcher:
                    df = await df_fetcher(sym)
                else:
                    df = await asyncio.to_thread(fetch_bars, sym, "1Min", 390)
                if df is None or df.empty:
                    continue
                score = await asyncio.to_thread(conf_scanner.score, sym, df)
                if score.is_tradeable:
                    qualified += 1
            except Exception as exc:
                logging.debug("[RegimeEngine] %s 프리마켓 스캔 실패: %s", sym, exc)

        self._last_count    = qualified
        self._last_scan_date = _date.today()

        new_mode = self.detect_mode(qualified)
        self.switch_mode(new_mode, qualified)
        logging.info("[RegimeEngine] 프리마켓 스캔 완료 — 후보 %d종목 → %s", qualified, new_mode.value)
        return new_mode

    async def run_premarket_loop(
        self,
        scan_symbols: List[str],
        conf_scanner,
        df_fetcher=None,
    ) -> None:
        """
        매일 9:20 ET에 프리마켓 스캔을 실행하는 무한 루프.
        main.py의 asyncio.gather에 태스크로 추가.
        """
        from datetime import datetime
        logging.info("[RegimeEngine] 프리마켓 루프 시작 (매일 9:20 ET 스캔)")

        while True:
            try:
                now_et = datetime.now(ET)
                # 장 시작 10분 전 체크
                if (now_et.weekday() < 5 and
                        now_et.hour == PREMARKET_SCAN_HOUR and
                        now_et.minute == PREMARKET_SCAN_MINUTE and
                        self._last_scan_date != _date.today()):
                    await self.run_daily_premarket_scan(scan_symbols, conf_scanner, df_fetcher)
            except Exception as exc:
                logging.error("[RegimeEngine] 프리마켓 루프 오류: %s", exc)
            await asyncio.sleep(30)   # 30초마다 시각 체크
