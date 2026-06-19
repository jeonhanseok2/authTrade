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
import threading as _threading
from dataclasses import dataclass
from datetime import date as _date
from typing import Callable, Dict, List, Optional

from core.MarketRegimeAnalyzer import MarketRegimeAnalyzer
from core.StrategyManager      import StrategyManager
from core.AccountManager       import AccountManager
from core.regime_engine        import MarketMode
from strategy.b2_allocation    import B2AllocMode, AllocationTarget
import storage.db_manager as dbm


# ─────────────────────────────────────────────────────────────────────
# B4 스나이퍼 모드 (콜옵션 버전) — 모듈 레벨 상수 & 타입
# ─────────────────────────────────────────────────────────────────────

_B4_OPT_UNIVERSE   = ["QQQ", "SPY"]   # 기초 자산 (옵션 매매 대상)
_B4O_INIT_SL       = 0.20             # 초기 손절 −20%
_B4O_BREAK_AT      = 0.30             # +30% → 본전 방어선
_B4O_PARTIAL_AT    = 0.50             # +50% → 50% 부분 익절
_B4O_TRAIL_DIST    = 0.15             # 와이드 트레일링 −15% from peak
_B4O_STEP1_AT      = 1.00             # +100% → 플로어 +70%
_B4O_STEP1_FLOOR   = 0.70
_B4O_STEP2_AT      = 2.00             # +200% → 플로어 +150%
_B4O_STEP2_FLOOR   = 1.50
_B4O_CAPITAL       = 0.30             # 예수금 30% 사용 (버킷과 자금 중복 방지)
_B4O_RVOL_MIN      = 2.50             # 기초자산 RVOL ≥ 250%
_B4O_ENTRY_H       = 10               # 10:00 ET 이후 진입
_B4O_ENTRY_M       = 0
_B4O_EOD_H         = 15               # 15:30 ET 타임스탑
_B4O_EOD_M         = 30
_B4O_POLL_S        = 2                # 청산 체크 주기 (초)
_B4O_SCAN_S        = 10               # 진입 스캔 주기 (초)
_B4O_CONSEC_LOSS   = 3                # 연속 손절일 → 쿨다운 트리거
_B4O_COOLDOWN_DAYS = 2                # 쿨다운 기간 (거래일)


@dataclass
class _B4Position:
    """B4 스나이퍼 포지션 상태 — 옵션 계약 추적."""
    opt_symbol:    str    # OCC 옵션 심볼 (예: "QQQ240621C00490000")
    underlying:    str    # 기초 자산 ("QQQ" or "SPY")
    entry_price:   float  # 계약당 진입 프리미엄 ($)
    qty_total:     int    # 최초 계약 수
    qty_remaining: int    # 잔여 계약 수 (부분 익절 후 감소)
    stop_price:    float  # 현재 동적 스탑가 (프리미엄 기준, 래칫)
    peak_price:    float  # 진입 후 최고 프리미엄 (트레일링 기준)
    partial_taken: bool   # 50% 부분 익절 실행 여부
    entry_time:    object # datetime (zoneinfo ET)
    trade_date:    str    # YYYY-MM-DD (ET, 쿨다운 계산 기준)


# ── 옵션 데이터 클라이언트 싱글턴 ────────────────────────────────────
_OPT_DATA_CLIENT      = None
_OPT_DATA_CLIENT_LOCK = _threading.Lock()


def _get_opt_data_client():
    """OptionHistoricalDataClient 싱글턴 (lazy init, thread-safe)."""
    global _OPT_DATA_CLIENT
    if _OPT_DATA_CLIENT is not None:
        return _OPT_DATA_CLIENT
    with _OPT_DATA_CLIENT_LOCK:
        if _OPT_DATA_CLIENT is None:
            import os
            try:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                _OPT_DATA_CLIENT = OptionHistoricalDataClient(
                    os.getenv("ALPACA_API_KEY", ""),
                    os.getenv("ALPACA_SECRET_KEY", ""),
                )
                logging.info("[B4] OptionHistoricalDataClient 초기화 완료")
            except Exception as exc:
                logging.warning("[B4] OptionHistoricalDataClient 초기화 실패: %s", exc)
    return _OPT_DATA_CLIENT

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


# ─────────────────────────────────────────────────────────────────────
# B4 스나이퍼 모드 (콜옵션 버전) — 독립 비동기 태스크
# ─────────────────────────────────────────────────────────────────────

