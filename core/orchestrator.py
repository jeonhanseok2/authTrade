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
from datetime import datetime, timezone
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
from strategy.exit_strategy import ExitStrategyEngine, ExitDecision
from storage.db import PositionDB

# 인버스 ETF 헤지 종목
PANIC_HEDGE_ETFS = ["SQQQ", "SDS"]
# VIX 변화율 선제 차단 임계치 (전일 대비 20% 이상 급등)
VIX_ROC_THRESHOLD = 0.20

# 지정가 청산 슬리피지 (토스 시장가 슬리피지 방지)
_EXIT_LIMIT_SLIP = 0.003   # 일반 청산: 현재가 -0.3%
_STOP_LIMIT_SLIP = 0.005   # 손절/긴급: 현재가 -0.5%


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

        # B3 급등주 전용 고도화 청산 엔진 (개미 털기 방어 + 가변 ATR)
        self.exit_engine = ExitStrategyEngine(notify=self._notify)

    # ──────────────────────────────────────────────────────────────────
    # 공통 유틸
    # ──────────────────────────────────────────────────────────────────

    def _notify(self, msg: str) -> None:
        if self.notifier:
            try:
                self.notifier.send(msg)
            except Exception:
                pass

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
        """당일 시가 조회 — Daily 봉 Open 기준 (프리마켓 폭락 반영, 1분봉 아님)."""
        df = self._fetch_bars(symbol, "1Day", 3)  # 3일치로 장 시작 전 상황도 커버
        if df is None or df.empty or "open" not in df.columns:
            return 0.0
        # 타임스탬프 컬럼이 있으면 오늘자 봉만 선택 (장 시작 전엔 어제 봉이 마지막일 수 있음)
        from datetime import date
        import zoneinfo
        today_et = date.today()  # 서버가 ET 기준이 아닐 수 있어 last row 사용 fallback
        if "timestamp" in df.columns:
            df["_date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(
                zoneinfo.ZoneInfo("America/New_York")
            ).dt.date
            today_rows = df[df["_date"] == today_et]
            if not today_rows.empty:
                return float(today_rows.iloc[-1]["open"])
        # fallback: 마지막 row (Daily 봉이므로 정규장 open = 프리마켓 반영)
        return float(df.iloc[-1]["open"])

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
            import pandas as pd

            if self._is_toss():
                # Toss API: 1m / 1d 만 지원 (5m 없음)
                # "5Min" 요청은 1m × 5배 count로 같은 시간 범위 커버
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

            from alpaca.data.requests  import StockBarsRequest   # type: ignore
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore

            tf_map = {
                "1Min": TimeFrame(1,  TimeFrameUnit.Minute),
                "5Min": TimeFrame(5,  TimeFrameUnit.Minute),
                "1Day": TimeFrame(1,  TimeFrameUnit.Day),
            }
            tf  = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, limit=limit)
            resp = self.data_client.get_stock_bars(req)

            bars = getattr(resp, "df", None) or resp.get(symbol)
            if bars is None or (hasattr(bars, "empty") and bars.empty):
                return None
            if hasattr(bars, "reset_index"):
                return bars.reset_index(drop=True)
            rows = [{"open": b.open, "high": b.high, "low": b.low,
                     "close": b.close, "volume": b.volume} for b in bars]
            return pd.DataFrame(rows)
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
            limit_px = round(price * (1 - _EXIT_LIMIT_SLIP), 4)
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

            # 손절/하드스탑은 넓은 슬리피지, 그 외는 타이트
            urgent = reason in ("stop_loss", "hard_stop_gap_down", "breakeven_stop")
            slip   = _STOP_LIMIT_SLIP if urgent else _EXIT_LIMIT_SLIP
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
                pnl = (price - float(pos["entry_price"])) * qty
                pnl_hint = f"  PnL: {'+'if pnl>=0 else ''}${pnl:.2f}"
            else:
                pnl_hint = ""

            self._notify(f"[{strategy.upper()}] {sym} 청산: {reason} @ ${price:.2f}{pnl_hint}")
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
            reason = await asyncio.to_thread(
                self._check_exit_reason, sym, entry, last, peak, cfg_b, strategy, now
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
        self, sym, entry, last, peak, cfg, strategy, now
    ) -> Optional[str]:
        """모든 청산 조건 확인 — 동기 실행 (to_thread 내)."""
        stop_pct = float(cfg.get("stop_loss_pct", 0.05))

        # 1. 하드 스탑: 장 시작 갭락이 손절폭 초과 → 즉시 시장가 청산
        open_px = self._fetch_open_price(sym)
        if open_px and hard_stop_gap_down(entry, open_px, stop_pct):
            return "hard_stop_gap_down"

        # 2. 고정손절 vs ATR손절 — 더 타이트한 쪽 우선
        atr     = self._fetch_atr(sym)
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

        self.bucket_capital.update_equity(acct.equity)

        # A/B 로테이션: 결제 완료 현금 갱신 (Cash Account T+1)
        if self.bucket_capital._ab_mode and hasattr(self.broker, "get_settled_cash"):
            try:
                settled = await asyncio.to_thread(self.broker.get_settled_cash)
                self.bucket_capital.update_settled_cash(settled)
                logging.debug("[MONITOR] 결제현금 갱신: $%.0f (그룹 %s)", settled, self.bucket_capital.active_group)
            except Exception as _e:
                logging.warning("[MONITOR] 결제현금 조회 실패: %s", _e)

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
                ask = self._fetch_ask(sym)
                buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                self.db.open_position(sym, "value_long", last, qty, getattr(fs, "sector", ""))
                self.db.record_trade(sym, "buy", qty, last, "value_long", reason)
                self._notify(f"[B1] {sym} 매수 {qty}주 @ ${last:.2f} | {reason}")
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
                    await asyncio.to_thread(self._bucket2_cycle)
            except Exception as exc:
                logging.error("[B2] 예외: %s", exc)
            await asyncio.sleep(900)

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
                ask = self._fetch_ask(sym)
                buy_px = round(ask * 1.002, 4) if ask > 0 else None
                self.broker.submit_order(
                    symbol=sym, qty=qty, side="buy",
                    type="limit" if buy_px else "market",
                    price=buy_px, tif="IOC",
                )
                self.db.open_position(sym, "etf_swing", last, qty, "ETF")
                self.db.record_trade(sym, "buy", qty, last, "etf_swing", reason)
                self._notify(f"[B2] {sym} 매수 {qty}주 @ ${last:.2f} | {reason}")
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
        # GapCandidate 생성 — 실시간 bar라 갭/RVOL 정보 없음 (기본값 0)
        candidate = GapCandidate(symbol=symbol, atr=0.0, score=50.0)

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

        # 스푸핑 블랙리스트 체크
        if self._stream and hasattr(self._stream, "is_spoof_blacklisted"):
            if self._stream.is_spoof_blacklisted(symbol):
                logging.info("[B3] %s 스푸핑 블랙리스트 — 진입 차단", symbol)
                return

        budget = self.bucket_capital.allocated("squeeze")
        last   = await asyncio.to_thread(self._fetch_last, symbol)
        if last <= 0:
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

        try:
            ask = self._fetch_ask(symbol)
            buy_px = round(ask * 1.002, 4) if ask > 0 else None
            self.broker.submit_order(
                symbol=symbol, qty=qty, side="buy",
                type="limit" if buy_px else "market",
                price=buy_px, tif="IOC",
            )
            await asyncio.to_thread(self.db.open_position, symbol, "squeeze", last, qty, "")
            await asyncio.to_thread(self.db.record_trade, symbol, "buy", qty, last, "squeeze", reason)
            if self._stream:
                self._stream.hold([symbol])
            self._notify(f"[B3] {symbol} 매수 {qty}주 @ ${last:.2f} | {reason}")
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
