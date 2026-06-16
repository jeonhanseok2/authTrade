# strategy/spoof_detector.py
"""
스푸핑(Spoofing) 감지 — Level 2 호가창 잔량 급변 감지.

스푸핑: 대량 허위 주문을 넣었다 빠르게 취소해 가격을 조종하는 행위.
감지: 500ms 내 (매수잔량 - 매도잔량) / 전체잔량 변화율이 임계치 이상 → 1회 적발
3회 적발 시 당일 해당 심볼 블랙리스트 등재 → B3 진입 차단
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Dict, Set


class SpoofDetector:
    def __init__(
        self,
        window_ms:   int   = 500,   # 변화율 추적 윈도우 (밀리초)
        swing_threshold: float = 0.30,  # 30% 이상 잔량 불균형 스윙 = 1회 적발
        ban_count:   int   = 3,     # 적발 횟수 도달 시 블랙리스트
    ):
        self._window_ms      = window_ms
        self._swing_threshold = swing_threshold
        self._ban_count      = ban_count

        # 심볼별 최근 imbalance 기록: deque of (ts_ms, imbalance)
        self._history:    Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._detections: Dict[str, int]   = defaultdict(int)
        self._blacklist:  Set[str]         = set()
        self._day: str = ""

    def _reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._blacklist.clear()
            self._detections.clear()
            self._day = today

    def update(
        self,
        symbol:   str,
        bid_qty:  float,  # 매수 잔량 (총합)
        ask_qty:  float,  # 매도 잔량 (총합)
    ) -> bool:
        """
        호가 잔량 업데이트. 스푸핑 감지 시 True 반환.
        블랙리스트 등재 시 로그 출력.
        """
        self._reset_daily()
        sym = symbol.upper()

        if sym in self._blacklist:
            return False

        total = bid_qty + ask_qty
        if total <= 0:
            return False

        ts_ms     = time.time() * 1000
        imbalance = (bid_qty - ask_qty) / total  # -1(매도우위) ~ +1(매수우위)
        hist      = self._history[sym]
        hist.append((ts_ms, imbalance))

        # 500ms 윈도우 내 데이터만 추출
        cutoff  = ts_ms - self._window_ms
        window  = [(t, i) for t, i in hist if t >= cutoff]
        if len(window) < 2:
            return False

        swing = max(i for _, i in window) - min(i for _, i in window)
        if swing < self._swing_threshold:
            return False

        # 적발
        self._detections[sym] += 1
        count = self._detections[sym]
        logging.warning(
            "[Spoof] %s 적발 %d/%d — 잔량 스윙 %.1f%% (500ms)",
            sym, count, self._ban_count, swing * 100,
        )
        if count >= self._ban_count:
            self._blacklist.add(sym)
            logging.warning("[Spoof] %s 당일 블랙리스트 등재 (진입 차단)", sym)
            return True

        return False

    def is_blacklisted(self, symbol: str) -> bool:
        self._reset_daily()
        return symbol.upper() in self._blacklist
