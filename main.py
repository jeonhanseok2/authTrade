# main.py
"""
authTrade 메인 진입점 — 3-버킷 자동매매 아키텍처.

버킷 구조:
  Bucket 1 (value_long)  : 가치주 장기투자 — 펀더멘털 점수 기반
  Bucket 2 (etf_swing)   : ETF 스윙/장기/단기 — 레짐 + 추세 기반
  Bucket 3 (squeeze)     : 스퀴즈 + 급등 초단타 — TTM Squeeze 기반

매 사이클(poll_seconds마다) 실행 순서:
  1. 휴장/거래창/서킷브레이커/VIX/데드존 체크
  2. 시장 레짐 분석 (analysis.market)
  3. [EXIT 우선] 보유 포지션 청산 조건 확인
  4. [ENTRY] 신규 진입: 버킷3(squeeze) → 버킷2(etf) → 버킷1(value) 순
  5. 텔레그램 알림
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import yaml

from utils.market_time import is_us_trading_session
from config import load_mode_env
from strategy.risk import within_trade_window, market_circuit_breaker_triggered
from strategy.regime import fetch_vix, is_high_volatility, is_deadzone
from strategy.signals import compute_indicators, atr_for_sizing
from strategy.sizing import atr_position_size, budget_cap_size
from strategy.filters import sector_concentration_ok
from data.fetch import fetch_recent_bars
from storage.db import PositionDB

# 분석 모듈
from analysis.market import analyze_market, MarketRegime
from analysis.news import fetch_articles, analyze_sentiment, gpt_summarize_news
from analysis.governance import analyze_governance
from analysis.fundamental import analyze_fundamental

# 3-버킷 전략 모듈
from strategy.value_long import value_long_entry, value_long_exit, scan_value_candidates
from strategy.etf_swing import etf_swing_entry, etf_swing_exit, get_etf_candidates, ETF_UNIVERSE
from strategy.squeeze import (
    squeeze_entry, squeeze_hold_or_exit, squeeze_stop_loss,
    compute_trailing_stop, scalp_reentry, scan_squeeze_candidates,
    get_breakeven_stop, gap_and_go_squeeze_entry,
)
from strategy.scanner import (
    scan_gap_candidates, scan_short_squeeze,
    format_scan_report, GapCandidate,
)

# 텔레그램 / GPT (실패해도 trading 중단 없음)
try:
    from notify.telegram_notifier import send_telegram
except Exception:
    def send_telegram(msg: str) -> None:
        logging.debug("[notify] %s", msg)

try:
    from ai.gpt_helper import summarize_news
except Exception:
    def summarize_news(sym: str, text: str) -> str:
        return ""

# 브로커 선택
USE_PAPER_SIM = False
try:
    from trader.execution import AlpacaBroker
except Exception:
    USE_PAPER_SIM = True
from trader.paper import PaperSimBroker

try:
    from alpaca.data.historical import StockHistoricalDataClient
except Exception:
    StockHistoricalDataClient = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────────────
def load_watchlist(path: str, fallback: list) -> list:
    """워치리스트 파일 로드. 실패 시 fallback 반환."""
    try:
        with open(path, "r") as f:
            symbols = [x.strip().upper() for x in f if x.strip() and not x.startswith("#")]
        return symbols or fallback
    except Exception:
        return fallback


def _get_cfg_float(cfg: dict, *keys, default: float = 0.0) -> float:
    """중첩 딕셔너리에서 float 값 안전 추출."""
    val = cfg
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k, default)
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────
# 버킷 1: 가치주 청산 체크
# ─────────────────────────────────────────────────────────────────────
def _exit_value_long(sym: str, db_pos: dict, current_price: float,
                     peak_price: float, regime: str, cfg: dict) -> tuple[bool, str]:
    vl_cfg = cfg.get("value_long", {})
    fs     = analyze_fundamental(sym)
    return value_long_exit(
        symbol        = sym,
        entry_price   = db_pos["entry_price"],
        current_price = current_price,
        peak_price    = peak_price,
        regime        = regime,
        stop_pct      = _get_cfg_float(vl_cfg, "stop_loss_pct",    default=0.08),
        target_pct    = _get_cfg_float(vl_cfg, "take_profit_pct",  default=0.25),
        fs            = fs,
    )


# ─────────────────────────────────────────────────────────────────────
# 버킷 2: ETF 청산 체크
# ─────────────────────────────────────────────────────────────────────
def _exit_etf(sym: str, db_pos: dict, current_price: float,
              peak_price: float, regime: str) -> tuple[bool, str]:
    timeframe = db_pos.get("notes", "swing")  # notes 필드에 타임프레임 저장
    if timeframe not in ("short", "swing", "long"):
        timeframe = "swing"
    return etf_swing_exit(sym, db_pos["entry_price"], current_price, peak_price, timeframe, regime)


# ─────────────────────────────────────────────────────────────────────
# 버킷 3: 스퀴즈 청산 체크 — 계단식 트레일링 + 오더플로우
#
# 고정 익절 없음 — 매수 세력이 살아 있는 동안은 보유.
# 계단식 트레일링 스탑으로 50~300% 급등 시 수익 극대화.
# ─────────────────────────────────────────────────────────────────────
def _exit_squeeze(sym: str, db_pos: dict, df, current_price: float,
                  peak_price: float, cfg: dict) -> tuple[bool, str]:
    sq_cfg      = cfg.get("squeeze", {})
    entry_price = db_pos["entry_price"]
    atr         = atr_for_sizing(df)

    # 동적 손절가 복원 (DB에서 가져오거나 재계산)
    dynamic_stop = _get_cfg_float(db_pos, "dynamic_stop", default=0.0)
    if dynamic_stop == 0:
        # 최초 계산: ATR 기반 초기 손절선
        if atr > 0:
            dynamic_stop = entry_price - atr * _get_cfg_float(sq_cfg, "atr_multiplier", default=1.5)

    # 손익분기점 이동 적용 (+10% 도달 시 손절가를 진입가로 올림)
    be_stop = get_breakeven_stop(
        entry_price, current_price,
        _get_cfg_float(sq_cfg, "breakeven_trigger_pct", default=0.10),
    )
    if be_stop > dynamic_stop:
        dynamic_stop = be_stop

    # 계단식 트레일링 + 오더플로우 분배 감지로 청산 판단
    should_exit, reason, new_stop = squeeze_hold_or_exit(
        df, entry_price, current_price, peak_price,
        dynamic_stop = dynamic_stop,
        atr          = atr,
        atr_mult     = _get_cfg_float(sq_cfg, "atr_multiplier", default=1.5),
    )

    # 동적 손절가 갱신 (DB update는 호출자에서 처리)
    db_pos["dynamic_stop"] = new_stop

    return should_exit, reason


# ─────────────────────────────────────────────────────────────────────
# 프리마켓 스캔 사이클 (7:00~9:30 ET)
#
# 장 시작 전 갭업 후보 탐색 → 우선순위 리스트 생성
# 장 시작 직후 one_cycle()에서 이 리스트 기반으로 Gap&Go 진입
# ─────────────────────────────────────────────────────────────────────
def premarket_scan(symbols: list, cfg: dict, gap_watchlist: dict) -> None:
    """
    프리마켓 갭업 스캐너.
    갭업 20%+ + RVOL 5x+ + Float 50M 이하 조건 충족 종목을
    gap_watchlist에 저장해 장 시작 후 진입에 활용.

    gap_watchlist: 공유 딕셔너리 {symbol: GapCandidate}
    """
    sq_cfg    = cfg.get("squeeze", {})
    min_gap   = float(sq_cfg.get("min_gap_pct",  20.0))
    min_rvol  = float(sq_cfg.get("min_rvol",      5.0))

    scan_criteria = {
        "min_gap_pct":        min_gap,
        "min_gap_pct_soft":   4.0,
        "min_rvol_premarket": min_rvol,
        "min_rvol_intraday":  10.0,
        "max_float_ok":       50_000_000,
    }

    print(f"[SCAN] 프리마켓 스캔 시작 — {len(symbols)}개 종목")
    candidates = scan_gap_candidates(symbols, scan_criteria, min_score=35.0)

    # 숏스퀴즈 후보도 병행 스캔
    squeeze_candidates = scan_short_squeeze(symbols, min_short_pct=15.0, min_dtc=3.0)

    # gap_watchlist 업데이트
    gap_watchlist.clear()
    for c in candidates:
        gap_watchlist[c.symbol] = c

    if candidates:
        report = format_scan_report(candidates)
        print(f"[SCAN] 갭업 후보 {len(candidates)}개 발견")
        send_telegram(report)
    else:
        print("[SCAN] 갭업 후보 없음")

    if squeeze_candidates:
        top_sq = squeeze_candidates[:3]
        sq_msg = "🔥 <b>숏스퀴즈 후보</b>\n" + "\n".join(
            f"{s['symbol']}: Short {s['short_pct']}% DTC {s['days_to_cover']}일 Float {s['float_m']}M"
            for s in top_sq
        )
        print(f"[SCAN] 숏스퀴즈 후보: {[s['symbol'] for s in top_sq]}")
        send_telegram(sq_msg)


# ─────────────────────────────────────────────────────────────────────
# 메인 사이클
# ─────────────────────────────────────────────────────────────────────
def one_cycle(broker, data_client, db: PositionDB, cfg: dict,
              symbols: list, args, market_cache: dict,
              gap_watchlist: Optional[dict] = None) -> None:
    """
    한 사이클 실행.
    market_cache: 시장 레짐 캐시 (5분 TTL) — 반복 호출 최적화
    """
    now = datetime.now(timezone.utc)

    # ── A. 휴장 스킵 ─────────────────────────────────────────────────
    if cfg.get("engine", {}).get("skip_calls_when_market_closed", True) \
            and not args.ignore_window \
            and not is_us_trading_session(now):
        logging.debug("[SKIP] 휴장 시간")
        return

    # ── B. 거래창 제한 ────────────────────────────────────────────────
    tw = cfg.get("engine", {}).get("trade_window", {})
    if not args.ignore_window and not within_trade_window(
        now,
        tw.get("start_minutes_after_open", 5),
        tw.get("end_minutes_before_close", 5),
    ):
        logging.debug("[SKIP] Out of trade window")
        return

    # ── C. 서킷브레이커 ───────────────────────────────────────────────
    if data_client and not args.no_market_check:
        spy_df = fetch_recent_bars(data_client, "SPY", minutes=args.minutes)
        if spy_df is not None and not spy_df.empty:
            if market_circuit_breaker_triggered(spy_df):
                print("[HALT] S&P -7% 서킷브레이커 발동")
                send_telegram("🚨 서킷브레이커 발동 — 모든 신규 진입 중단")
                return

    # ── D. VIX/데드존 체크 ────────────────────────────────────────────
    block_entry = False
    regime_cfg  = cfg.get("regime", {})
    if regime_cfg.get("vix_filter_enabled", True):
        vix     = fetch_vix()
        vix_max = float(regime_cfg.get("vix_max_entry", 30.0))
        if is_high_volatility(vix, vix_max):
            print(f"[WARN] VIX={vix:.1f} — 신규 진입 차단")
            block_entry = True

    dz_cfg = cfg.get("engine", {}).get("deadzone", {})
    if dz_cfg.get("enabled", True) and is_deadzone(
        now,
        start_hour=int(dz_cfg.get("start_hour", 11)),
        start_min =int(dz_cfg.get("start_min",  30)),
        end_hour  =int(dz_cfg.get("end_hour",   13)),
        end_min   =int(dz_cfg.get("end_min",     0)),
    ):
        print("[WARN] 점심 데드존 — 신규 진입 차단")
        block_entry = True

    # ── E. 시장 레짐 분석 (5분 캐시) ─────────────────────────────────
    cache_ts = market_cache.get("ts", 0)
    if time.time() - cache_ts > 300:
        market: MarketRegime = analyze_market()
        market_cache["market"] = market
        market_cache["ts"]     = time.time()
        print(f"[MARKET] {market.regime.upper()} | VIX={market.vix:.1f} | {market.summary}")
    else:
        market = market_cache["market"]
    regime = market.regime

    # ── F. 가격 데이터 수집 ───────────────────────────────────────────
    # 버킷별 워치리스트 통합 (최대 60개)
    etf_list   = load_watchlist(
        cfg.get("etf_swing", {}).get("watchlist_file", "watchlists/etf_symbols.txt"), []
    )
    value_list = load_watchlist(
        cfg.get("value_long", {}).get("watchlist_file", "watchlists/value_symbols.txt"), []
    )
    all_symbols = list(dict.fromkeys(symbols + etf_list + value_list))[:60]

    dfs: dict = {}
    for s in all_symbols:
        try:
            dfs[s] = fetch_recent_bars(data_client, s, minutes=args.minutes) if data_client else None
        except Exception as e:
            logging.warning("[WARN] fetch %s: %s", s, e)
            dfs[s] = None
        time.sleep(0.05)

    # ── G. 계좌 상태 ─────────────────────────────────────────────────
    acct           = broker.get_account()
    equity         = float(acct.get("portfolio_value", acct.get("cash", 0.0)))
    per_trade_risk = _get_cfg_float(cfg, "risk", "per_trade_risk_pct", default=0.01)
    atr_mult       = _get_cfg_float(cfg, "risk", "atr_multiplier",     default=2.0)
    sector_cfg     = cfg.get("sector", {})
    max_per_sector = int(sector_cfg.get("max_positions_per_sector", 3))
    sector_counts  = db.count_open_by_sector()
    broker_pos     = broker.list_positions()

    # ── H. 청산 처리 (EXIT 먼저) ──────────────────────────────────────
    risk_cfg    = cfg.get("risk", {})
    eod_mins    = int(risk_cfg.get("eod_exit_minutes_before_close", 15))

    from strategy.exits import stop_loss_hit, take_profit_hit, trailing_stop_active, rsi_overbought_exit, eod_exit

    for sym, broker_qty in list(broker_pos.items()):
        if broker_qty <= 0:
            continue
        df = dfs.get(sym)
        if df is None or df.empty:
            continue

        current_price = float(df["close"].iloc[-1])
        db_pos        = db.get_open_position(sym)
        entry_price   = db_pos["entry_price"] if db_pos else current_price
        peak_price    = db_pos["peak_price"]  if db_pos else current_price
        strategy      = db_pos["strategy"]    if db_pos else "unknown"
        qty_sell      = int(broker_qty)

        db.update_peak(sym, current_price)

        exit_flag, reason = False, ""

        # EOD 청산 (전략 무관 공통)
        if eod_exit(now, eod_mins):
            exit_flag, reason = True, "eod"

        # 전략별 청산 로직
        elif strategy == "value_long":
            exit_flag, reason = _exit_value_long(
                sym, db_pos, current_price, peak_price, regime, cfg
            )
        elif strategy == "etf_swing":
            exit_flag, reason = _exit_etf(sym, db_pos, current_price, peak_price, regime)
        elif strategy in ("squeeze", "scalp"):
            df_ind = compute_indicators(df)
            exit_flag, reason = _exit_squeeze(sym, db_pos, df_ind, current_price, peak_price, cfg)
        else:
            # 기본 리스크 규칙 (레거시 호환)
            df_ind = compute_indicators(df)
            rsi_thr = float(risk_cfg.get("rsi_overbought_exit", 80.0))
            if stop_loss_hit(entry_price, current_price, risk_cfg):
                exit_flag, reason = True, "stop_loss"
            elif take_profit_hit(entry_price, current_price, risk_cfg) \
                    and trailing_stop_active(entry_price, current_price, peak_price, risk_cfg):
                exit_flag, reason = True, "trailing_stop"
            elif rsi_overbought_exit(df_ind, rsi_thr):
                exit_flag, reason = True, "rsi_overbought"

        if exit_flag and reason:
            print(f"[EXIT] {reason} {sym} x{qty_sell} @ {current_price:.2f}")
            broker.submit_market_order(sym, qty_sell, "sell")
            db.close_position(sym)
            db.record_trade(sym, "sell", qty_sell, current_price, strategy, reason)
            send_telegram(f"🔴 <b>EXIT</b> {sym} x{qty_sell} @ {current_price:.2f} ({reason})")

    # ── I. 신규 진입 (block_entry=True면 스킵) ────────────────────────
    if block_entry:
        print(f"[DBG] tick {datetime.now().isoformat()} (진입 차단)")
        return

    # 계좌 및 섹터 상태 재조회 (청산 후 갱신)
    broker_pos    = broker.list_positions()
    sector_counts = db.count_open_by_sector()

    # 버킷 3 포지션 카운트 (I-0, I-1 공용)
    current_sq_count = sum(1 for p in db.list_open_positions() if p["strategy"] in ("squeeze", "scalp"))

    # ── I-0. 버킷 3-A: Gap&Go 진입 (프리마켓 스캐너 포착 종목) ──────
    # 갭업 20%+ + RVOL 10x+ 조건 충족 → 장 시작 직후 5분봉 기반 진입
    if gap_watchlist:
        for sym, gap_cand in list(gap_watchlist.items()):
            if current_sq_count >= sq_max:
                break
            if float(broker_pos.get(sym, 0.0)) > 0:
                continue
            df_5m = dfs.get(sym)
            if df_5m is None or df_5m.empty or len(df_5m) < 3:
                continue

            should_enter, entry_px, stop_px, reason = gap_and_go_squeeze_entry(
                sym, df_5m, gap_cand, regime
            )
            if not should_enter:
                logging.debug("[GAP&GO] %s 패스: %s", sym, reason)
                continue

            current_price = float(df_5m["close"].iloc[-1])
            atr           = atr_for_sizing(compute_indicators(df_5m))
            qty           = atr_position_size(atr, equity, per_trade_risk * 1.5, current_price, 2.0)
            if qty <= 0:
                qty = budget_cap_size(sq_budget / max(1, sq_max), current_price)
            if qty <= 0:
                continue

            squeeze_flag = "🔥숏스퀴즈" if gap_cand.squeeze_setup else ""
            print(f"[GAP&GO] BUY {sym} x{qty} @ {current_price:.2f} | {reason}")
            broker.submit_market_order(sym, qty, "buy")
            db.open_position(sym, "squeeze", current_price, qty, "")
            db.record_trade(sym, "buy", qty, current_price, "squeeze", reason)
            current_sq_count += 1
            gap_watchlist.pop(sym, None)  # 진입 완료 → 워치리스트에서 제거
            send_telegram(
                f"⚡ <b>GAP&GO BUY</b> {sym}{squeeze_flag} x{qty} @ {current_price:.2f}\n"
                f"갭 {gap_cand.gap_pct:+.0f}% | RVOL {gap_cand.rvol:.0f}x | {reason}"
            )

    # ── I-1. 버킷 3-B: TTM 스퀴즈 진입 ──────────────────────────────
    sq_cfg      = cfg.get("squeeze", {})
    sq_max      = int(sq_cfg.get("max_positions", 3))
    sq_budget   = float(sq_cfg.get("bucket_usd", 2_000_000))
    sq_vol_min  = float(sq_cfg.get("min_volume_ratio", 3.0))

    squeeze_dfs   = {s: df for s, df in dfs.items() if df is not None and not df.empty}
    sq_candidates = scan_squeeze_candidates(squeeze_dfs, regime)[:sq_max]

    for sym, sq_mom in sq_candidates:
        if current_sq_count >= sq_max:
            break
        if float(broker_pos.get(sym, 0.0)) > 0:
            continue
        df = dfs[sym]
        should_enter, reason = squeeze_entry(sym, df, regime, sq_vol_min)
        if not should_enter:
            continue

        current_price = float(df["close"].iloc[-1])
        df_ind  = compute_indicators(df)
        atr     = atr_for_sizing(df_ind)
        qty     = atr_position_size(atr, equity, per_trade_risk * 1.5, current_price, 1.5)
        if qty <= 0:
            qty = budget_cap_size(sq_budget / max(1, sq_max), current_price)
        if qty <= 0:
            continue

        print(f"[SQ] BUY {sym} x{qty} @ {current_price:.2f} | mom={sq_mom:.4f} | {reason}")
        broker.submit_market_order(sym, qty, "buy")
        db.open_position(sym, "squeeze", current_price, qty, "")
        db.record_trade(sym, "buy", qty, current_price, "squeeze", reason)
        current_sq_count += 1
        send_telegram(f"⚡ <b>SQUEEZE BUY</b> {sym} x{qty} @ {current_price:.2f}")

    # ── I-2. 버킷 2: ETF 진입 ────────────────────────────────────────
    etf_cfg       = cfg.get("etf_swing", {})
    etf_max       = int(etf_cfg.get("max_positions", 4))
    etf_budget    = float(etf_cfg.get("bucket_usd", 3_000_000))
    etf_candidates = get_etf_candidates(market)

    current_etf_count = sum(1 for p in db.list_open_positions() if p["strategy"] == "etf_swing")
    for sym in etf_candidates:
        if current_etf_count >= etf_max:
            break
        df = dfs.get(sym)
        if df is None or df.empty:
            continue
        if float(broker_pos.get(sym, 0.0)) > 0:
            continue

        should_enter, reason, tf = etf_swing_entry(sym, df, market)
        if not should_enter:
            continue

        current_price = float(df["close"].iloc[-1])
        df_ind  = compute_indicators(df)
        atr     = atr_for_sizing(df_ind)
        qty     = atr_position_size(atr, equity, per_trade_risk, current_price, atr_mult)
        if qty <= 0:
            qty = budget_cap_size(etf_budget / max(1, etf_max), current_price)
        if qty <= 0:
            continue

        etf_sector = ETF_UNIVERSE.get(sym, {}).get("name", "ETF")
        if not sector_concentration_ok(etf_sector, sector_counts, max_per_sector):
            continue

        print(f"[ETF] BUY {sym} x{qty} @ {current_price:.2f} | {tf} | {reason}")
        broker.submit_market_order(sym, qty, "buy")
        # notes 필드에 타임프레임 저장 (청산 로직에서 참조)
        db.open_position(sym, "etf_swing", current_price, qty, etf_sector)
        db.record_trade(sym, "buy", qty, current_price, "etf_swing", reason)
        sector_counts[etf_sector] = sector_counts.get(etf_sector, 0) + 1
        current_etf_count += 1
        send_telegram(f"📊 <b>ETF BUY</b> {sym} x{qty} @ {current_price:.2f} ({tf})")

    # ── I-3. 버킷 1: 가치주 진입 ─────────────────────────────────────
    vl_cfg     = cfg.get("value_long", {})
    vl_max     = int(vl_cfg.get("max_positions", 8))
    vl_budget  = float(vl_cfg.get("bucket_usd", 15_000_000))
    vl_min_sc  = float(vl_cfg.get("min_fund_score", 55.0))

    current_vl_count = sum(1 for p in db.list_open_positions() if p["strategy"] == "value_long")
    if current_vl_count < vl_max:
        vl_candidates = scan_value_candidates(value_list[:30], min_score=vl_min_sc)

        for sym, fs in vl_candidates:
            if current_vl_count >= vl_max:
                break
            df = dfs.get(sym)
            if df is None or df.empty:
                continue
            if float(broker_pos.get(sym, 0.0)) > 0:
                continue

            should_enter, reason = value_long_entry(sym, df, regime, vl_min_sc)
            if not should_enter:
                continue

            current_price = float(df["close"].iloc[-1])
            df_ind  = compute_indicators(df)
            atr     = atr_for_sizing(df_ind)
            qty     = atr_position_size(atr, equity, per_trade_risk * 0.8, current_price, atr_mult)
            if qty <= 0:
                qty = budget_cap_size(vl_budget / max(1, vl_max), current_price)
            if qty <= 0:
                continue

            sym_sector = fs.sector or ""
            if not sector_concentration_ok(sym_sector, sector_counts, max_per_sector):
                continue

            print(f"[VAL] BUY {sym} x{qty} @ {current_price:.2f} | score={fs.score:.1f} | {reason}")
            broker.submit_market_order(sym, qty, "buy")
            db.open_position(sym, "value_long", current_price, qty, sym_sector)
            db.record_trade(sym, "buy", qty, current_price, "value_long", reason)
            sector_counts[sym_sector] = sector_counts.get(sym_sector, 0) + 1
            current_vl_count += 1

            # GPT 뉴스 요약 (활성화된 경우)
            if cfg.get("notify", {}).get("gpt", {}).get("enabled", False):
                articles = fetch_articles(sym, hours=24, limit=5)
                summary  = gpt_summarize_news(sym, articles)
                if summary:
                    send_telegram(f"📰 <b>{sym} 뉴스</b>\n{summary}")

            send_telegram(
                f"💎 <b>VALUE BUY</b> {sym} x{qty} @ {current_price:.2f} "
                f"(점수:{fs.score:.0f})"
            )

    print(f"[DBG] tick {datetime.now().isoformat()}")


# ─────────────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="authTrade 3-버킷 자동매매")
    parser.add_argument("--symbol",         default=None,   help="단일 종목 테스트")
    parser.add_argument("--loop",           action="store_true")
    parser.add_argument("--minutes",        type=int,  default=600)
    parser.add_argument("--ignore-window",  action="store_true")
    parser.add_argument("--no-market-check", action="store_true")
    args = parser.parse_args()

    # ── 설정 로드 ─────────────────────────────────────────────────────
    cfg = {}
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    mode    = load_mode_env()
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    paper   = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    print(f"[MODE] {mode} | paper={paper} | symbol={args.symbol}")

    # ── 브로커 / 데이터 클라이언트 ────────────────────────────────────
    if USE_PAPER_SIM or not api_key or not secret:
        print("[WARN] PaperSim 모드 (실제 주문 없음)")
        broker      = PaperSimBroker()
        data_client = None
    else:
        broker      = AlpacaBroker(api_key, secret, paper=paper)
        data_client = StockHistoricalDataClient(api_key, secret) if StockHistoricalDataClient else None

    print("[INFO] Account:", broker.get_account())

    # ── SQLite 포지션 저널 ─────────────────────────────────────────────
    db_path = cfg.get("storage", {}).get("db_path", "storage/trade.db")
    db      = PositionDB(db_path)

    # ── 유니버스 (기본 워치리스트) ─────────────────────────────────────
    fallback = cfg.get("universe", {}).get("fallback_symbols", ["AAPL", "MSFT", "SPY"])
    symbols  = load_watchlist(
        cfg.get("universe", {}).get("watchlist_file", "watchlists/symbols.txt"), fallback
    )
    if args.symbol:
        symbols = [args.symbol]

    # 레짐 캐시 (사이클 간 공유 — analyze_market 중복 호출 방지)
    market_cache: dict = {}

    # 프리마켓 스캐너가 채우는 갭업 워치리스트 (symbol → GapCandidate)
    gap_watchlist: dict = {}

    # ── 루프 ─────────────────────────────────────────────────────────
    if args.loop:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        last_premarket_scan = 0.0  # 마지막 프리마켓 스캔 시각

        try:
            while True:
                try:
                    now_et = datetime.now(ET)
                    hour   = now_et.hour
                    minute = now_et.minute

                    # 프리마켓 스캔: 7:00~9:25 ET, 5분마다 실행
                    if 7 <= hour < 9 or (hour == 9 and minute < 25):
                        if time.time() - last_premarket_scan > 300:
                            premarket_scan(symbols + load_watchlist(
                                cfg.get("squeeze", {}).get("watchlist_file", "watchlists/symbols.txt"), []
                            ), cfg, gap_watchlist)
                            last_premarket_scan = time.time()

                    one_cycle(broker, data_client, db, cfg, symbols, args,
                              market_cache, gap_watchlist)
                except Exception as exc:
                    logging.error("[ERROR] 사이클 예외: %s", exc, exc_info=True)
                time.sleep(int(cfg.get("engine", {}).get("poll_seconds", 60)))
        except KeyboardInterrupt:
            print("\n[EXIT] 사용자 중단.")
    else:
        one_cycle(broker, data_client, db, cfg, symbols, args, market_cache, gap_watchlist)


if __name__ == "__main__":
    main()
