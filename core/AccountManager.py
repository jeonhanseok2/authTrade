# core/AccountManager.py
"""
계좌 자금 관리 모듈 — A/B 그룹 로테이션 + Settled Cash 재확인

책임:
  1. A/B 그룹 로테이션 (홀수 날 Group A, 짝수 날 Group B)
  2. 모드 전환 시마다 Settled Cash 재확인 → 미결제 현금 진입 원천 차단
  3. 신뢰도 점수 기반 자금 배분 (≥90→전액, 70~89→절반, <70→금지)
  4. B2 ETF 로테이션 예산 관리

T+1 규정:
  Cash Account는 아직 결제 완료되지 않은 매도 대금으로 신규 매수 시
  프리라이딩(Freeriding) 위반이 발생합니다.
  레짐 모드 전환 시 반드시 on_mode_switch()를 호출하여 자금 상태를
  재확인해야 합니다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from core.bucket_capital import BucketCapitalManager


class AccountManager:
    """
    계좌 자금 관리자.

    BucketCapitalManager 위에서 브로커 API 호출을 통합하여
    항상 최신 결제 완료 현금 기준으로 진입 예산을 산출합니다.
    """

    def __init__(
        self,
        bucket_capital: BucketCapitalManager,
        notify:         Optional[Callable[[str], None]] = None,
    ) -> None:
        self._bc     = bucket_capital
        self._notify = notify or (lambda _: None)

    # ── 상태 조회 ─────────────────────────────────────────────────────

    @property
    def active_group(self) -> str:
        """오늘 활성 그룹 (A 또는 B)."""
        return self._bc.active_group

    @property
    def total_equity(self) -> float:
        return self._bc.total_equity

    @property
    def daily_group_equity(self) -> float:
        """A/B 모드: min(총자산/2, settled_cash). 일반 모드: 총자산."""
        return self._bc.daily_group_equity

    # ── 자금 산출 ─────────────────────────────────────────────────────

    def capital_for(self, bucket: str, score: int = 100) -> float:
        """
        신뢰도 점수 적용 진입 가능 자금.

        score ≥ 90  → 버킷 전액
        score 70~89 → 버킷 절반
        score  < 70 → 0 (진입 금지)
        """
        return self._bc.allocated_by_score(bucket, score)

    def capital_b2(self) -> float:
        """B2 버킷 기본 자금 (ETF 로테이션 예산)."""
        return self._bc.allocated("etf_swing")

    def capital_b3(self) -> float:
        """B3 버킷 기본 자금."""
        return self._bc.allocated("squeeze")

    # ── Settled Cash 갱신 ────────────────────────────────────────────

    async def refresh(self, broker) -> float:
        """
        총 자산 + Settled Cash 모두 브로커에서 재조회.
        monitor 루프에서 주기적으로 호출.

        Returns:
            결제 완료 현금 금액
        """
        try:
            acct   = await asyncio.to_thread(broker.get_account)
            equity = float(acct.get("portfolio_value") or 0)
            if equity > 0:
                self._bc.update_equity(equity)
        except Exception as exc:
            logging.warning("[AccountManager] 총 자산 조회 실패: %s", exc)

        return await self.refresh_settled_cash(broker)

    async def refresh_settled_cash(self, broker) -> float:
        """
        Settled Cash만 재조회 (모드 전환 시 필수).

        Cash Account T+1 규정: 미결제 자금으로 신규 진입 시 프리라이딩 위반.
        레짐 모드 전환 시 반드시 호출하여 가용 자금을 재확인합니다.
        """
        settled = 0.0
        try:
            if hasattr(broker, "get_settled_cash"):
                settled = await asyncio.to_thread(broker.get_settled_cash)
            else:
                acct    = await asyncio.to_thread(broker.get_account)
                settled = float(acct.get("cash") or 0)
        except Exception as exc:
            logging.warning("[AccountManager] Settled Cash 조회 실패: %s", exc)

        self._bc.update_settled_cash(settled)
        logging.info(
            "[AccountManager] Settled Cash: $%.0f | 그룹: %s | 가용: $%.0f",
            settled, self.active_group, self.daily_group_equity,
        )
        return settled

    async def on_mode_switch(self, broker) -> None:
        """
        레짐 모드 전환 직후 호출 — Settled Cash 재확인 + 텔레그램 보고.

        모드 전환 후 이전 모드 포지션이 미결제 상태일 수 있으므로
        신뢰할 수 있는 자금 베이스라인을 재수립합니다.
        """
        settled = await self.refresh_settled_cash(broker)
        msg = (
            f"💼 [자금 재확인] 모드 전환 후 Settled Cash 동기화\n"
            f"그룹: {self.active_group} | "
            f"Settled Cash: ${settled:,.0f} | "
            f"오늘 가용 자금: ${self.daily_group_equity:,.0f}"
        )
        self._notify(msg)

    # ── 수익률 기록 / 자산 갱신 ─────────────────────────────────────

    def record_return(self, bucket: str, return_pct: float) -> None:
        self._bc.record_return(bucket, return_pct)

    def update_equity(self, new_equity: float) -> None:
        self._bc.update_equity(new_equity)

    def check_and_rebalance(self) -> bool:
        return self._bc.check_and_rebalance()

    def summary(self) -> str:
        return self._bc.summary()
