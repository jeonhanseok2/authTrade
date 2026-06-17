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
        self._bar_handler   = None      # run() 에서 세팅 — 동적 구독용 재사용
        self._quote_handler = None

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

    def add_symbols(self, symbols: list[str]) -> None:
        """
        실행 중인 스트림에 신규 종목 동적 추가.

        watch set을 업데이트하고, 스트림이 실행 중이면 WebSocket 구독도 갱신.
        스트림 미시작 시 watch만 업데이트 → run() 시작 시 자동으로 포함.
        """
        new = [s.upper() for s in symbols if s.upper() not in self._watch]
        if not new:
            return
        self._watch.update(new)
        if self._stream is not None and self._bar_handler is not None:
            try:
                # 기존 핸들러(_bar_handler)를 재사용 — self._watch 참조하므로 새 종목 자동 처리
                self._stream.subscribe_bars(self._bar_handler, *new)
                self._stream.subscribe_quotes(self._quote_handler, *new)
                logging.info("[WS] 동적 구독 추가 %d종목: %s", len(new), new[:8])
            except Exception as exc:
                logging.warning("[WS] 동적 구독 실패 (다음 재시작 시 적용): %s", exc)
        else:
            logging.info("[WS] watch 추가 %d종목 (스트림 시작 전, 시작 시 구독됨): %s", len(new), new[:8])

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

        # add_symbols()에서 재사용할 수 있도록 인스턴스에 저장
        self._bar_handler   = _bar_handler
        self._quote_handler = _quote_handler

        self._stream.subscribe_bars(_bar_handler, *all_syms)
        self._stream.subscribe_quotes(_quote_handler, *all_syms)

        logging.info("[WS] 스트림 시작 — 진입감시: %d, Spread감시: %d", len(self._watch), len(self._hold))
        # stream.run()은 내부에서 asyncio.run()을 호출 → 이미 실행 중인 이벤트 루프와 충돌.
        # _run_forever()는 run()이 래핑하는 순수 async 메서드이므로 직접 await 가능.
        await self._stream._run_forever()

    async def stop(self) -> None:
        if self._stream:
            await self._stream.stop()
            logging.info("[WS] 스트림 종료")
