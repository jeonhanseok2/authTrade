# core/StrategyManager.py
"""
전략 전환 엔진 — B3/B2 모드 스위칭 + 각 전략 실행 조율

책임:
  1. check_market_regime(): 매일 프리마켓 스캔 결과로 B3/B2 결정
  2. B3 모드: A/B 그룹 로테이션, 3분룰, 개미털기 방어 조율
  3. B2 모드: {TQQQ, SOXL, FNGU, LABU, QQQ, SPY} ATR 조정 수익률 Top2 순환매
  4. 모드 전환 시 60초 동기화 대기 + AccountManager 자금 재확인

B2 내부 레짐:
  BULL_LEVERAGE : 주가 > 20일 MA → TQQQ/SOXL/FNGU/LABU 상위 2개 × 50%
  DEFENSE_INDEX : 주가 ≤ 20일 MA → QQQ/SPY 모멘텀 강한 1개 × 100%
  CASH          : QQQ + SPY 모두 MA20 하향 → 전량 청산
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from core.regime_engine          import MarketMode
from core.MarketRegimeAnalyzer   import MarketRegimeAnalyzer, ScanResult
from strategy.b2_allocation      import B2AllocationEngine, B2AllocMode, AllocationTarget


class StrategyManager:
    """
    전략 전환 엔진.

    Orchestrator가 레짐 엔진과 B2 배분 엔진을 직접 건드리지 않고
    이 클래스를 단일 창구로 사용합니다.

    사용 예:
        mgr = StrategyManager(analyzer, b2_alloc, notify=orch._notify)
        # main 루프에서
        await mgr.run_premarket_loop(b3_syms)
        # B3 진입 전
        if mgr.is_b3 and not mgr.is_syncing:
            ...
        # B2 리밸런싱
        if mgr.is_b2:
            target = mgr.rebalance_b2()
    """

    def __init__(
        self,
        analyzer: MarketRegimeAnalyzer,
        b2_alloc: B2AllocationEngine,
        account_mgr=None,            # AccountManager (있으면 모드 전환 시 자금 재확인)
        broker=None,
        notify:   Optional[Callable[[str], None]] = None,
    ) -> None:
        self._analyzer    = analyzer
        self._b2_alloc    = b2_alloc
        self._account_mgr = account_mgr
        self._broker      = broker
        self._notify      = notify or (lambda _: None)

    # ── 모드 조회 ─────────────────────────────────────────────────────

    @property
    def current_mode(self) -> MarketMode:
        return self._analyzer.current_mode

    @property
    def is_b3(self) -> bool:
        return self._analyzer.current_mode == MarketMode.B3_AGGRESSIVE

    @property
    def is_b2(self) -> bool:
        return self._analyzer.current_mode == MarketMode.B2_SWING

    @property
    def is_syncing(self) -> bool:
        """모드 전환 직후 60초 동기화 중이면 True → 신규 진입 차단."""
        return self._analyzer.is_syncing

    @property
    def sync_remaining(self) -> float:
        return self._analyzer.sync_remaining

    @property
    def b2_alloc_mode(self) -> B2AllocMode:
        return self._b2_alloc.current_mode

    @property
    def b2_target(self) -> Optional[AllocationTarget]:
        return self._b2_alloc.current_target

    # ── B3 모드 유틸 ─────────────────────────────────────────────────

    def b3_entry_allowed(self, active_group: str) -> bool:
        """B3 신규 진입 가능 여부 (모드 확인 + 동기화 대기 체크)."""
        return self.is_b3 and not self.is_syncing

    # ── B2 리밸런싱 ──────────────────────────────────────────────────

    def rebalance_b2(self) -> AllocationTarget:
        """
        B2 포트폴리오 리밸런싱.

        QQQ/SPY vs MA20 → BULL_LEVERAGE / DEFENSE_INDEX / CASH 결정 후
        ATR 조정 수익률 Top2 종목 선정.

        B2 유니버스: TQQQ, SOXL, FNGU, LABU (레버리지) / QQQ, SPY (방어)
        """
        target = self._b2_alloc.rebalance()
        logging.info("[StrategyManager] B2 리밸런싱: %s", target.summary())
        return target

    def check_b2_weekly_exit(self, symbol: str):
        """방어 모드: 주봉 MA20 이탈 여부 체크 → (should_exit, reason)."""
        return self._b2_alloc.check_weekly_exit(symbol)

    # ── 프리마켓 스캔 루프 ───────────────────────────────────────────

    async def run_premarket_loop(
        self,
        scan_symbols: List[str],
        df_fetcher=None,
    ) -> None:
        """
        main.py asyncio.gather 태스크 — 매일 9:20 ET 모드 결정.

        스캔 완료 후 모드가 전환되었으면 AccountManager.on_mode_switch()를
        호출하여 Settled Cash를 재확인합니다.
        """
        import asyncio as _asyncio

        async def _loop():
            from datetime import date as _date, datetime
            import zoneinfo
            ET = zoneinfo.ZoneInfo("America/New_York")
            last_date = _date.min

            logging.info("[StrategyManager] 프리마켓 루프 시작 (매일 9:20 ET)")
            while True:
                try:
                    now_et = datetime.now(ET)
                    if (
                        now_et.weekday() < 5
                        and now_et.hour   == 9
                        and now_et.minute == 40   # 장 개시 10분 후 — 실제 1분봉 기반 alpha 계산
                        and last_date != _date.today()
                    ):
                        prev_mode = self.current_mode
                        result: ScanResult = await self._analyzer.scan(scan_symbols, df_fetcher)
                        last_date = _date.today()

                        # 모드 전환 시 자금 재확인 (T+1 Settled Cash 동기화)
                        if result.mode != prev_mode and self._account_mgr and self._broker:
                            await self._account_mgr.on_mode_switch(self._broker)

                except Exception as exc:
                    logging.error("[StrategyManager] 프리마켓 루프 오류: %s", exc)
                await _asyncio.sleep(30)

        await _loop()
