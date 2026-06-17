# core/orchestrator.py
"""
비동기 오케스트레이터 — 버킷별 독립 태스크.

태스크 구조:
  exit_task     : 전 버킷 포지션 청산 체크 — 30초 주기
  monitor_task  : 킬스위치 + VIX RoC + 리밸런싱 — 60초 주기
  bucket1_task  : 가치주 장기투자 — 60분 주기 (yfinance 무거운 API)
  bucket2_task  : ETF 스윙 — 15분 주기
  bucket3_ws    : 급등주 단타 — WebSocket 이벤트 기반 (실시간)

Panic 레짐 처리:
  - 전량 강제청산(구 로직) 삭제
  - B3(급등주) 즉시 청산 + B2에 인버스 ETF(SQQQ/SDS) 헤지 포지션 추가
  - B1(가치주)은 장기 관점 유지 (Panic 이후 저가 매수 기회)
"""
from __future__ import annotations

import asyncio
import logging
import pandas as pd
from datetime import datetime, date as _date, timezone
from typing import Any, Dict, List, Optional

from core.kill_switch    import KillSwitch
from core.bucket_capital import BucketCapitalManager
from core.websocket_stream import Bucket3Stream
from strategy.exits import (
    hard_stop_gap_down, effective_stop_price,
    take_profit_hit, trailing_stop_active,
    rsi_overbought_exit, eod_exit, bid_ask_spread_exit,
    breakeven_stop_hit, partial_exit_check,
)
from strategy.exit_strategy      import ExitStrategyEngine, ExitDecision
from strategy.confidence_scanner import ConfidenceScanner
from core.regime_engine          import RegimeEngine, MarketMode
from strategy.b2_allocation      import B2AllocationEngine, B2AllocMode
from core.MarketRegimeAnalyzer   import MarketRegimeAnalyzer
from core.StrategyManager        import StrategyManager
from core.AccountManager         import AccountManager
from strategy.news_analyzer      import NewsAnalyzer
from storage.db import PositionDB
import os
import storage.db_manager as dbm

# 인버스 ETF 헤지 종목
PANIC_HEDGE_ETFS = ["SQQQ", "SDS"]
# VIX 변화율 선제 차단 임계치 (전일 대비 20% 이상 급등)
VIX_ROC_THRESHOLD = 0.20

# 지정가 청산 슬리피지 (토스 시장가 슬리피지 방지)
_EXIT_LIMIT_SLIP = 0.003   # 일반 청산: 현재가 -0.3%
_STOP_LIMIT_SLIP = 0.005   # 손절/긴급: 현재가 -0.5%

# Paper Trading 슬리피지 시뮬레이션 (매수 +0.1%, 매도 -0.1%)
_PAPER_SLIP_BUY  = 0.001
_PAPER_SLIP_SELL = 0.001


