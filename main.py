import argparse, os, time
from datetime import datetime, timezone
import yaml

from utils.market_time import is_us_trading_session
from notify.telegram_notifier import send_telegram
from ai.gpt_helper import summarize_news

from config import load_mode_env
from strategy.risk import within_trade_window, market_circuit_breaker_triggered
from data.fetch import fetch_recent_bars

from strategy.entries import momentum_entry, value_entry
from strategy.exits import stop_loss_hit, take_profit_hit, trailing_stop_active
from data.fundamentals import fetch_quick_fundamentals
from news.check import is_positive_news

USE_PAPER_SIM = False
try:
    from trader.execution import AlpacaBroker
except Exception:
    USE_PAPER_SIM = True

from trader.paper import PaperSimBroker
from alpaca.data.historical import StockHistoricalDataClient


def load_watchlist(path, fallback):
    try:
        with open(path, "r") as f:
            ls = [x.strip().upper() for x in f if x.strip() and not x.startswith("#")]
            return ls or fallback
    except Exception:
        return fallback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--fast", type=int, default=10)   # 현재 사용 안 하지만 남겨둠
    parser.add_argument("--slow", type=int, default=30)   # 현재 사용 안 하지만 남겨둠
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--minutes", type=int, default=600)
    parser.add_argument("--ignore-window", action="store_true",
                        help="거래창/휴장 무시하고 강제 실행")
    parser.add_argument("--no-market-check", action="store_true",
                        help="서킷브레이커 체크 끄기")
    args = parser.parse_args()

    # 1) 설정/키 로드
    cfg = {}
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

    mode = load_mode_env()
    api_key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    print(f"[MODE] {mode} | paper={paper} | symbol={args.symbol}")

    # 2) 브로커/데이터 클라이언트
    if USE_PAPER_SIM or not api_key or not secret:
        print("[WARN] PaperSim mode (no live orders).")
        broker = PaperSimBroker()
        data_client = None
    else:
        broker = AlpacaBroker(api_key, secret, paper=paper)
        data_client = StockHistoricalDataClient(api_key, secret)

    print("[INFO] Account:", broker.get_account())

    # 3) 유니버스
    watchlist = load_watchlist(
        cfg.get("universe", {}).get("watchlist_file", "watchlists/symbols.txt"),
        cfg.get("universe", {}).get("fallback_symbols", ["AAPL", "MSFT", "SPY"]),
    )
    symbols = [args.symbol] if args.symbol else watchlist

    # 4) 한 사이클 실행 (클로저)
    def one_cycle():
        now = datetime.now(timezone.utc)

        # (A) 휴장 시 호출 스킵 (옵션으로 무시 가능)
        skip_closed = cfg.get("engine", {}).get("skip_calls_when_market_closed", True)
        if skip_closed and not args.ignore_window and not is_us_trading_session(now):
            print("[SKIP] US 시장 휴장 시간 → 외부 데이터 호출 생략")
            return

        # (B) 거래창 제한 (장 시작 N분 후 ~ 마감 M분 전)
        tw = cfg.get("engine", {}).get("trade_window", {})
        if not args.ignore_window and not within_trade_window(
                now,
                tw.get("start_minutes_after_open", 5),
                tw.get("end_minutes_before_close", 5),
        ):
            print("[SKIP] Out of trade window")
            return

        # (C) 시장 서킷브레이커 (옵션으로 비활성화 가능)
        if data_client and not args.no_market_check:
            breaker_syms = ["^GSPC", "VOO", "SPY"]
            spy_df = None
            for bs in breaker_syms:
                spy_df = fetch_recent_bars(data_client, bs, minutes=min(args.minutes, 600))
                if spy_df is not None and not spy_df.empty:
                    break
            if spy_df is not None and not spy_df.empty and market_circuit_breaker_triggered(spy_df):
                print("[HALT] Market breaker (S&P ~ -7% intraday); pause")
                return

        # (D) 분봉 수집
        dfs = {}
        for s in symbols[:40]:
            try:
                dfs[s] = fetch_recent_bars(data_client, s, minutes=args.minutes) if data_client else None
            except Exception as e:
                print(f"[WARN] fetch failed for {s}: {e}")
                dfs[s] = None
            time.sleep(0.05)

        # (E) 급등주 후보
        mom_cfg = cfg.get("momentum_rules", {})
        mom_budget = float(cfg.get("budgets", {}).get("momentum_bucket_usd", 3000))
        mom_max = int(cfg.get("budgets", {}).get("momentum_max_positions", 3))
        momentum_candidates = []
        for sym, df in dfs.items():
            if df is None or df.empty:
                continue
            if momentum_entry(df, mom_cfg):
                momentum_candidates.append(sym)
        momentum_candidates = momentum_candidates[:mom_max]

        # (F) 저평가 후보
        val_cfg = cfg.get("value_rules", {})
        val_budget = float(cfg.get("budgets", {}).get("value_bucket_usd", 17000))
        val_max = int(cfg.get("budgets", {}).get("value_max_positions", 8))
        fundamentals = fetch_quick_fundamentals(symbols[:50])
        value_candidates = [it["symbol"] for it in fundamentals if value_entry(it, val_cfg)][:val_max]

        # (G) 매매 실행
        acct = broker.get_account()
        positions = broker.list_positions() if hasattr(broker, "list_positions") else {}
        # cash = float(acct.get("cash", 0.0))  # 필요시 사용

        # 급등주 버킷: 소액 진입, 긍정 뉴스면 약간 증액
        per_mom_budget = mom_budget / max(1, len(momentum_candidates)) if momentum_candidates else 0.0
        for sym in momentum_candidates:
            df = dfs.get(sym)
            if df is None or df.empty:
                continue
            price = float(df["close"].iloc[-1])
            qty = int(per_mom_budget // max(price, 1.0))
            if qty <= 0:
                continue
            if float(positions.get(sym, 0.0)) > 0:
                continue

            if is_positive_news(sym, cfg.get("news", {}).get("positive_keywords", [])):
                qty = max(qty, int(qty * 1.2))

            print(f"[MOM] BUY {sym} qty={qty} price≈{price}")
            if hasattr(broker, "submit_market_order"):
                resp = broker.submit_market_order(sym, qty, "buy")
                send_telegram(f"🟢 <b>BUY</b> {sym} x{qty}")
                # GPT 요약(선택): news_text 를 확보했을 때만
                if cfg.get("notify", {}).get("gpt", {}).get("enabled", False):
                    news_text = ""  # TODO: 실제 뉴스 텍스트 연결
                    if news_text:
                        summary = summarize_news(sym, news_text)
                        if summary:
                            send_telegram(f"📰 <b>{sym} 뉴스 요약</b>\n{summary}")

        # 저평가 버킷: 스윙/장기
        per_val_budget = val_budget / max(1, len(value_candidates)) if value_candidates else 0.0
        for sym in value_candidates:
            df = dfs.get(sym)
            if df is None or df.empty:
                continue
            price = float(df["close"].iloc[-1])
            qty = int(per_val_budget // max(price, 1.0))
            if qty <= 0:
                continue
            if float(positions.get(sym, 0.0)) > 0:
                continue

            print(f"[VAL] BUY {sym} qty={qty} price≈{price}")
            if hasattr(broker, "submit_market_order"):
                resp = broker.submit_market_order(sym, qty, "buy")
                send_telegram(f"🟢 <b>BUY</b> {sym} x{qty}")

        # 리스크 관리 (데모: entry/peak 상태 저장은 추후 보강)
        risk_cfg = cfg.get("risk", {})
        for sym, qty in list(positions.items()):
            if qty <= 0:
                continue
            df = dfs.get(sym)
            if df is None or df.empty:
                continue
            last = float(df["close"].iloc[-1])
            entry = last * 0.98   # TODO: 실 엔트리 가격 저장 후 사용
            peak  = last * 1.02   # TODO: 피크 갱신 로직 추가
            if stop_loss_hit(entry, last, risk_cfg):
                print(f"[RISK] STOP {sym} qty={int(qty)} last={last}")
                broker.submit_market_order(sym, int(qty), "sell")
                send_telegram(f"🔴 <b>EXIT</b> {sym} x{int(qty)} (stop loss)")
                continue
            if take_profit_hit(entry, last, risk_cfg) and trailing_stop_active(entry, last, peak, risk_cfg):
                print(f"[RISK] TRAIL STOP {sym} qty={int(qty)} last={last}")
                broker.submit_market_order(sym, int(qty), "sell")
                send_telegram(f"🔴 <b>EXIT</b> {sym} x{int(qty)} (trailing stop)")

        print("[DBG] tick ", datetime.now().isoformat())

    # 5) 루프
    if args.loop:
        try:
            while True:
                one_cycle()
                time.sleep(int(cfg.get("engine", {}).get("poll_seconds", 60)))
        except KeyboardInterrupt:
            print("\n[EXIT] Stopped by user.")
    else:
        one_cycle()


if __name__ == "__main__":
    main()
