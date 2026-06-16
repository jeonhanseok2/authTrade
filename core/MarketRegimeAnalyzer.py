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

ET                    = zoneinfo.ZoneInfo("America/New_York")
PREMARKET_SCAN_HOUR   = 9
PREMARKET_SCAN_MINUTE = 20
SCAN_MAX_SYMBOLS      = 50
SCAN_INTERVAL_SEC     = 30


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
    ) -> ScanResult:
        """
        신뢰도 스캔 → 모드 결정.

        df_fetcher: async callable(symbol) -> pd.DataFrame | None
                    미제공 시 yfinance 직접 조회.
        """
        import yfinance as yf

        scores: Dict[str, ConfidenceScore] = {}
        qualified = 0

        for sym in scan_symbols[:SCAN_MAX_SYMBOLS]:
            try:
                if df_fetcher:
                    df = await df_fetcher(sym)
                else:
                    raw = await asyncio.to_thread(
                        yf.download, sym, period="1d", interval="1m",
                        progress=False, auto_adjust=True,
                    )
                    if raw is None or raw.empty:
                        continue
                    raw.columns = [c.lower() for c in raw.columns]
                    df = raw

                if df is None or df.empty:
                    continue

                sc = await asyncio.to_thread(self._scanner.score, sym, df)

                # 뉴스 심리 보정: 최종 신뢰도 = 차트×0.7 + 뉴스×0.3
                effective_score = sc.total
                if self._news:
                    if self._news.is_blocked(sym):
                        logging.info("[MarketRegimeAnalyzer] %s 뉴스 차단 — 스킵", sym)
                        continue
                    effective_score = await asyncio.to_thread(
                        self._news.blend, sym, sc.total
                    )

                if effective_score >= 70:   # 보정 후 기준으로 판단
                    scores[sym] = sc
                    qualified += 1

            except Exception as exc:
                logging.debug("[MarketRegimeAnalyzer] %s 스캔 실패: %s", sym, exc)

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
    ) -> None:
        """
        매일 9:20 ET 프리마켓 스캔 루프.
        main.py asyncio.gather에 태스크로 등록.
        """
        logging.info("[MarketRegimeAnalyzer] 프리마켓 루프 시작 (매일 9:20 ET 스캔)")
        while True:
            try:
                now_et = datetime.now(ET)
                if (
                    now_et.weekday() < 5
                    and now_et.hour   == PREMARKET_SCAN_HOUR
                    and now_et.minute == PREMARKET_SCAN_MINUTE
                    and self._last_date != _date.today()
                ):
                    await self.scan(scan_symbols, df_fetcher)
            except Exception as exc:
                logging.error("[MarketRegimeAnalyzer] 루프 오류: %s", exc)
            await asyncio.sleep(SCAN_INTERVAL_SEC)
