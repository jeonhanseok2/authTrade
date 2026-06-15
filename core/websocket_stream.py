# core/websocket_stream.py
"""
버킷 3 전용 — Alpaca WebSocket 실시간 스트림.

구독 채널:
  Bars   (1분봉) : Gap&Go + TTM Squeeze 진입 신호 감지
  Quotes (호가)  : 보유 포지션 Bid-Ask Spread 실시간 감시 (Level2 대용)

이벤트 흐름:
  bar_handler   → watch_symbols 목록 종목만 처리 → 진입 신호 콜백
  quote_handler → hold_symbols 목록 종목만 처리 → Spread 탈출 콜백
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Set


# Spread 탈출 임계치 — Bid-Ask 스프레드 >= 1.5% 면 즉시 탈출
SPREAD_EXIT_PCT = 0.015


class Bucket3Stream:
    def __init__(
        self,
        on_bar:   Callable,   # async (symbol: str, bar) -> None
        on_quote: Callable,   # async (symbol: str, bid: float, ask: float) -> None
    ):
        self._on_bar      = on_bar
        self._on_quote    = on_quote
        self._stream      = None
        self._watch: Set[str] = set()   # 진입 감시 종목 (후보)
        self._hold:  Set[str] = set()   # 보유 포지션 (Spread 감시)

    # ── 종목 등록/해제 ────────────────────────────────────────────────

    def watch(self, symbols: list[str]) -> None:
        """프리마켓 스캔 후보 등록 — bar 이벤트 처리 대상."""
        self._watch.update(symbols)
        logging.debug("[WS] watch 추가: %s", symbols)

    def hold(self, symbols: list[str]) -> None:
        """보유 포지션 등록 — quote spread 감시 대상."""
        self._hold.update(symbols)

    def release(self, symbol: str) -> None:
        """포지션 청산 후 감시 해제."""
        self._hold.discard(symbol)
        self._watch.discard(symbol)

    # ── 스트림 시작/종료 ──────────────────────────────────────────────

    async def run(self) -> None:
        """WebSocket 스트림 시작 — 종목 없으면 대기 후 재시도."""
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret  = os.getenv("ALPACA_SECRET_KEY", "")
        if not api_key or not secret:
            logging.error("[WS] ALPACA_API_KEY / ALPACA_SECRET_KEY 미설정")
            return

        try:
            from alpaca.data.live import StockDataStream  # type: ignore
        except ImportError:
            logging.error("[WS] alpaca-py 패키지 필요: pip install alpaca-py>=0.35.0")
            return

        # 종목이 없으면 30초마다 재확인
        while not (self._watch or self._hold):
            logging.info("[WS] 감시 종목 없음 — 30초 후 재시도")
            await asyncio.sleep(30)

        self._stream = StockDataStream(api_key, secret)
        all_syms = list(self._watch | self._hold)

        async def _bar_handler(bar):
            sym = getattr(bar, "symbol", "")
            if sym in self._watch:
                try:
                    await self._on_bar(sym, bar)
                except Exception as exc:
                    logging.error("[WS] bar_handler 예외 (%s): %s", sym, exc)

        async def _quote_handler(quote):
            sym = getattr(quote, "symbol", "")
            if sym not in self._hold:
                return
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid > 0 and ask > 0:
                try:
                    await self._on_quote(sym, bid, ask)
                except Exception as exc:
                    logging.error("[WS] quote_handler 예외 (%s): %s", sym, exc)

        self._stream.subscribe_bars(_bar_handler, *all_syms)
        self._stream.subscribe_quotes(_quote_handler, *all_syms)

        logging.info("[WS] 스트림 시작 — 진입감시: %d, Spread감시: %d", len(self._watch), len(self._hold))
        await self._stream.run()

    async def stop(self) -> None:
        if self._stream:
            await self._stream.stop()
            logging.info("[WS] 스트림 종료")