class Orchestrator:
    def __init__(
        self,
        broker,
        data_client,
        db: PositionDB,
        cfg: Dict[str, Any],
        kill_switch: KillSwitch,
        bucket_capital: BucketCapitalManager,
        notifier=None,
    ):
        self.broker         = broker
        self.data_client    = data_client
        self.db             = db
        self.cfg            = cfg
        self.kill_switch    = kill_switch
        self.bucket_capital = bucket_capital
        self.notifier       = notifier

        self._prev_vix: float     = 0.0
        self._prev_regime: str    = "bull"
        self._stream: Optional[Bucket3Stream] = None
        self._hedge_active: bool  = False  # Panic 헤지 포지션 보유 중 여부

        # ── 저수준 엔진 (하위 호환 유지) ─────────────────────────────
        self.exit_engine   = ExitStrategyEngine(notify=self._notify)
        self.conf_scanner  = ConfidenceScanner()
        self.regime_engine = RegimeEngine(notify=self._notify)
        self.b2_alloc      = B2AllocationEngine(notify=self._notify)

        # ── 고수준 모듈 (3대 관심사 분리) ────────────────────────────
        # 뉴스 심리 분석기 (차트 점수 30% 보정)
        self.news_analyzer = NewsAnalyzer(notify=self._notify)

        # 시장 상태 진단: 프리마켓 스캔 → B3/B2 모드 결정
        self.regime_analyzer = MarketRegimeAnalyzer(
            regime_engine=self.regime_engine,
            conf_scanner=self.conf_scanner,
            notify=self._notify,
            news_analyzer=self.news_analyzer,
        )
        # 계좌 자금 관리: A/B 로테이션 + Settled Cash 재확인
        self.account_mgr = AccountManager(
            bucket_capital=bucket_capital,
            notify=self._notify,
        )
        # 전략 전환 엔진: B3/B2 모드 스위칭 + B2 리밸런싱 조율
        self.strategy_mgr = StrategyManager(
            analyzer=self.regime_analyzer,
            b2_alloc=self.b2_alloc,
            account_mgr=self.account_mgr,
            broker=broker,
            notify=self._notify,
        )
        # 종목별 3분 룰 체크 완료 여부 (포지션 청산 시 제거)
        self._3min_checked: set = set()
        # 당일 시가 캐시: symbol → (date, open_px) — 장 중 불변값 재조회 방지
        self._open_price_cache: Dict[str, tuple] = {}
        # B3 일간 갭/RVOL 캐시: symbol → (gap_pct, rvol, date_str)
        # on_bar()는 gap_pct=0/rvol=0 기본값이라 항상 진입 실패 → 여기서 실제 값 계산
        self._b3_daily_cache: Dict[str, tuple] = {}

    # ──────────────────────────────────────────────────────────────────
    # 공통 유틸
    # ──────────────────────────────────────────────────────────────────

    def _notify(self, msg: str) -> None:
        if self.notifier:
            try:
                self.notifier.send(msg)
            except Exception:
                pass

    def _get_b3_gap_data(self, symbol: str, df_intraday: pd.DataFrame) -> tuple:
        """
        B3 on_bar()용 갭업%·RVOL 계산 (종목당 하루 1회 캐시).

        on_bar()는 장중 실시간 bar이므로 gap_pct/rvol 정보가 없다.
        일봉 25개를 한 번만 조회해 전일 종가 대비 당일 시가 갭과
        장중 거래량을 전일 평균으로 나눈 RVOL을 산출, 하루 동안 재사용.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        cached = self._b3_daily_cache.get(symbol)
        if cached and cached[2] == today:
            return cached[0], cached[1]

        gap_pct = 0.0
        rvol    = 0.0
        try:
            daily = self._fetch_bars(symbol, "1Day", 25)
            if daily is not None and len(daily) >= 2:
                prev_close = float(daily["close"].iloc[-2])
                if prev_close > 0 and not df_intraday.empty:
                    today_open = float(df_intraday["open"].iloc[0])
                    gap_pct = (today_open - prev_close) / prev_close * 100.0

                avg_vol_20 = float(
                    daily["volume"].rolling(20, min_periods=5).mean().iloc[-1] or 1
                )
                intraday_vol = float(df_intraday["volume"].sum()) if not df_intraday.empty else 0.0
                bars_so_far  = max(len(df_intraday), 1)
                projected_vol = intraday_vol * (78.0 / bars_so_far)   # 78 = 390min ÷ 5min
                rvol = projected_vol / avg_vol_20 if avg_vol_20 > 0 else 0.0
        except Exception as exc:
            logging.debug("[B3] %s gap_data 산출 실패: %s", symbol, exc)

        self._b3_daily_cache[symbol] = (gap_pct, rvol, today)
        return gap_pct, rvol

    async def _get_account(self):
        from risk.guard import AccountState
        try:
            acct       = await asyncio.to_thread(self.broker.get_account)
            equity     = float(getattr(acct, "equity",       0) or 0)
            unrealized = float(getattr(acct, "unrealized_pl", 0) or 0)
            return AccountState(equity=equity, day_pnl=unrealized)
        except Exception as exc:
            logging.warning("[orchestrator] 계좌 조회 실패: %s", exc)
            return None

    def _is_tradeable(self) -> bool:
        """신규 진입 가능 여부 (킬스위치 + 데드존)."""
        if self.kill_switch.is_killed:
            return False
        dz = self.cfg.get("engine", {}).get("deadzone", {})
        if dz.get("enabled", True):
            from strategy.regime import is_deadzone
            if is_deadzone(
                datetime.now(timezone.utc),
                start_hour = int(dz.get("start_hour", 11)),
                start_min  = int(dz.get("start_min",  30)),
                end_hour   = int(dz.get("end_hour",   13)),
                end_min    = int(dz.get("end_min",     0)),
            ):
                return False
        return True

    def _is_toss(self) -> bool:
        return hasattr(self.broker, "get_candles")  # TossInvestBroker 판별

    def _is_paper(self) -> bool:
        return os.getenv("MODE", "paper").lower() == "paper"

    def _fetch_last(self, symbol: str) -> float:
        try:
            if self._is_toss():
                return self.broker.get_price(symbol)
            from alpaca.data.requests import StockLatestTradeRequest  # type: ignore
            resp = self.data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
            return float(resp[symbol].price)
        except Exception:
            return 0.0

    def _fetch_open_price(self, symbol: str) -> float:
        """당일 시가 조회 — Daily 봉 Open 기준 (프리마켓 폭락 반영, 1분봉 아님).

        결과를 당일 내 캐싱: 시가는 장 중 불변이므로 30초 exit 사이클마다 API 재호출 불필요.
        """
        import zoneinfo
        today_et = datetime.now(zoneinfo.ZoneInfo("America/New_York")).date()
        cached = self._open_price_cache.get(symbol)
        if cached and cached[0] == today_et:
            return cached[1]

        df = self._fetch_bars(symbol, "1Day", 3)
        if df is None or df.empty or "open" not in df.columns:
            return 0.0
        open_px = float(df.iloc[-1]["open"])
        self._open_price_cache[symbol] = (today_et, open_px)
        return open_px

    def _fetch_atr(self, symbol: str) -> float:
        df = self._fetch_bars(symbol, "1Day", 20)
        if df is not None and not df.empty:
            try:
                from strategy.signals import compute_indicators, atr_for_sizing
                return atr_for_sizing(compute_indicators(df))
            except Exception:
                pass
        return 0.0

    def _fetch_bars(self, symbol: str, timeframe: str, limit: int):
        try:
            if self._is_toss():
                # Toss API: 1m / 1d 만 지원 (5m 없음)
                if timeframe == "5Min":
                    toss_interval, toss_count = "1m", limit * 5
                elif timeframe == "1Min":
                    toss_interval, toss_count = "1m", limit
                else:
                    toss_interval, toss_count = "1d", limit
                candles = self.broker.get_candles(symbol, interval=toss_interval, count=toss_count)
                if not candles:
                    return None
                return pd.DataFrame(candles)[["open", "high", "low", "close", "volume"]]

            # Alpaca: 세마포어·싱글턴이 적용된 공용 클라이언트 사용 (DataFrame or 버그 회피)
            from data.alpaca_bars import fetch_bars as _ab_fetch
            return _ab_fetch(symbol, timeframe, limit)

        except Exception as exc:
            logging.debug("[orchestrator] bars 조회 실패 %s/%s: %s", symbol, timeframe, exc)
            return None

    def _calc_qty(self, price: float, budget: float) -> int:
        from strategy.sizing import budget_cap_size
        return budget_cap_size(budget, price)

    def _fetch_ask(self, symbol: str) -> float:
        """최우선 매도 호가 조회 (IOC 지정가 매수용)."""
        try:
            if self._is_toss():
                ob = self.broker.get_orderbook(symbol)
                return float(ob.get("ask", 0.0))
            # Alpaca: 최신 체결가를 ask 근사치로 사용
            return self._fetch_last(symbol)
        except Exception:
            return 0.0

    def _do_partial_exit(
        self, sym: str, qty: int, price: float,
        new_stage: int, sell_ratio: float, strategy: str,
    ) -> None:
        """분할 청산 — 잔량 중 sell_ratio 비율만큼 매도."""
        sell_qty = max(1, int(qty * sell_ratio))
        remain   = qty - sell_qty
        try:
            slip_p   = _PAPER_SLIP_SELL if self._is_paper() else _EXIT_LIMIT_SLIP
            limit_px = round(price * (1 - slip_p), 4)
            self.broker.submit_order(symbol=sym, qty=sell_qty, side="sell", type="limit", price=limit_px)
            self.db.update_partial_stage(sym, new_stage, remain)
            self.db.record_trade(sym, "sell", sell_qty, price, strategy,
                                 f"partial_exit_stage{new_stage}")
            pnl = (price - float(self.db.get_open_position(sym)["entry_price"])) * sell_qty
            self._notify(
                f"[{strategy.upper()}] {sym} 분할청산 stage{new_stage} "
                f"{sell_qty}주 @ ${price:.2f}  PnL: {'+'if pnl>=0 else ''}${pnl:.2f}  잔량: {remain}주"
            )
            logging.info("[EXIT] %s 분할청산 stage%d %d주 @ $%.2f (잔량 %d주)",
                         sym, new_stage, sell_qty, price, remain)
        except Exception as exc:
            logging.error("[EXIT] %s 분할청산 실패: %s", sym, exc)

    def _do_exit(self, sym: str, qty: int, price: float, reason: str, strategy: str) -> None:
        try:
            # 청산 전 포지션 정보 조회 (closed_trades 기록용)
            pos = self.db.get_open_position(sym)

            # 손절/하드스탑은 넓은 슬리피지, 그 외는 타이트 (Paper: 균일 0.1%)
            urgent = reason in ("stop_loss", "hard_stop_gap_down", "breakeven_stop")
            if self._is_paper():
                slip = _PAPER_SLIP_SELL
            else:
                slip = _STOP_LIMIT_SLIP if urgent else _EXIT_LIMIT_SLIP
            limit_px = round(price * (1 - slip), 4)
            self.broker.submit_order(symbol=sym, qty=qty, side="sell", type="limit", price=limit_px)
            self.db.close_position(sym)
            self.db.record_trade(sym, "sell", qty, price, strategy, reason)

            # 청산 완료 기록 (일지/통계용)
            if pos:
                self.db.record_closed_trade(
                    symbol      = sym,
                    strategy    = strategy,
                    entry_price = float(pos["entry_price"]),
                    exit_price  = price,
                    qty         = qty,
                    entry_ts    = pos["entry_ts"],
                    exit_ts     = datetime.now(timezone.utc).isoformat(),
                    exit_reason = reason,
                    sector      = pos.get("sector", ""),
                )
                pnl     = (price - float(pos["entry_price"])) * qty
                pnl_pct = (price - float(pos["entry_price"])) / float(pos["entry_price"]) * 100
                pnl_hint = f"  PnL: {'+'if pnl>=0 else ''}${pnl:.2f} ({pnl_pct:+.1f}%)"
                # db_manager: 사계절 엔진 trades 테이블에 실시간 기록
                try:
                    dbm.save_trade(
                        symbol=sym, buy_price=float(pos["entry_price"]),
                        sell_price=price, quantity=qty,
                        mode=f"{strategy}|{reason}",
                        result=round(pnl_pct, 2),
                    )
                except Exception:
                    pass
            else:
                pnl_pct  = 0.0
                pnl_hint = ""

            # 청산 후 3분룰 상태 해제
            self._3min_checked.discard(sym)
            # 신뢰도 블랙리스트도 해제 (다음 진입 기회 허용)
            self.conf_scanner.blacklist.clear(sym)

            # 텔레그램: 매도 사유 + 최종 수익률 포함
            mode_label = "PAPER" if self._is_paper() else "LIVE"
            self._notify(
                f"📉 [{strategy.upper()}/{mode_label}] {sym} 청산\n"
                f"매도 사유: {reason}\n"
                f"청산가: ${price:.2f}{pnl_hint}"
            )
            logging.info("[EXIT] %s 청산: %s @ $%.2f%s", sym, reason, price, pnl_hint)
        except Exception as exc:
            logging.error("[EXIT] %s 청산 실패: %s", sym, exc)

    # ──────────────────────────────────────────────────────────────────
    # EXIT 태스크 — 30초 주기
    # ──────────────────────────────────────────────────────────────────

    async def run_exit_loop(self) -> None:
        logging.info("[EXIT] 청산 루프 시작 (30초 주기)")
        while True:
            try:
                await self._exit_cycle()
            except Exception as exc:
                logging.error("[EXIT] 예외: %s", exc)
            await asyncio.sleep(30)

    async def _exit_cycle(self) -> None:
        positions = await asyncio.to_thread(self.db.list_open_positions)
        if not positions:
            return

        now = datetime.now(timezone.utc)
        for pos in positions:
            sym           = pos["symbol"]
            entry         = float(pos["entry_price"])
            peak          = float(pos.get("peak_price") or entry)
            strategy      = pos.get("strategy", "")
            qty           = int(pos.get("qty", 0))
            partial_stage = int(pos.get("partial_stage") or 0)
            if qty <= 0:
                continue

            last = await asyncio.to_thread(self._fetch_last, sym)
            if last <= 0:
                continue

            # peak 갱신
            if last > peak:
                await asyncio.to_thread(self.db.update_peak, sym, last)
                peak = last

            atr_now = 0.0   # squeeze 블록에서 갱신; 다른 전략은 _check_exit_reason 내부에서 조회

            # ── 3분 룰: 진입 후 3분 이내 수익 미달 → 절반 매도 ─────
            if strategy == "squeeze" and sym not in self._3min_checked:
                entry_ts_str = pos.get("entry_ts", "")
                if entry_ts_str:
                    try:
                        from datetime import datetime, timezone
                        entry_dt  = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
                        hold_mins = (now - entry_dt).total_seconds() / 60.0
                        if 3.0 <= hold_mins <= 8.0:   # 3~8분 사이에 한 번만 판정
                            self._3min_checked.add(sym)
                            pnl_pct = (last - entry) / entry if entry > 0 else 0.0
                            if pnl_pct <= 0.0:
                                sell_qty = max(1, qty // 2)
                                await asyncio.to_thread(
                                    self._do_partial_exit, sym, qty, last,
                                    partial_stage + 5, 0.5, strategy
                                )
                                qty = qty - sell_qty
                                self._notify(
                                    f"⚠️ [3분룰] {sym} 진입 {hold_mins:.1f}분 경과 수익 미달 "
                                    f"({pnl_pct*100:+.1f}%) — 절반 매도"
                                )
                                logging.info("[EXIT][3분룰] %s hold=%.1f분 pnl=%.1f%% → 절반 매도",
                                             sym, hold_mins, pnl_pct * 100)
                                if qty <= 0 and self._stream:
                                    self._stream.release(sym)
                                    continue
                    except Exception as _e:
                        logging.debug("[EXIT][3분룰] %s ts 파싱 오류: %s", sym, _e)

            # ── 분할 청산 (B3 squeeze 전용) ──────────────────────────
            # partial_exits_enabled: false → 기계적 % 분할 비활성화
            # 대신 _check_exit_reason 내 분배(distribution) 감지 시 50% 부분 청산
            squeeze_cfg = self.cfg.get("squeeze", {})
            if strategy == "squeeze" and squeeze_cfg.get("partial_exits_enabled", True):
                new_stage, sell_ratio = partial_exit_check(entry, last, partial_stage)
                if sell_ratio > 0:
                    await asyncio.to_thread(
                        self._do_partial_exit, sym, qty, last, new_stage, sell_ratio, strategy
                    )
                    qty = max(1, qty - int(qty * sell_ratio))
                    partial_stage = new_stage

            # ── B2 방어 모드: 주봉 20주 MA 이탈 시 청산 ─────────────────
            if strategy == "etf_swing" and self.b2_alloc.current_mode == B2AllocMode.CASH:
                await asyncio.to_thread(self._do_exit, sym, qty, last, "b2_cash_protection", strategy)
                continue

            if strategy == "etf_swing":
                from strategy.b2_allocation import B2AllocMode as _B2M
                if self.b2_alloc.current_mode == _B2M.DEFENSE_INDEX:
                    # 인트라데이 손절: -8% 하드 스탑 (주봉 청산만 있어 손실 무제한 방치 버그 수정)
                    defense_sl_pct = self.cfg.get("etf_swing", {}).get("long_sl_pct", 0.08)
                    if entry > 0 and last <= entry * (1.0 - defense_sl_pct):
                        stop_reason = (
                            f"DEFENSE_INDEX 손절 -{defense_sl_pct*100:.0f}% "
                            f"(진입 ${entry:.2f} → 현재 ${last:.2f})"
                        )
                        await asyncio.to_thread(self._do_exit, sym, qty, last, stop_reason, strategy)
                        logging.info("[B2][DEFENSE] %s 손절 청산: %s", sym, stop_reason)
                        continue
                    wk_exit, wk_reason = await asyncio.to_thread(
                        self.b2_alloc.check_weekly_exit, sym
                    )
                    if wk_exit:
                        await asyncio.to_thread(self._do_exit, sym, qty, last, wk_reason, strategy)
                        continue

            # ── ExitStrategyEngine: B3 고도화 청산 (가변 ATR + 개미 털기 방어) ─
            # hard_stop / stop_loss / distribution 은 _check_exit_reason 에서 처리.
            # 이 엔진은 trailing_stop 판단을 대체하며 breakeven_trap / orderflow 도 추가.
            if strategy == "squeeze":
                atr_now = await asyncio.to_thread(self._fetch_atr, sym)
                df_flow = await asyncio.to_thread(self._fetch_bars, sym, "5Min", 100)
                peak_pnl_pct = (peak - entry) / entry if entry > 0 else 0.0

                # 거래량 정보 추출
                cur_vol, avg_vol_20 = 0.0, 0.0
                if df_flow is not None and not df_flow.empty:
                    cur_vol = float(df_flow.iloc[-1].get("volume", 0) or 0)
                    from strategy.signals import compute_indicators
                    if "vol_ma20" not in df_flow.columns:
                        df_flow = compute_indicators(df_flow)
                    avg_vol_20 = float(df_flow.iloc[-1].get("vol_ma20", 0) or 0)

                signal = await asyncio.to_thread(
                    self.exit_engine.assess,
                    sym, entry, last, peak, atr_now, df_flow,
                    cur_vol, avg_vol_20, peak_pnl_pct,
                )

                if signal.decision == ExitDecision.SELL:
                    await asyncio.to_thread(self._do_exit, sym, qty, last, signal.reason, strategy)
                    if self._stream:
                        self._stream.release(sym)
                    continue   # 청산 완료 → 이하 _check_exit_reason 스킵

                if signal.decision == ExitDecision.SHAKEOUT_WAIT:
                    continue   # 이번 사이클 스킵 (대기 중)
                # HOLD → _check_exit_reason 로 폴스루

            cfg_b = self.cfg.get(strategy, self.cfg.get("risk", {}))
            # squeeze는 위에서 atr_now를 이미 조회했으므로 재사용 (중복 API 호출 방지)
            _atr_hint = atr_now if strategy == "squeeze" else None
            reason = await asyncio.to_thread(
                self._check_exit_reason, sym, entry, last, peak, cfg_b, strategy, now, _atr_hint
            )
            if reason:
                # 분배 감지 → 부분 청산 후 포지션 유지 (전량 청산 아님)
                if reason.startswith("distribution_partial:"):
                    dist_ratio = float(reason.split(":")[1])
                    sell_qty = max(1, int(qty * dist_ratio))
                    await asyncio.to_thread(
                        self._do_partial_exit, sym, qty, last,
                        partial_stage + 10,  # 분배 청산은 stage 10+으로 구분
                        dist_ratio, strategy
                    )
                    qty -= sell_qty
                    if qty <= 0 and self._stream and strategy == "squeeze":
                        self._stream.release(sym)
                else:
                    await asyncio.to_thread(self._do_exit, sym, qty, last, reason, strategy)
                    if self._stream and strategy == "squeeze":
                        self._stream.release(sym)

    def _check_exit_reason(
        self, sym, entry, last, peak, cfg, strategy, now,
        atr: float = 0.0,   # 이미 조회된 ATR 재사용 (squeeze는 _exit_cycle에서 전달)
    ) -> Optional[str]:
        """모든 청산 조건 확인 — 동기 실행 (to_thread 내)."""
        stop_pct = float(cfg.get("stop_loss_pct", 0.05))

        # 1. 하드 스탑: 장 시작 갭락이 손절폭 초과 → 즉시 시장가 청산
        open_px = self._fetch_open_price(sym)
        if open_px and hard_stop_gap_down(entry, open_px, stop_pct):
            return "hard_stop_gap_down"

        # 2. 고정손절 vs ATR손절 — 더 타이트한 쪽 우선 (atr=0이면 내부 조회)
        if atr <= 0.0:
            atr = self._fetch_atr(sym)
        atr_mul = float(cfg.get("atr_multiplier", 2.0))
        eff_stop = effective_stop_price(entry, stop_pct, peak, atr, atr_mul)
        if last <= eff_stop:
            return "stop_loss"

        # 3. Breakeven Stop — 고점 +15% 도달 후 현재가 진입가 이하
        breakeven_trigger = float(cfg.get("breakeven_trigger_pct", 0.15))
        if breakeven_stop_hit(entry, last, peak, breakeven_trigger):
            return "breakeven_stop"

        # 4. 목표가 도달 — squeeze는 분할청산+ATR트레일링으로 수익 극대화, 고정 TP 없음
        if strategy != "squeeze" and take_profit_hit(entry, last, cfg):
            return "take_profit"

        # 5. 분배(distribution) 감지 — squeeze 전용 부분 청산 트리거
        # 기계적 % 분할 대신: 매도세가 매수세를 실제로 압도할 때 50% 청산
        if strategy == "squeeze":
            pnl_pct = (last - entry) / entry if entry > 0 else 0.0
            dist_ratio = float(cfg.get("distribution_exit_ratio", 0.50))
            if pnl_pct >= 0.15:  # 최소 +15% 이상 수익 구간에서만 분배 감지
                df_recent = self._fetch_bars(sym, "5Min", 20)
                if df_recent is not None and not df_recent.empty:
                    from strategy.squeeze import is_distribution_detected
                    distributing, dist_reason = is_distribution_detected(df_recent, lookback_bars=5)
                    if distributing:
                        logging.info("[EXIT][%s] 분배 감지 → %d%% 부분 청산: %s",
                                     sym, int(dist_ratio * 100), dist_reason)
                        return f"distribution_partial:{dist_ratio}"

        # 6. 트레일링 스탑
        # squeeze: ExitStrategyEngine(_exit_cycle)에서 이미 처리 — 여기선 스킵
        if strategy != "squeeze" and trailing_stop_active(entry, last, peak, cfg):
            return "trailing_stop"

        # 7. 장 마감 N분 전 — 인트라데이 전략만 (squeeze/etf_swing)
        if strategy in ("squeeze", "etf_swing"):
            mins = int(self.cfg.get("risk", {}).get("eod_exit_minutes_before_close", 15))
            if eod_exit(now, mins):
                return "eod_exit"

        return None

    # ──────────────────────────────────────────────────────────────────
    # MONITOR 태스크 — 60초 주기 (킬스위치 + VIX RoC + 리밸런싱)
    # ──────────────────────────────────────────────────────────────────

    async def run_monitor_loop(self) -> None:
        logging.info("[MONITOR] 모니터 루프 시작 (60초 주기)")
        tick = 0
        _journal_done_date: str = ""  # 하루 한 번만 생성
        while True:
            try:
                await self._monitor_cycle()
                tick += 1
                # 60틱(60분)마다 성과 기반 리밸런싱
                if tick >= 60:
                    await asyncio.to_thread(self.bucket_capital.check_and_rebalance)
                    tick = 0

                # 장 마감 후 일지 자동 생성 (16:05 ET 이후, 하루 한 번)
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
                today  = now_et.strftime("%Y-%m-%d")
                if (now_et.hour == 16 and now_et.minute >= 5
                        and _journal_done_date != today
                        and now_et.weekday() < 5):  # 평일만
                    try:
                        from storage.journal import generate_and_save
                        await asyncio.to_thread(generate_and_save, self.db, today, True)
                        _journal_done_date = today
                        logging.info("[MONITOR] 일지 자동 생성 완료: %s", today)
                    except Exception as je:
                        logging.warning("[MONITOR] 일지 생성 실패: %s", je)

            except Exception as exc:
                logging.error("[MONITOR] 예외: %s", exc)
            await asyncio.sleep(60)

    async def _monitor_cycle(self) -> None:
        acct = await self._get_account()
        if not acct:
            return

        # AccountManager 경유 — 총 자산 + Settled Cash 통합 갱신
        await self.account_mgr.refresh(self.broker)

        # 킬스위치 — 미실현 손익 기준
        killed = self.kill_switch.update(acct.equity, acct.day_pnl)
        if killed:
            logging.warning("[MONITOR] 킬스위치 ON — 미실현손익 기반 일손실 한도 초과")

        # VIX 절대값 + 변화율(RoC) 이중 체크
        from strategy.regime import fetch_vix
        vix = await asyncio.to_thread(fetch_vix)
        if vix and vix > 0:
            from risk.guard import vix_rate_of_change_alert
            if self._prev_vix > 0 and vix_rate_of_change_alert(vix, self._prev_vix, VIX_ROC_THRESHOLD):
                logging.warning(
                    "[MONITOR] VIX 급등 감지: %.1f → %.1f (+%.0f%%) — 선제 진입 차단",
                    self._prev_vix, vix, (vix - self._prev_vix) / self._prev_vix * 100,
                )
                self._notify(f"⚠️ VIX 급등: {self._prev_vix:.1f} → {vix:.1f}")
            self._prev_vix = vix

        # Panic 레짐 전환 감지 → 헤지 분기
        from analysis.market import analyze_market
        regime = await asyncio.to_thread(analyze_market)
        current_regime = getattr(regime, "regime", "bull") if hasattr(regime, "regime") else regime
        if current_regime == "panic" and self._prev_regime != "panic":
            logging.warning("[MONITOR] Panic 레짐 진입 — 헤지 분기 실행")
            await self._apply_panic_hedge()
        elif current_regime != "panic" and self._prev_regime == "panic":
            self._hedge_active = False
            logging.info("[MONITOR] Panic 해제 — 헤지 상태 리셋")
        self._prev_regime = current_regime

    async def _apply_panic_hedge(self) -> None:
        """
        Panic 레짐 대응 — 전량청산 대신 헤지.

        처리:
          B3(급등주): 즉시 전량 청산 (Panic 중 급등 불가)
          B2(ETF):    인버스 ETF(SQQQ/SDS) 헤지 포지션 추가
          B1(가치주): 유지 (장기 관점, Panic 저점이 매수 기회)
        """
        if self._hedge_active:
            return

        positions = await asyncio.to_thread(self.db.list_open_positions)

        # B3 전량 청산
        for pos in positions:
            if pos["strategy"] != "squeeze":
                continue
            sym  = pos["symbol"]
            qty  = int(pos.get("qty", 0))
            last = await asyncio.to_thread(self._fetch_last, sym)
            if qty > 0 and last > 0:
                await asyncio.to_thread(self._do_exit, sym, qty, last, "panic_hedge_b3_exit", "squeeze")
                if self._stream:
                    self._stream.release(sym)

        # B2 인버스 ETF 헤지 추가
        b2_budget = self.bucket_capital.allocated("etf_swing") * 0.30  # B2 예산의 30%를 헤지에 사용
        for hedge_sym in PANIC_HEDGE_ETFS:
            if await asyncio.to_thread(self.db.get_open_position, hedge_sym):
                continue
            last = await asyncio.to_thread(self._fetch_last, hedge_sym)
            if last <= 0:
                continue
            qty = self._calc_qty(last, b2_budget / len(PANIC_HEDGE_ETFS))
            if qty <= 0:
                continue
            try:
                ask = self._fetch_ask(hedge_sym)
                buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=hedge_sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                await asyncio.to_thread(self.db.open_position, hedge_sym, "etf_swing", last, qty, "InverseETF")
                await asyncio.to_thread(self.db.record_trade, hedge_sym, "buy", qty, last, "etf_swing", "panic_hedge")
                self._notify(f"🛡️ Panic 헤지: {hedge_sym} {qty}주 @ ${last:.2f}")
            except Exception as exc:
                logging.error("[MONITOR] 헤지 주문 실패 %s: %s", hedge_sym, exc)

        self._hedge_active = True
        self._notify("🚨 Panic 레짐: B3 청산 + 인버스 ETF 헤지 적용")

    # ──────────────────────────────────────────────────────────────────
    # 버킷 1 — 60분 주기
    # ──────────────────────────────────────────────────────────────────

    async def run_bucket1_loop(self, symbols: List[str]) -> None:
        logging.info("[B1] 가치주 루프 시작 (60분 주기)")
        while True:
            try:
                if self._is_tradeable():
                    await asyncio.to_thread(self._bucket1_cycle, symbols)
            except Exception as exc:
                logging.error("[B1] 예외: %s", exc)
            await asyncio.sleep(3600)

    def _bucket1_cycle(self, symbols: List[str]) -> None:
        from analysis.market     import analyze_market
        from strategy.value_long import value_long_entry, scan_value_candidates
        from strategy.regime     import fetch_vix, is_high_volatility

        vix = fetch_vix()
        if is_high_volatility(vix or 0):
            logging.info("[B1] VIX %.1f — 신규 진입 차단", vix or 0)
            return

        regime  = analyze_market()
        r_str   = getattr(regime, "regime", "bull") if hasattr(regime, "regime") else regime
        budget  = self.bucket_capital.allocated("value_long")
        cfg_b1  = self.cfg.get("value_long", {})
        max_pos = int(cfg_b1.get("max_positions", 8))

        positions = self.db.list_open_positions()
        b1_count  = sum(1 for p in positions if p["strategy"] == "value_long")
        if b1_count >= max_pos:
            return

        candidates = scan_value_candidates(symbols, min_score=float(cfg_b1.get("min_fund_score", 55.0)))
        per_pos_budget = budget / max_pos

        for sym, fs in candidates:
            if b1_count >= max_pos:
                break
            if self.db.get_open_position(sym):
                continue
            df = self._fetch_bars(sym, "1Day", 60)
            if df is None:
                continue

            ok, reason = value_long_entry(
                sym, df, r_str,
                min_score=float(cfg_b1.get("min_fund_score", 55.0)),
                min_safety_margin=float(cfg_b1.get("min_safety_margin_pct", 10.0)),
            )
            if not ok:
                continue

            last = self._fetch_last(sym)
            if last <= 0:
                continue
            qty = self._calc_qty(last, per_pos_budget)
            if qty <= 0:
                continue

            try:
                if self._is_paper():
                    buy_px = round(last * (1 + _PAPER_SLIP_BUY), 4)
                else:
                    ask = self._fetch_ask(sym)
                    buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                self.db.open_position(sym, "value_long", last, qty, getattr(fs, "sector", ""))
                self.db.record_trade(sym, "buy", qty, last, "value_long", reason)
                try:
                    dbm.save_trade(
                        symbol=sym, buy_price=last, sell_price=None,
                        quantity=qty, mode="B1-Value", result=None,
                    )
                except Exception:
                    pass
                mode_label = "PAPER" if self._is_paper() else "LIVE"
                self._notify(f"[B1/{mode_label}] {sym} 매수 {qty}주 @ ${last:.2f} | {reason}")
                b1_count += 1
            except Exception as exc:
                logging.error("[B1] %s 주문 실패: %s", sym, exc)

    # ──────────────────────────────────────────────────────────────────
    # 버킷 2 — 15분 주기
    # ──────────────────────────────────────────────────────────────────

    async def run_bucket2_loop(self) -> None:
        logging.info("[B2] ETF 스윙 루프 시작 (15분 주기)")
        while True:
            try:
                if self._is_tradeable():
                    # StrategyManager 경유 — B3/B2 모드 + 동기화 상태 조회
                    if self.strategy_mgr.is_b2:
                        if not self.strategy_mgr.is_syncing:
                            await asyncio.to_thread(self._b2_alloc_cycle)
                    else:
                        await asyncio.to_thread(self._bucket2_cycle)
            except Exception as exc:
                logging.error("[B2] 예외: %s", exc)
            await asyncio.sleep(900)

    def _b2_alloc_cycle(self) -> None:
        """
        B2 동적 자산 배분 사이클 (B2_SWING 모드).

        B2AllocationEngine.rebalance() 결과에 따라:
          BULL_LEVERAGE → 레버리지 ETF 상위 2개 진입
          DEFENSE_INDEX → 지수 ETF 1개 진입
          CASH          → 모든 B2 포지션 청산
        """
        from strategy.etf_swing       import swing_b2_entry, B2_LEVERAGE_UNIVERSE, B2_DEFENSE_UNIVERSE
        from strategy.b2_allocation   import B2AllocMode
        from strategy.regime          import fetch_vix, is_high_volatility

        # 매매 전 예수금 실시간 재확인 (T+1 프리라이딩 방지)
        if not self._is_toss():
            try:
                settled = self.broker.get_settled_cash()
                self.bucket_capital.update_settled_cash(settled)
                logging.info("[B2] 예수금 재확인: $%.0f", settled)
                if settled <= 0:
                    logging.info("[B2] 예수금 $0 — 매매 차단")
                    return
            except Exception as exc:
                logging.debug("[B2] 예수금 조회 실패: %s", exc)

        vix = fetch_vix()
        if is_high_volatility(vix or 0):
            return

        target  = self.strategy_mgr.rebalance_b2()
        budget  = self.account_mgr.capital_b2()
        cfg_b2  = self.cfg.get("etf_swing", {})

        # ── CASH 모드: 모든 B2 포지션 청산 ─────────────────────────
        if target.mode == B2AllocMode.CASH:
            positions = self.db.list_open_positions()
            for pos in positions:
                if pos.get("strategy") != "etf_swing":
                    continue
                sym  = pos["symbol"]
                qty  = int(pos.get("qty", 0))
                last = self._fetch_last(sym)
                if last > 0 and qty > 0:
                    self._do_exit(sym, qty, last, "b2_cash_protection", "etf_swing")
            return

        # ── 기존 B2 포지션 중 목표 포트폴리오에 없는 것 청산 ─────────
        positions = self.db.list_open_positions()
        for pos in positions:
            if pos.get("strategy") != "etf_swing":
                continue
            sym = pos["symbol"]
            if sym not in target.symbols:
                qty  = int(pos.get("qty", 0))
                last = self._fetch_last(sym)
                if last > 0 and qty > 0:
                    self._do_exit(sym, qty, last, "b2_rebalance_exit", "etf_swing")

        # ── 신규 목표 포지션 진입 ────────────────────────────────────
        for sym in target.symbols:
            if self.db.get_open_position(sym):
                continue   # 이미 보유 중

            df = self._fetch_bars(sym, "1Day", 60)
            if df is None:
                continue

            # B2 전용 MA20+RSI30 진입 판단 (3분룰 없음)
            ok, reason, trailing_stop = swing_b2_entry(sym, df)
            if not ok:
                logging.debug("[B2-Alloc] %s 진입 조건 미충족: %s", sym, reason)
                continue

            last = self._fetch_last(sym)
            if last <= 0:
                continue

            weight = target.weights.get(sym, 0.5)
            qty    = self._calc_qty(last, budget * weight)
            if qty <= 0:
                continue

            try:
                if self._is_paper():
                    buy_px = round(last * (1 + _PAPER_SLIP_BUY), 4)
                else:
                    ask    = self._fetch_ask(sym)
                    buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                self.db.open_position(sym, "etf_swing", last, qty, "")
                self.db.record_trade(sym, "buy", qty, last, "etf_swing", reason)
                try:
                    dbm.save_trade(
                        symbol=sym, buy_price=last, sell_price=None,
                        quantity=qty, mode=f"B2-Alloc|{target.mode.value}", result=None,
                    )
                except Exception:
                    pass
                alloc_desc = target.reasons.get(sym, "")
                mode_label = "PAPER" if self._is_paper() else "LIVE"
                self._notify(
                    f"📊 [B2-{'공격' if target.mode == B2AllocMode.BULL_LEVERAGE else '방어'}/{mode_label}] "
                    f"{sym} 매수 {qty}주 @ ${last:.2f}\n"
                    f"배분: {weight*100:.0f}% | {alloc_desc}\n"
                    f"근거: {reason}"
                )
            except Exception as exc:
                logging.error("[B2-Alloc] %s 주문 실패: %s", sym, exc)

    def _bucket2_cycle(self) -> None:
        from analysis.market    import analyze_market
        from strategy.etf_swing import get_etf_candidates, etf_swing_entry
        from strategy.regime    import fetch_vix, is_high_volatility

        vix = fetch_vix()
        if is_high_volatility(vix or 0):
            return

        regime  = analyze_market()
        budget  = self.bucket_capital.allocated("etf_swing")
        cfg_b2  = self.cfg.get("etf_swing", {})
        max_pos = int(cfg_b2.get("max_positions", 4))

        positions = self.db.list_open_positions()
        b2_count  = sum(1 for p in positions if p["strategy"] == "etf_swing")
        if b2_count >= max_pos:
            return

        candidates  = get_etf_candidates(regime)
        per_pos_bud = budget / max_pos

        for sym in candidates:
            if b2_count >= max_pos:
                break
            if self.db.get_open_position(sym):
                continue
            df = self._fetch_bars(sym, "1Day", 90)
            if df is None:
                continue

            timeframe = cfg_b2.get("timeframe", "auto")
            ok, price, stop, reason = etf_swing_entry(sym, df, regime, timeframe)
            if not ok:
                continue

            last = self._fetch_last(sym)
            if last <= 0:
                continue
            qty = self._calc_qty(last, per_pos_bud)
            if qty <= 0:
                continue

            try:
                if self._is_paper():
                    buy_px = round(last * (1 + _PAPER_SLIP_BUY), 4)
                else:
                    ask = self._fetch_ask(sym)
                    buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                self.db.open_position(sym, "etf_swing", last, qty, "ETF")
                self.db.record_trade(sym, "buy", qty, last, "etf_swing", reason)
                try:
                    dbm.save_trade(
                        symbol=sym, buy_price=last, sell_price=None,
                        quantity=qty, mode="B2-ETF", result=None,
                    )
                except Exception:
                    pass
                mode_label = "PAPER" if self._is_paper() else "LIVE"
                self._notify(f"[B2/{mode_label}] {sym} 매수 {qty}주 @ ${last:.2f} | {reason}")
                b2_count += 1
            except Exception as exc:
                logging.error("[B2] %s 주문 실패: %s", sym, exc)

    # ──────────────────────────────────────────────────────────────────
    # 버킷 3 WebSocket 이벤트 핸들러
    # ──────────────────────────────────────────────────────────────────

    async def on_bar(self, symbol: str, bar) -> None:
        """1분봉 수신 → 급등주 진입 신호 판단."""
        if not self._is_tradeable():
            return

        from strategy.regime import fetch_vix, is_high_volatility
        vix = await asyncio.to_thread(fetch_vix)
        if is_high_volatility(vix or 0):
            return

        if await asyncio.to_thread(self.db.get_open_position, symbol):
            return

        cfg_b3  = self.cfg.get("squeeze", {})
        max_pos = int(cfg_b3.get("max_positions", 3))
        positions = await asyncio.to_thread(self.db.list_open_positions)
        if sum(1 for p in positions if p["strategy"] == "squeeze") >= max_pos:
            return

        df = await asyncio.to_thread(self._fetch_bars, symbol, "5Min", 60)
        if df is None or df.empty:
            return

        from analysis.market  import analyze_market
        from strategy.scanner import GapCandidate, vwap_pullback_entry
        from strategy.squeeze import gap_and_go_squeeze_entry

        regime    = await asyncio.to_thread(analyze_market)
        close_val = float(getattr(bar, "close", 0) or 0)
        # 일봉 기반 gap_pct/rvol 계산 (하루 1회 캐시) — 기본값 0 이면 모든 진입 필터 실패
        gap_pct, rvol = await asyncio.to_thread(self._get_b3_gap_data, symbol, df)
        candidate = GapCandidate(symbol=symbol, gap_pct=gap_pct, rvol=rvol, atr=0.0, score=50.0)

        # ── 1차 진입: Gap&Go (첫 5분봉 고점 돌파) ────────────────
        ok, price, stop, reason = await asyncio.to_thread(
            gap_and_go_squeeze_entry, symbol, df, candidate, regime
        )

        # ── 2차 진입: VWAP 풀백 재진입 (1차 실패 시) ─────────────
        if not ok:
            ok, price, stop, reason = await asyncio.to_thread(
                vwap_pullback_entry,
                df,
                candidate.gap_pct,
                candidate.rvol,
            )
            if ok:
                reason = "[2차]" + reason

        if not ok:
            return

        # ── 스푸핑 블랙리스트 ────────────────────────────────────────
        if self._stream and hasattr(self._stream, "is_spoof_blacklisted"):
            if self._stream.is_spoof_blacklisted(symbol):
                logging.info("[B3] %s 스푸핑 블랙리스트 — 진입 차단", symbol)
                return

        # ── 신뢰도 스코어 (RVOL/Alpha/VWAP 100점) ───────────────────
        if self.conf_scanner.is_blacklisted(symbol):
            logging.debug("[B3] %s 신뢰도 블랙리스트 — 스킵", symbol)
            return

        conf = await asyncio.to_thread(self.conf_scanner.score, symbol, df)
        if not conf.is_tradeable:
            logging.info("[B3] %s 신뢰도 %d점 미달 (70점 기준) — 진입 차단", symbol, conf.total)
            return

        last = await asyncio.to_thread(self._fetch_last, symbol)
        if last <= 0:
            return

        # 진입 전 예수금 실시간 재확인 (T+1 프리라이딩 방지)
        if not self._is_toss():
            await self.account_mgr.refresh_settled_cash(self.broker)

        # 신뢰도 점수 기반 예산 결정 — AccountManager 경유 (A/B + 점수 통합)
        budget = self.account_mgr.capital_for("squeeze", conf.total)
        if budget <= 0:
            return

        # 켈리 사이징: 최근 5회 승률 60%+ → 예산 1.2배
        try:
            from strategy.sizing import kelly_scale_factor
            recent_trades = self.db.get_closed_trades(limit=20)
            kelly = kelly_scale_factor(recent_trades, "squeeze")
            if kelly > 1.0:
                logging.info("[B3] 켈리 스케일 %.1fx 적용 (최근 승률 60%+)", kelly)
            budget = budget * kelly
        except Exception:
            pass

        qty = self._calc_qty(last, budget / max_pos)
        if qty <= 0:
            return

        capital_label = "전액" if conf.capital_ratio >= 1.0 else "절반"
        try:
            if self._is_paper():
                buy_px = round(last * (1 + _PAPER_SLIP_BUY), 4)
            else:
                ask = self._fetch_ask(symbol)
                buy_px = round(ask * 1.002, 4) if ask > 0 else None
            self.broker.submit_order(
                symbol=symbol, qty=qty, side="buy",
                type="limit" if buy_px else "market",
                price=buy_px, tif="IOC",
            )
            await asyncio.to_thread(self.db.open_position, symbol, "squeeze", last, qty, "")
            await asyncio.to_thread(self.db.record_trade, symbol, "buy", qty, last, "squeeze", reason)
            try:
                dbm.save_trade(
                    symbol=symbol, buy_price=last, sell_price=None,
                    quantity=qty, mode=f"B3|{conf.total}pt", result=None,
                )
            except Exception:
                pass
            if self._stream:
                self._stream.hold([symbol])
            mode_label = "PAPER" if self._is_paper() else "LIVE"
            self._notify(
                f"📈 [B3/{mode_label}] {symbol} 매수 {qty}주 @ ${last:.2f}\n"
                f"신뢰도 {conf.total}점 ({capital_label} ${budget:,.0f} 투입)\n"
                f"근거: {reason}"
            )
        except Exception as exc:
            logging.error("[B3] %s 주문 실패: %s", symbol, exc)

    async def on_quote(self, symbol: str, bid: float, ask: float) -> None:
        """호가 수신 → Bid-Ask Spread 탈출 감지."""
        if not bid_ask_spread_exit(bid, ask):
            return

        pos = await asyncio.to_thread(self.db.get_open_position, symbol)
        if not pos:
            return

        qty = int(pos.get("qty", 0))
        if qty <= 0:
            return

        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        await asyncio.to_thread(
            self._do_exit, symbol, qty, mid,
            f"spread_exit({spread_pct:.1f}%)", "squeeze"
        )
        if self._stream:
            self._stream.release(symbol)

    async def run_bucket3_stream(self, scan_symbols: List[str]) -> None:
        """버킷 3 스트림 시작 (Toss PollingStream 또는 Alpaca WebSocket)."""
        # main.py에서 미리 PollingStream을 주입한 경우 덮어쓰지 않음
        if self._stream is None:
            self._stream = Bucket3Stream(on_bar=self.on_bar, on_quote=self.on_quote)
        self._stream.watch(scan_symbols)

        # 보유 포지션도 즉시 등록
        positions = self.db.list_open_positions()
        held = [p["symbol"] for p in positions if p["strategy"] == "squeeze"]
        if held:
            self._stream.hold(held)

        logging.info("[B3] WebSocket 스트림 시작 — %d 종목 감시", len(scan_symbols))
        await self._stream.run()
