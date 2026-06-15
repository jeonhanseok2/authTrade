# risk/guard.py
"""
거래 가드 — 서킷브레이커 + 계좌 레벨 리스크 차단.

추가:
  vix_rate_of_change_alert : VIX 전일 대비 변화율 >= 20% → 선제 차단
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

from settings import settings


@dataclass
class AccountState:
    equity:  float
    day_pnl: float  # 미실현(평가) + 실현 당일 손익


class TradingGuard:
    def __init__(self, get_index_pct=lambda: 0.0):
        self.get_index_pct    = get_index_pct
        self._cooldown_until: Optional[datetime] = None

    def market_halt(self) -> bool:
        """지수 -7% 급락 시 30분 거래 정지 서킷브레이커."""
        if self.get_index_pct() <= -7.0:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            return True
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            return True
        return False

    def can_enter(
        self,
        account:        AccountState,
        open_positions: int,
        order_value:    float,
    ) -> bool:
        """
        신규 진입 가능 여부.

        차단 조건:
          - 미실현 손익 기준 일손실 한도 초과 (-max_daily_loss%)
          - 단일 주문 금액 한도 초과
          - 최대 포지션 수 초과
        """
        if account.equity > 0 and (account.day_pnl / account.equity) <= -settings.max_daily_loss:
            return False
        if order_value > settings.max_order_value:
            return False
        if open_positions >= settings.max_positions:
            return False
        return True


def vix_rate_of_change_alert(
    current_vix: float,
    prev_vix:    float,
    threshold:   float = 0.20,
) -> bool:
    """
    VIX 변화율(RoC) 기반 선제 공포 감지.

    단순 절대값(30) 체크만으로는 감지하지 못하는
    '빠른 공포 확산'을 전일 대비 변화율로 선제 탐지.

    예) VIX 20 → 25 (+25%) → threshold(20%) 초과 → True

    Args:
        current_vix: 현재 VIX
        prev_vix:    전일(또는 이전) VIX
        threshold:   변화율 임계치 (기본 20%)

    Returns:
        True → VIX 급등 감지 (신규 진입 차단 권고)
    """
    if prev_vix <= 0 or current_vix <= 0:
        return False
    roc = (current_vix - prev_vix) / prev_vix
    return roc >= threshold