async def run_b4_sniper_mode(
    broker,
    notify: Optional[Callable[[str], None]] = None,
) -> None:
    """
    B4 스나이퍼 모드 (콜옵션 버전) — 완전 독립 비동기 태스크.

    기초 자산  : QQQ / SPY
    매매 대상  : 0DTE~2DTE ATM 콜옵션 계약
    진입 필터  : 10:00 ET 이후 + 기초자산 RVOL ≥ 250% + VIX 전일대비 하락/보합
    포지션 규모: 예수금의 50%  (나머지 50% 현금 유지)
    청산 규칙  : −20% 초기손절 / +30% 본전 / +50% 절반 익절 / 와이드 트레일링 −15%
                 +100% 플로어 +70% / +200% 플로어 +150% / 타임스탑 15:30 ET
    """
    import asyncio
    import time as _time_mod
    import zoneinfo
    from datetime import datetime, date as _date_cls, timedelta as _td
    from data.alpaca_bars import fetch_bars

    ET      = zoneinfo.ZoneInfo("America/New_York")
    _notify = notify or (lambda _: None)

    logging.info("[B4] 스나이퍼 모드 (콜옵션) 시작 — 기초자산: %s", _B4_OPT_UNIVERSE)

    # ── 런타임 상태 ─────────────────────────────────────────────────
    pos: Optional[_B4Position] = None
    avg_vols: Dict[str, float] = {}   # underlying → 20일 평균 5분봉 거래량
    _vol_ts = 0.0
    _vix_cache: dict = {}

    _FETCH_TIMEOUT = 20.0   # 개별 외부 I/O 타임아웃(초)

    # ── 1. VIX 필터 (5분 캐시) ──────────────────────────────────────
    async def _vix_ok() -> bool:
        """VIX 전일 대비 하락/보합 여부. 데이터 실패 시 진입 허용."""
        now_m = _time_mod.monotonic()
        if _vix_cache.get("ts", 0) and now_m - _vix_cache["ts"] < 300:
            return _vix_cache.get("last", 0) <= _vix_cache.get("prev", float("inf"))
        try:
            import yfinance as yf
            df = await asyncio.wait_for(
                asyncio.to_thread(
                    yf.download, "^VIX", period="5d", interval="1d",
                    progress=False, auto_adjust=True,
                ),
                timeout=_FETCH_TIMEOUT,
            )
            if df is not None and len(df) >= 2:
                close_arr = df["Close"].to_numpy().flatten()
                prev = float(close_arr[-2])
                last = float(close_arr[-1])
                _vix_cache.update({"prev": prev, "last": last, "ts": now_m})
                ok = last <= prev
                logging.info("[B4] VIX 전일%.2f → 현재%.2f → %s",
                             prev, last, "롱허용" if ok else "롱차단")
                return ok
        except asyncio.TimeoutError:
            logging.warning("[B4] VIX 조회 타임아웃(%ds) — 허용(방어)", int(_FETCH_TIMEOUT))
        except Exception as exc:
            logging.warning("[B4] VIX 조회 실패: %s — 허용(방어)", exc)
        return True

    # ── 2. 기초자산 평균 5분봉 거래량 갱신 (1시간마다) ──────────────
    async def _refresh_avg_vols() -> None:
        logging.info("[B4] 평균 거래량 갱신 시작 — %s", _B4_OPT_UNIVERSE)
        for sym in _B4_OPT_UNIVERSE:
            try:
                df = await asyncio.wait_for(
                    asyncio.to_thread(fetch_bars, sym, "5Min", 20 * 78),
                    timeout=_FETCH_TIMEOUT,
                )
                if df is not None and not df.empty:
                    avg_vols[sym] = float(df["volume"].mean())
                    logging.info("[B4] %s 평균5분봉거래량=%.0f", sym, avg_vols[sym])
                else:
                    logging.warning("[B4] %s 거래량 데이터 없음 (장 외 또는 API 미응답)", sym)
            except asyncio.TimeoutError:
                logging.warning("[B4] %s 거래량 갱신 타임아웃(%ds)", sym, int(_FETCH_TIMEOUT))
            except Exception as exc:
                logging.warning("[B4] %s 거래량 갱신 실패: %s", sym, exc)

    # ── 3. 기초자산 현재가 (최신 1분봉 종가) ────────────────────────
    async def _spot_price(underlying: str) -> float:
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(fetch_bars, underlying, "1Min", 2),
                timeout=_FETCH_TIMEOUT,
            )
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        except asyncio.TimeoutError:
            logging.warning("[B4] %s 현재가 조회 타임아웃", underlying)
        except Exception:
            pass
        return 0.0

    # ── 4. 기초자산 현재 5분봉 거래량 ───────────────────────────────
    async def _cur_5min_vol(underlying: str) -> float:
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(fetch_bars, underlying, "5Min", 2),
                timeout=_FETCH_TIMEOUT,
            )
            if df is not None and not df.empty:
                return float(df["volume"].iloc[-1])
        except asyncio.TimeoutError:
            logging.warning("[B4] %s 5분봉거래량 조회 타임아웃", underlying)
        except Exception:
            pass
        return 0.0

    # ── 5. 0DTE~2DTE ATM 콜옵션 심볼 조회 ──────────────────────────
    async def _find_atm_call(underlying: str, spot: float) -> Optional[str]:
        """Alpaca 옵션체인 API로 ATM 콜옵션 OCC 심볼 반환."""
        today      = _date_cls.today()
        exp_cutoff = today + _td(days=2)
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            req  = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_gte=str(today),
                expiration_date_lte=str(exp_cutoff),
                type="call",
                status="active",
            )
            resp      = await asyncio.to_thread(broker.client.get_option_contracts, req)
            contracts = getattr(resp, "option_contracts", None) or []

            best_sym  = None
            best_diff = float("inf")
            for c in contracts:
                try:
                    diff = abs(float(c.strike_price) - spot)
                    if diff < best_diff:
                        best_diff = diff
                        best_sym  = c.symbol
                except Exception:
                    continue

            if best_sym:
                logging.info("[B4] %s ATM 콜 선택: %s (스팟$%.2f, 행사가 차이$%.2f)",
                             underlying, best_sym, spot, best_diff)
            return best_sym
        except Exception as exc:
            logging.warning("[B4] %s 옵션체인 조회 실패: %s", underlying, exc)
            return None

    # ── 6. 옵션 프리미엄 실시간 조회 ((bid+ask)/2) ───────────────────
    async def _opt_price(opt_symbol: str) -> float:
        """OptionHistoricalDataClient로 옵션 최신 중간가 조회."""
        client = _get_opt_data_client()
        if client is None:
            return 0.0
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest
            req  = OptionLatestQuoteRequest(symbol_or_symbols=opt_symbol)
            resp = await asyncio.to_thread(client.get_option_latest_quote, req)

            # dict-like 또는 attr-like 응답 처리
            if hasattr(resp, "get"):
                quote = resp.get(opt_symbol)
            else:
                try:
                    quote = resp[opt_symbol]
                except (KeyError, TypeError):
                    quote = getattr(resp, opt_symbol, None)

            if quote:
                bid = float(getattr(quote, "bid_price", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
                return float(ask or bid)
        except Exception as exc:
            logging.debug("[B4] 옵션 프리미엄 조회 실패 %s: %s", opt_symbol, exc)
        return 0.0

    # ── 7. 동적 스탑가 산출 (래칫: 절대 내려가지 않음) ─────────────
    def _compute_stop(p: _B4Position, current: float) -> float:
        """
        현재 프리미엄 기준 스탑가 산출.
        반환값 ≥ p.stop_price (래칫 보장).
        """
        entry = p.entry_price
        peak  = p.peak_price            # 이미 갱신된 최고가
        pnl   = (current - entry) / entry if entry > 0 else 0.0

        if not p.partial_taken:
            # [1단계] 초기 손절(-20%) → 본전 방어선(+30% 이상)
            floor = entry if pnl >= _B4O_BREAK_AT else entry * (1.0 - _B4O_INIT_SL)
        else:
            # [2단계] 와이드 트레일링 — 부분 익절(+50%) 이후
            if pnl >= _B4O_STEP2_AT:                           # +200%
                step_floor = entry * (1.0 + _B4O_STEP2_FLOOR)
            elif pnl >= _B4O_STEP1_AT:                         # +100%
                step_floor = entry * (1.0 + _B4O_STEP1_FLOOR)
            else:
                step_floor = 0.0                               # 플로어 미활성

            trailing = peak * (1.0 - _B4O_TRAIL_DIST)          # peak 기준 −15%
            floor    = max(step_floor, trailing)

        return max(floor, p.stop_price)     # 래칫

    # ── 8. 청산 신호 판단 ────────────────────────────────────────────
    def _exit_signal(
        p: _B4Position, price: float, now_et: datetime
    ) -> tuple:
        """
        Returns (do_exit: bool, exit_qty: int, reason: str).
        exit_qty == p.qty_remaining → 전량 청산.
        exit_qty <  p.qty_remaining → 부분 익절 (50%).
        """
        # 타임스탑 15:30 ET
        if now_et.hour > _B4O_EOD_H or (
            now_et.hour == _B4O_EOD_H and now_et.minute >= _B4O_EOD_M
        ):
            return True, p.qty_remaining, "타임스탑 15:30 ET"

        pnl = (price - p.entry_price) / p.entry_price if p.entry_price > 0 else 0.0

        # 초기 손절 (−20%)
        if pnl <= -_B4O_INIT_SL:
            return True, p.qty_remaining, f"초기손절 {pnl*100:+.1f}%"

        # 동적 스탑 터치 (전량)
        if price <= p.stop_price and p.stop_price > 0:
            if p.stop_price < p.entry_price:
                label = "손절"
            elif abs(p.stop_price - p.entry_price) / p.entry_price < 0.005:
                label = "본전청산"
            else:
                label = "트레일링청산"
            return (True, p.qty_remaining,
                    f"{label} PnL{pnl*100:+.1f}% (스탑${p.stop_price:.2f})")

        # 50% 부분 익절 (미실행 시 1회만)
        if not p.partial_taken and pnl >= _B4O_PARTIAL_AT:
            partial_qty = max(1, p.qty_total // 2)
            return (True, partial_qty,
                    f"부분익절+{_B4O_PARTIAL_AT*100:.0f}% ({partial_qty}계약)")

        return False, 0, ""

    # ── 9. 진입 실행 ─────────────────────────────────────────────────
    async def _enter(
        underlying: str, opt_sym: str, premium: float,
        qty: int, now_et: datetime,
    ) -> Optional[_B4Position]:
        trade_date = now_et.strftime("%Y-%m-%d")
        stop0 = premium * (1.0 - _B4O_INIT_SL)
        try:
            await asyncio.to_thread(
                broker.submit_order,
                symbol=opt_sym, qty=qty, side="buy", type="market",
            )
            cost = premium * qty * 100  # 1계약 = 100주
            _notify(
                f"📡 [B4/스나이퍼] {underlying} 콜옵션 매수\n"
                f"  계약: {opt_sym}\n"
                f"  수량: {qty}계약 @ ${premium:.2f}\n"
                f"  초기손절: ${stop0:.2f} (−20%)\n"
                f"  자본 투입: ${cost:,.0f}"
            )
            logging.info("[B4] 진입 %s %d계약 @ $%.2f 스탑$%.2f",
                         opt_sym, qty, premium, stop0)
            return _B4Position(
                opt_symbol=opt_sym, underlying=underlying,
                entry_price=premium,
                qty_total=qty, qty_remaining=qty,
                stop_price=stop0, peak_price=premium,
                partial_taken=False,
                entry_time=now_et, trade_date=trade_date,
            )
        except Exception as exc:
            logging.error("[B4] %s 매수 주문 실패: %s", opt_sym, exc)
            return None

    # ── 10. 청산 실행 (전량 또는 부분) ──────────────────────────────
    async def _exit_pos(
        p: _B4Position, price: float, exit_qty: int, reason: str,
    ) -> None:
        pnl_pct = (price - p.entry_price) / p.entry_price if p.entry_price > 0 else 0.0
        pnl_usd = (price - p.entry_price) * exit_qty * 100
        try:
            await asyncio.to_thread(
                broker.submit_order,
                symbol=p.opt_symbol, qty=exit_qty, side="sell", type="market",
            )
            icon = "✅" if pnl_usd >= 0 else "🔴"
            _notify(
                f"{icon} [B4/스나이퍼] {p.underlying} 청산\n"
                f"  계약: {p.opt_symbol}\n"
                f"  {exit_qty}계약 @ ${price:.2f}\n"
                f"  PnL {pnl_pct*100:+.1f}% (${pnl_usd:+,.0f})\n"
                f"  사유: {reason}"
            )
            logging.info("[B4] 청산 %s %d계약 @ $%.2f PnL%+.1f%% — %s",
                         p.opt_symbol, exit_qty, price, pnl_pct * 100, reason)

            # DB 기록 (완성된 거래만 저장)
            await asyncio.to_thread(
                dbm.save_b4_trade,
                p.opt_symbol, p.entry_price, price,
                exit_qty, pnl_pct, reason, p.trade_date,
            )

            # 전량 청산 후 쿨다운 체크
            if exit_qty >= p.qty_remaining:
                await asyncio.to_thread(_check_cooldown)

        except Exception as exc:
            logging.error("[B4] %s 청산 주문 실패: %s", p.opt_symbol, exc)

    # ── 11. 쿨다운 트리거 판단 ──────────────────────────────────────
    def _check_cooldown() -> None:
        consec = dbm.get_b4_consecutive_loss_days()
        if consec >= _B4O_CONSEC_LOSS and not dbm.is_b4_cooldown_active():
            reason = (f"{_B4O_CONSEC_LOSS}거래일 연속 손절 "
                      f"→ {_B4O_COOLDOWN_DAYS}거래일 휴식")
            dbm.set_b4_cooldown(reason=reason, trading_days=_B4O_COOLDOWN_DAYS)
            _notify(
                f"⏸ [B4] 쿨다운 활성\n"
                f"  {_B4O_CONSEC_LOSS}일 연속 손절 감지\n"
                f"  → {_B4O_COOLDOWN_DAYS}거래일 스캔 중지"
            )
            logging.warning("[B4] 쿨다운 설정: %s", reason)

    # ── 초기화: 평균 거래량 선계산 ──────────────────────────────────
    await _refresh_avg_vols()
    _vol_ts = _time_mod.monotonic()

    # ═══════════════════════════════════════════════════════════════
    # 메인 루프
    # ═══════════════════════════════════════════════════════════════
    while True:
        try:
            now_et     = datetime.now(ET)
            loop_start = _time_mod.monotonic()

            # 평균 거래량 1시간마다 갱신
            if _time_mod.monotonic() - _vol_ts > 3600:
                await _refresh_avg_vols()
                _vol_ts = _time_mod.monotonic()

            # 장 시간 외: 포지션 없으면 대기
            market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
            market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
            if now_et < market_open:
                await asyncio.sleep(30)
                continue
            if now_et >= market_close and pos is None:
                await asyncio.sleep(60)
                continue

            # ─────────────────────────────────────────────────────
            # [포지션 보유 중] 청산 체크 — 2초 주기
            # ─────────────────────────────────────────────────────
            if pos is not None:
                price = await _opt_price(pos.opt_symbol)
                if price > 0:
                    # peak 갱신
                    if price > pos.peak_price:
                        pos.peak_price = price

                    # 스탑 래칫
                    new_stop = _compute_stop(pos, price)
                    if new_stop > pos.stop_price:
                        logging.info(
                            "[B4] %s 스탑 $%.2f → $%.2f (현가$%.2f PnL%+.1f%%)",
                            pos.opt_symbol, pos.stop_price, new_stop, price,
                            (price - pos.entry_price) / pos.entry_price * 100,
                        )
                        pos.stop_price = new_stop

                    # 청산 판단
                    do_exit, exit_qty, reason = _exit_signal(pos, price, now_et)
                    if do_exit:
                        is_partial = (exit_qty < pos.qty_remaining)
                        await _exit_pos(pos, price, exit_qty, reason)
                        if is_partial:
                            # 부분 익절 → 잔여 포지션으로 와이드 트레일링 전환
                            pos.qty_remaining -= exit_qty
                            pos.partial_taken  = True
                            logging.info(
                                "[B4] %s 부분익절 후 잔여 %d계약 → 와이드 트레일링",
                                pos.opt_symbol, pos.qty_remaining,
                            )
                        else:
                            pos = None

                await asyncio.sleep(_B4O_POLL_S)
                continue

            # ─────────────────────────────────────────────────────
            # [포지션 없음] 진입 스캔 — 10초 주기
            # ─────────────────────────────────────────────────────

            # B4 활성화 여부 (텔레그램 /set_b4 off 로 런타임 차단 가능)
            if dbm.get_system_state("B4_ENABLED", "on") != "on":
                logging.debug("[B4] 비활성 상태 — 스캔 스킵")
                await asyncio.sleep(_B4O_SCAN_S)
                continue

            # 쿨다운 체크
            if await asyncio.to_thread(dbm.is_b4_cooldown_active):
                logging.debug("[B4] 쿨다운 중 — 스캔 스킵")
                await asyncio.sleep(_B4O_SCAN_S)
                continue

            # 진입 허용 시간: 10:00 ~ 15:15 ET
            entry_start  = now_et.replace(
                hour=_B4O_ENTRY_H, minute=_B4O_ENTRY_M, second=0, microsecond=0
            )
            entry_cutoff = now_et.replace(
                hour=_B4O_EOD_H, minute=15, second=0, microsecond=0
            )
            if now_et < entry_start or now_et >= entry_cutoff:
                await asyncio.sleep(_B4O_SCAN_S)
                continue

            # VIX 필터
            if not await _vix_ok():
                logging.info("[B4] VIX 필터 차단 — 진입 스킵")
                await asyncio.sleep(_B4O_SCAN_S)
                continue

            # 기초자산별 스캔
            if not any(avg_vols.get(u, 0) > 0 for u in _B4_OPT_UNIVERSE):
                logging.info("[B4] 평균 거래량 미준비 — 스캔 스킵 (장 외 또는 초기화 중)")
                await asyncio.sleep(_B4O_SCAN_S)
                continue

            for underlying in _B4_OPT_UNIVERSE:
                avg_v = avg_vols.get(underlying, 0)
                if avg_v <= 0:
                    continue

                # RVOL 필터 (기초자산 기준)
                cur_vol = await _cur_5min_vol(underlying)
                rvol    = cur_vol / avg_v if avg_v > 0 else 0.0
                if rvol < _B4O_RVOL_MIN:
                    logging.debug("[B4] %s RVOL=%.1fx < %.0f%% — 스킵",
                                  underlying, rvol, _B4O_RVOL_MIN * 100)
                    continue

                # 기초자산 현재가
                spot = await _spot_price(underlying)
                if spot <= 0:
                    continue

                # ATM 콜옵션 심볼 조회
                opt_sym = await _find_atm_call(underlying, spot)
                if not opt_sym:
                    logging.warning("[B4] %s ATM 콜옵션 없음 — 스킵", underlying)
                    continue

                # 옵션 현재 프리미엄
                premium = await _opt_price(opt_sym)
                if premium <= 0:
                    logging.warning("[B4] %s 프리미엄 조회 실패 — 스킵", opt_sym)
                    continue

                # 예수금 × B4_CAPITAL% → 계약 수 산출 (1계약 = 프리미엄 × 100)
                try:
                    settled = await asyncio.to_thread(broker.get_settled_cash)
                except Exception:
                    try:
                        acct    = await asyncio.to_thread(broker.get_account)
                        settled = float(acct.get("cash", 0))
                    except Exception:
                        logging.warning("[B4] 예수금 조회 실패 — 스킵")
                        continue

                try:
                    _cap = float(dbm.get_system_state("B4_CAPITAL", str(_B4O_CAPITAL)))
                    _cap = max(0.05, min(0.80, _cap))   # 5%~80% 범위 강제
                except Exception:
                    _cap = _B4O_CAPITAL
                budget = settled * _cap
                qty    = int(budget // (premium * 100))
                if qty <= 0:
                    logging.info(
                        "[B4] %s 예수금 부족 (settled=$%.0f, budget=$%.0f, "
                        "premium=$%.2f×100) — 스킵",
                        underlying, settled, budget, premium,
                    )
                    continue

                logging.info(
                    "[B4] %s 진입 신호! RVOL=%.1fx | %s $%.2f × %d계약",
                    underlying, rvol, opt_sym, premium, qty,
                )
                pos = await _enter(underlying, opt_sym, premium, qty, now_et)
                if pos:
                    break   # 1포지션만 진입

            elapsed    = _time_mod.monotonic() - loop_start
            await asyncio.sleep(max(0.0, _B4O_SCAN_S - elapsed))

        except asyncio.CancelledError:
            logging.info("[B4] 태스크 취소 — 강제 청산 시도")
            if pos is not None:
                price = await _opt_price(pos.opt_symbol)
                if price > 0:
                    await _exit_pos(pos, price, pos.qty_remaining, "태스크취소-강제청산")
                pos = None
            return
        except Exception as exc:
            logging.error("[B4] 루프 오류: %s", exc, exc_info=True)
            await asyncio.sleep(5)
