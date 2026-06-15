# core/kill_switch.py
"""
인메모리 킬스위치 — 미실현 손익 기반 일손실 한도 차단.

- 미실현(평가) 손익이 일손실 한도 초과 시 즉시 킬
- 날짜가 바뀌면 자동 리셋 (자정 복구)
- 스레드 안전: Lock으로 보호
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date


@dataclass
class KillSwitch:
    daily_loss_limit_pct: float = 0.02  # 미실현 포함 일손실 한도 (-2%)

    _killed: bool = field(default=False, init=False, repr=False)
    _kill_date: date | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def update(self, equity: float, unrealized_pnl: float, today: date | None = None) -> bool:
        """
        미실현 손익 반영 후 킬스위치 상태 갱신.

        Args:
            equity:         현재 계좌 평가액
            unrealized_pnl: 미실현 손익 (음수 = 손실)
            today:          날짜 (테스트용 주입, 기본값 = 오늘)

        Returns:
            True → 킬 상태 (신규 진입 차단)
        """
        from datetime import date as date_cls
        today = today or date_cls.today()

        with self._lock:
            # 날짜 바뀌면 자동 리셋 (자정 복구)
            if self._kill_date and self._kill_date != today:
                self._killed = False
                self._kill_date = None

            if equity <= 0:
                return self._killed

            loss_pct = unrealized_pnl / equity
            if loss_pct <= -self.daily_loss_limit_pct:
                if not self._killed:
                    logging.warning(
                        "[KillSwitch] 일손실 한도 초과 — 미실현: %.2f%% / 한도: -%.2f%%",
                        loss_pct * 100, self.daily_loss_limit_pct * 100,
                    )
                self._killed = True
                self._kill_date = today

            return self._killed

    @property
    def is_killed(self) -> bool:
        with self._lock:
            return self._killed

    def force_reset(self) -> None:
        """긴급 수동 리셋 (운영자 직접 호출 전용)."""
        with self._lock:
            self._killed = False
            self._kill_date = None
        logging.info("[KillSwitch] 수동 리셋 완료")
