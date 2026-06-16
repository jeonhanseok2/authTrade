# core/session_watchdog.py
"""
Toss 세션 감시 워커.

Toss는 공식 API가 없어 세션 토큰 만료나 서버 점검 시 예고 없이 끊김.
포지션 보유 중 연결 끊기면 청산 로직이 멈춤 → 치명적 손실 가능.

대응:
  - 30초마다 get_account() 헬스체크
  - 3회 연속 실패 → 킬스위치 강제 발동 + 텔레그램 알림
  - 연결 복구 시 자동 킬스위치 해제 (자정 리셋 대기)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional


async def run_session_watchdog(
    broker,
    kill_switch,
    notifier=None,
    interval: int = 30,
    fail_threshold: int = 3,
) -> None:
    """
    Toss 세션 감시 — BROKER=toss 일 때만 main.py에서 실행.

    Args:
        broker:         TossInvestBroker 인스턴스
        kill_switch:    KillSwitch 인스턴스
        notifier:       텔레그램 노티파이어 (없으면 로그만)
        interval:       헬스체크 주기 (초, 기본 30)
        fail_threshold: 이 횟수 연속 실패 시 킬스위치 발동
    """
    consecutive_fails = 0
    logging.info("[Watchdog] Toss 세션 감시 시작 (interval=%ds, threshold=%d)", interval, fail_threshold)

    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(broker.get_account)
            if consecutive_fails > 0:
                logging.info("[Watchdog] 세션 복구 (연속실패 %d → 0)", consecutive_fails)
            consecutive_fails = 0
        except Exception as exc:
            consecutive_fails += 1
            logging.error("[Watchdog] 세션 오류 (%d회 연속): %s", consecutive_fails, exc)

            if consecutive_fails >= fail_threshold:
                kill_switch.force_kill()
                msg = (
                    f"🚨 [세션 단절] Toss API {consecutive_fails}회 연속 응답 없음\n"
                    f"신규 진입 차단됨 — 보유 포지션 수동 확인 필요\n"
                    f"오류: {exc}"
                )
                logging.critical("[Watchdog] 킬스위치 발동: %s", msg)
                if notifier:
                    try:
                        notifier.send(msg)
                    except Exception:
                        pass
