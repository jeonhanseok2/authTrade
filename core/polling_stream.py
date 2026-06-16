# core/polling_stream.py
"""
토스증권용 1초 폴링 스트림 — Alpaca WebSocket 대체.

Alpaca StockDataStream 대비 차이:
  - 1초 간격 REST 폴링 (WebSocket 없음)
  - on_bar  : 1분봉이 확정될 때마다 콜백 (분 변경 감지)
  - on_quote: 1초마다 호가(bid/ask) 콜백 → spread exit 감지

폴링 대상:
  _watch : 진입 후보 (bar 이벤트만)
  _hold  : 보유 포지션 (bar + quote 이벤트)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set


class PollingStream:
    POLL_INTERVAL = 1.0  # 초

    def __init__(
        self,
        broker,                          # TossInvestBroker
        on_bar:   Callable,              # async def on_bar(symbol, bar)
        on_quote: Callable,              # async def on_quote(symbol, bid, ask)
    ):
        self._broker   = broker
        self._on_bar   = on_bar
        self._on_quote = on_quote
        self._watch: Set[str] = set()   # 진입 후보 심볼
        self._hold:  Set[str] = set()   # 보유 포지션 심볼
        self._running = False
        # 마지막으로 처리한 분(minute) 추적 → 분 변경 시 bar 이벤트 발생
        self._last_minute: Dict[str, int] = {}
        # 마지막 bar 데이터 캐시
        self._last_bar: Dict[str, dict] = {}
        from strategy.spoof_detector import SpoofDetector
        self._spoof = SpoofDetector()

    # ── 심볼 관리 ─────────────────────────────────────────────────────

    def watch(self, symbols: list[str]) -> None:
        self._watch.update(s.upper() for s in symbols)
        logging.info("[Polling] watch 추가: %s", symbols)

    def hold(self, symbols: list[str]) -> None:
        self._hold.update(s.upper() for s in symbols)
        logging.info("[Polling] hold 추가: %s", symbols)

    def release(self, symbol: str) -> None:
        sym = symbol.upper()
        self._hold.discard(sym)
        self._last_minute.pop(sym, None)
        self._last_bar.pop(sym, None)
        logging.info("[Polling] hold 해제: %s", sym)

    # ── 실행 / 정지 ───────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logging.info("[Polling] 1초 폴링 스트림 시작")
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logging.warning("[Polling] 폴링 오류: %s", exc)
            await asyncio.sleep(self.POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        logging.info("[Polling] 스트림 정지")

    def is_spoof_blacklisted(self, symbol: str) -> bool:
        return self._spoof.is_blacklisted(symbol)

    # ── 폴링 사이클 ───────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        all_symbols = list(self._watch | self._hold)
        if not all_symbols:
            return

        now    = datetime.now(timezone.utc)
        minute = now.minute

        # 현재가 일괄 조회 (최대 200개 — Toss 제한)
        prices: Dict[str, float] = {}
        try:
            prices = await asyncio.to_thread(self._broker.get_prices, all_symbols)
        except Exception as exc:
            logging.debug("[Polling] 시세 조회 실패: %s", exc)
            return

        for sym in all_symbols:
            price = prices.get(sym, 0.0)
            if price <= 0:
                continue

            # ── bar 이벤트: 분 변경 감지 ──────────────────────────
            last_min = self._last_minute.get(sym, -1)
            if minute != last_min:
                self._last_minute[sym] = minute
                bar = _make_bar(sym, price, now)
                self._last_bar[sym] = bar
                try:
                    await self._on_bar(sym, _BarProxy(bar))
                except Exception as exc:
                    logging.warning("[Polling] on_bar(%s) 오류: %s", sym, exc)

            # ── quote 이벤트: 보유 포지션만 (spread exit 감지) ──
            if sym in self._hold:
                try:
                    ob = await asyncio.to_thread(self._broker.get_orderbook, sym)
                    bid     = ob.get("bid", 0.0)
                    ask     = ob.get("ask", 0.0)
                    bid_qty = float(ob.get("bid_qty", 0) or 0)
                    ask_qty = float(ob.get("ask_qty", 0) or 0)
                    if bid > 0 and ask > 0:
                        # 스푸핑 감지 (잔량 데이터 있을 때만)
                        if bid_qty > 0 or ask_qty > 0:
                            self._spoof.update(sym, bid_qty, ask_qty)
                        await self._on_quote(sym, bid, ask)
                except Exception as exc:
                    logging.debug("[Polling] on_quote(%s) 오류: %s", sym, exc)


def _make_bar(symbol: str, close: float, ts: datetime) -> dict:
    return {
        "symbol":    symbol,
        "open":      close,
        "high":      close,
        "low":       close,
        "close":     close,
        "volume":    0,
        "timestamp": ts.isoformat(),
    }


class _BarProxy:
    """Alpaca Bar 객체와 같은 인터페이스 제공 (orchestrator 호환)."""
    def __init__(self, d: dict):
        self.symbol    = d["symbol"]
        self.open      = d["open"]
        self.high      = d["high"]
        self.low       = d["low"]
        self.close     = d["close"]
        self.volume    = d["volume"]
        self.timestamp = d["timestamp"]
