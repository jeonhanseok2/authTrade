# core/MarketRegimeAnalyzer.py
"""
시장 상태 진단 모듈 — 프리마켓 스캔 + 레짐 감지

매일 9:20 ET에 워치리스트 종목을 신뢰도 스코어링하여
B3_AGGRESSIVE / B2_SWING 모드를 결정합니다.

판단 기준:
  신뢰도 ≥70점 후보 ≥5개 → B3_AGGRESSIVE
  신뢰도 ≥70점 후보  <5개 → B2_SWING
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from typing import Callable, Dict, List, Optional

import zoneinfo

from core.regime_engine          import RegimeEngine, MarketMode
from strategy.confidence_scanner import ConfidenceScanner, ConfidenceScore
from strategy.news_analyzer      import NewsAnalyzer
from data.tier import (
    SCAN_HOUR         as PREMARKET_SCAN_HOUR,
    SCAN_MINUTE       as PREMARKET_SCAN_MINUTE,
    BAR_LIMIT_INTRADAY as PREMARKET_BAR_LIMIT,
    EXTENDED_HOURS_FEED as PREMARKET_EXTENDED,
    describe          as _tier_describe,
)

ET                = zoneinfo.ZoneInfo("America/New_York")
SCAN_MAX_SYMBOLS  = 50
SCAN_INTERVAL_SEC = 30


@dataclass
class ScanResult:
    """프리마켓 스캔 결과."""
    mode:       MarketMode
    count:      int                              # 신뢰도 ≥70점 후보 수
    scores:     Dict[str, ConfidenceScore] = field(default_factory=dict)
    scanned_at: str = ""

    def summary(self) -> str:
        top = list(self.scores.items())[:5]
        lines = [
            f"[프리마켓 스캔] {self.scanned_at}",
            f"모드: {self.mode.value} — 신뢰도 ≥70점 후보 {self.count}종목",
        ]
        for sym, sc in top:
            lines.append(f"  {sc.summary()}")
        if len(self.scores) > 5:
            lines.append(f"  ... 외 {len(self.scores) - 5}종목")
        return "\n".join(lines)


class MarketRegimeAnalyzer:
    """
    시장 상태 진단기.

    RegimeEngine + ConfidenceScanner를 통합하여 매일 장 시작 전
    프리마켓 스캔을 실행하고 B3/B2 모드를 결정합니다.

    Orchestrator → StrategyManager → 여기에 의존.
    """

    def __init__(
        self,
        regime_engine: RegimeEngine,
        conf_scanner:  ConfidenceScanner,
        notify:        Optional[Callable[[str], None]] = None,
        news_analyzer: Optional[NewsAnalyzer] = None,
    ) -> None:
        self._regime        = regime_engine
        self._scanner       = conf_scanner
        self._notify        = notify or (lambda _: None)
        self._news          = news_analyzer   # None이면 뉴스 보정 비활성
        self._last_result:  Optional[ScanResult] = None
        self._last_date:    _date = _date.min

    # ── 상태 조회 ─────────────────────────────────────────────────────

    @property
    def current_mode(self) -> MarketMode:
        return self._regime.current_mode

    @property
    def is_syncing(self) -> bool:
        """모드 전환 직후 60초 동기화 중이면 True → 신규 진입 차단."""
        return self._regime.is_syncing

    @property
    def sync_remaining(self) -> float:
        return self._regime.sync_remaining

    @property
    def last_result(self) -> Optional[ScanResult]:
        return self._last_result

    # ── 스캔 실행 ─────────────────────────────────────────────────────

    async def scan(
        self,
        scan_symbols: List[str],
        df_fetcher=None,
        stream_update_fn=None,   # callable(List[str]) — 신규 종목을 WS 스트림에 추가
    ) -> ScanResult:
        """
        신뢰도 스캔 → 모드 결정 (병렬 처리).

        df_fetcher:      async callable(symbol) -> pd.DataFrame | None
                         미제공 시 Alpaca 직접 조회 (세마포어 10 적용).
        stream_update_fn: callable(syms) — 뉴스 발굴 신규 종목을 WebSocket에 동적 추가.
        호출 시각: 9:40 ET — 장 개시 10분 후 실제 1분봉 기반 alpha 계산 가능.
        """
        from data.alpaca_bars import fetch_bars

        # ── 뉴스 기반 신규 종목 발굴 및 스캔 리스트 확장 ──────────────
        try:
            from data.news_universe import fetch_catalyst_symbols
            news_syms = await asyncio.to_thread(fetch_catalyst_symbols, 4, 100)
            if news_syms:
                existing = set(scan_symbols)
                added    = [s for s in news_syms if s not in existing]
                if added:
                    scan_symbols = list(scan_symbols) + added
                    logging.info(
                        "[MarketRegimeAnalyzer] 뉴스 기반 종목 %d개 추가 → 총 스캔 %d개: %s",
                        len(added), len(scan_symbols), added[:8],
                    )
                    # WebSocket에 신규 종목 동적 추가
                    if stream_update_fn and added:
                        try:
                            stream_update_fn(added)
                        except Exception as exc:
                            logging.debug("[MarketRegimeAnalyzer] stream 업데이트 실패: %s", exc)
        except Exception as exc:
            logging.debug("[MarketRegimeAnalyzer] 뉴스 유니버스 실패 (무시): %s", exc)

        async def _scan_one(sym: str):
            try:
                df = await df_fetcher(sym) if df_fetcher else \
                     await asyncio.to_thread(
                         fetch_bars, sym, "1Min",
                         PREMARKET_BAR_LIMIT, PREMARKET_EXTENDED,
                     )
                if df is None or df.empty:
                    return None
                sc = await asyncio.to_thread(self._scanner.score, sym, df)
                effective_score = sc.total
                if self._news:
                    if self._news.is_blocked(sym):
                        return None
                    effective_score = await asyncio.to_thread(
                        self._news.blend, sym, sc.total
                    )
                return (sym, sc) if effective_score >= 70 else None
            except Exception as exc:
                logging.debug("[MarketRegimeAnalyzer] %s 스캔 실패: %s", sym, exc)
                return None

        # 세마포어(10)로 자동 throttle되므로 gather로 병렬 실행
        raw = await asyncio.gather(
            *[_scan_one(s) for s in scan_symbols[:SCAN_MAX_SYMBOLS]],
            return_exceptions=False,
        )
        scores: Dict[str, ConfidenceScore] = {}
        qualified = 0
        for item in raw:
            if item:
                sym, sc = item
                scores[sym] = sc
                qualified += 1

        now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        mode   = self._regime.detect_mode(qualified)
        self._regime.switch_mode(mode, qualified)

        result = ScanResult(
            mode=mode,
            count=qualified,
            scores=scores,
            scanned_at=now_et,
        )
        self._last_result = result
        self._last_date   = _date.today()

        self._notify(result.summary())
        logging.info("[MarketRegimeAnalyzer] 스캔 완료 — %s", result.summary())
        return result

    # ── 프리마켓 루프 ─────────────────────────────────────────────────

    async def run_loop(
        self,
        scan_symbols: List[str],
        df_fetcher=None,
        stream_update_fn=None,   # callable(List[str]) — WebSocket 동적 추가용
    ) -> None:
        """
        매일 9:40 ET 프리마켓 스캔 루프.
        main.py asyncio.gather에 태스크로 등록.
        """
        logging.info("[MarketRegimeAnalyzer] 프리마켓 루프 시작 — %s", _tier_describe())
        while True:
            try:
                now_et = datetime.now(ET)
                if (
                    now_et.weekday() < 5
                    and now_et.hour   == PREMARKET_SCAN_HOUR
                    and now_et.minute == PREMARKET_SCAN_MINUTE
                    and self._last_date != _date.today()
                ):
                    await self.scan(scan_symbols, df_fetcher, stream_update_fn)
            except Exception as exc:
                logging.error("[MarketRegimeAnalyzer] 루프 오류: %s", exc)
            await asyncio.sleep(SCAN_INTERVAL_SEC)
