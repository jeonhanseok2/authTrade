"""
strategy/strategy_engine.py — 사계절 퀀트 엔진 통합 인터페이스

MarketRegimeAnalyzer + StrategyManager + AccountManager를 하나의 진입점으로
묶어 Orchestrator가 단순한 API로 사용할 수 있게 합니다.

주요 역할:
  1. 모드 결정 (B3 급등주 / B2 지수 ETF)
  2. 진입/청산 시 db_manager.save_trade() 자동 기록
  3. 모드 전환 시 db_manager.update_system_state() 자동 갱신
  4. 재시작 시 db_manager.get_system_state()로 이전 상태 복원

B3 모드:
  - A/B 그룹 로테이션 (홀수 날 A, 짝수 날 B)
  - 3분 룰: 진입 후 3~8분 내 PnL ≤ 0 → 절반 매도
  - ATR 가변 트레일링 스탑 (ExitStrategyEngine 위임)

B2 모드:
  - 유니버스: TQQQ, SOXL, FNGU, LABU (레버리지) / QQQ, SPY (방어)
  - ATR 조정 수익률 상위 2개 순환매
  - 주가 > MA20 → 레버리지 ETF, 주가 ≤ MA20 → 지수 ETF, 모두 하향 → 현금
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Callable, Dict, List, Optional

from core.MarketRegimeAnalyzer import MarketRegimeAnalyzer
from core.StrategyManager      import StrategyManager
from core.AccountManager       import AccountManager
from core.regime_engine        import MarketMode
from strategy.b2_allocation    import B2AllocMode, AllocationTarget
import storage.db_manager as dbm

# B2 풀 (레버리지 + 방어 통합)
B2_UNIVERSE: List[str] = ["TQQQ", "SOXL", "FNGU", "LABU", "QQQ", "SPY"]


class StrategyEngine:
    """
    사계절 퀀트 엔진 통합 인터페이스.

    Orchestrator가 이 클래스를 통해 레짐/자금/전략을 단일 API로 사용합니다.
    직접 MarketRegimeAnalyzer / StrategyManager / AccountManager를 건드리지 않아도 됩니다.

    사용 예:
        engine = StrategyEngine(analyzer, strategy_mgr, account_mgr, broker)
        engine.restore_state()                # 재시작 시 상태 복원

        # B3 진입 후
        engine.record_entry('NVDA', 120.5, 10, 'B3')

        # B3 청산 후
        engine.record_exit('NVDA', 125.0, 10, 'B3', 125.0 / 120.5 - 1)

        # 모드 전환
        await engine.on_mode_switch(broker)
    """

    def __init__(
        self,
        analyzer:     MarketRegimeAnalyzer,
        strategy_mgr: StrategyManager,
        account_mgr:  AccountManager,
        broker,
        notify:       Optional[Callable[[str], None]] = None,
    ) -> None:
        self._analyzer     = analyzer
        self._strategy_mgr = strategy_mgr
        self._account_mgr  = account_mgr
        self._broker       = broker
        self._notify       = notify or (lambda _: None)

        # DB 초기화 (테이블 없으면 생성)
        dbm.init_db()

    # ── 상태 조회 ─────────────────────────────────────────────────────

    @property
    def current_mode(self) -> MarketMode:
        return self._strategy_mgr.current_mode

    @property
    def is_b3(self) -> bool:
        return self._strategy_mgr.is_b3

    @property
    def is_b2(self) -> bool:
        return self._strategy_mgr.is_b2

    @property
    def is_syncing(self) -> bool:
        return self._strategy_mgr.is_syncing

    @property
    def active_group(self) -> str:
        return self._account_mgr.active_group

    def b3_entry_allowed(self) -> bool:
        """B3 신규 진입 가능 여부 (모드 + 동기화 + 그룹 확인)."""
        return self._strategy_mgr.b3_entry_allowed(self._account_mgr.active_group)

    def capital_for(self, bucket: str, score: int = 100) -> float:
        return self._account_mgr.capital_for(bucket, score)

    def capital_b2(self) -> float:
        return self._account_mgr.capital_b2()

    # ── 재시작 상태 복원 ──────────────────────────────────────────────

    def restore_state(self) -> None:
        """
        DB에서 이전 상태를 읽어 레짐 엔진에 적용.
        애플리케이션 시작 직후 호출.
        """
        saved_mode  = dbm.get_system_state("CURRENT_MODE")
        saved_group = dbm.get_system_state("ACTIVE_GROUP")

        if saved_mode:
            try:
                mode = MarketMode(saved_mode)
                # 엔진에 모드를 직접 주입 (스캔 없이 이전 상태 복원)
                self._analyzer._regime._mode = mode
                logging.info("[StrategyEngine] 이전 모드 복원: %s", saved_mode)
            except ValueError:
                logging.warning("[StrategyEngine] 알 수 없는 모드: %s — 기본값 유지", saved_mode)

        if saved_group:
            logging.info("[StrategyEngine] 이전 그룹: %s (오늘 활성 그룹: %s)",
                         saved_group, self.active_group)

    # ── 매매 기록 자동 저장 ───────────────────────────────────────────

    def record_entry(
        self,
        symbol:    str,
        buy_price: float,
        quantity:  float,
        mode:      str,
    ) -> None:
        """
        진입 기록 — trades에 sell_price/result = None 으로 INSERT.
        Orchestrator의 _do_buy() 에서 호출.
        """
        dbm.save_trade(
            symbol=symbol,
            buy_price=buy_price,
            sell_price=None,
            quantity=quantity,
            mode=mode,
            result=None,
        )
        logging.info("[StrategyEngine] 진입 기록: %s %s qty=%.1f @ %.4f",
                     mode, symbol, quantity, buy_price)

    def record_exit(
        self,
        symbol:     str,
        sell_price: float,
        quantity:   float,
        mode:       str,
        result_pct: float,
        buy_price:  float = 0.0,
    ) -> None:
        """
        청산 기록 — trades에 sell_price / result 포함하여 INSERT.
        Orchestrator의 _do_exit() 에서 호출.
        """
        dbm.save_trade(
            symbol=symbol,
            buy_price=buy_price,
            sell_price=sell_price,
            quantity=quantity,
            mode=mode,
            result=round(result_pct, 6),
        )
        logging.info("[StrategyEngine] 청산 기록: %s %s qty=%.1f @ %.4f (%.2f%%)",
                     mode, symbol, quantity, sell_price, result_pct * 100)

    # ── 모드 전환 처리 ────────────────────────────────────────────────

    async def on_mode_switch(self, new_mode: MarketMode) -> None:
        """
        모드 전환 시 호출.
          - system_state 갱신 (DB 영속화)
          - Settled Cash 재확인 (T+1 프리라이딩 방지)
          - 시장 로그 기록
        """
        mode_label = "B3" if new_mode == MarketMode.B3_AGGRESSIVE else "B2"

        # DB 상태 갱신
        dbm.update_system_state("CURRENT_MODE", new_mode.value)
        dbm.update_system_state("ACTIVE_GROUP",  self.active_group)

        # 시장 로그
        today = str(_date.today())
        dbm.save_market_log(
            date=today,
            nasdaq_ma20=None,       # 필요 시 호출자에서 주입
            regime=mode_label,
            scanner_score=None,
        )

        # Settled Cash 재확인 (AccountManager 경유)
        await self._account_mgr.on_mode_switch(self._broker)

        logging.info("[StrategyEngine] 모드 전환 완료: %s → DB/AccountManager 동기화", new_mode.value)

    # ── B2 리밸런싱 ──────────────────────────────────────────────────

    def rebalance_b2(self) -> AllocationTarget:
        """
        B2 포트폴리오 리밸런싱 실행 후 DB에 시장 로그 기록.

        Returns:
            AllocationTarget — 오늘의 목표 포트폴리오
        """
        target = self._strategy_mgr.rebalance_b2()
        mode_label = {
            B2AllocMode.BULL_LEVERAGE: "B2_BULL",
            B2AllocMode.DEFENSE_INDEX: "B2_DEFENSE",
            B2AllocMode.CASH:          "B2_CASH",
        }.get(target.mode, "B2")

        dbm.save_market_log(
            date=str(_date.today()),
            nasdaq_ma20=None,
            regime=mode_label,
            scanner_score=None,
        )
        dbm.update_system_state("B2_ALLOC_MODE", target.mode.value)
        return target

    def check_b2_weekly_exit(self, symbol: str):
        return self._strategy_mgr.check_b2_weekly_exit(symbol)

    # ── 프리마켓 루프 위임 ───────────────────────────────────────────

    async def run_premarket_loop(
        self,
        scan_symbols: List[str],
        df_fetcher=None,
    ) -> None:
        """main.py asyncio.gather 태스크 — StrategyManager에 위임."""
        await self._strategy_mgr.run_premarket_loop(scan_symbols, df_fetcher)

    # ── 현황 요약 (텔레그램 /status용) ───────────────────────────────

    def status_summary(self, open_positions: List[Dict]) -> str:
        """
        현재 봇 상태 텍스트 생성.
        텔레그램 /status 명령어에서 호출.
        """
        mode     = self.current_mode.value
        group    = self.active_group
        syncing  = f"  ⏳ 동기화 대기 {self._strategy_mgr.sync_remaining:.0f}초" if self.is_syncing else ""
        b2_inner = ""
        if self.is_b2:
            b2_inner = f"\nB2 내부 모드: {self._strategy_mgr.b2_alloc_mode.value}"

        # 오늘 매매 성적
        trades_today = dbm.get_trades_today()
        closed   = [t for t in trades_today if t["result"] is not None]
        total_pnl = sum(t["result"] for t in closed) if closed else 0.0
        wins     = sum(1 for t in closed if t["result"] > 0)
        losses   = len(closed) - wins
        pnl_str  = f"{total_pnl*100:+.2f}%" if closed else "없음"

        # 진입 중 종목
        holding_syms = [p["symbol"] for p in open_positions] if open_positions else []
        holding_str  = ", ".join(holding_syms) if holding_syms else "없음"

        lines = [
            f"📊 <b>봇 상태 요약</b>",
            f"현재 모드: <b>{mode}</b>{syncing}{b2_inner}",
            f"활성 그룹: <b>{group}</b>",
            f"",
            f"📈 오늘 매매 ({len(closed)}건)",
            f"  수익 {wins}건 / 손실 {losses}건 / 누계 {pnl_str}",
            f"",
            f"💼 보유 종목: {holding_str}",
        ]
        return "\n".join(lines)
