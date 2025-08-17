import argparse, os, time
from datetime import datetime, timezone
import yaml, pandas as pd

from config import load_mode_env
from strategy.signals import compute_sma
from strategy.risk import within_trade_window, market_circuit_breaker_triggered
from data.fetch import fetch_recent_bars
from data.screener import top_momentum

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
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--minutes", type=int, default=600)
    args = parser.parse_args()

    cfg = {}
    if os.path.exists("config.yaml"):
        with open("config.yaml","r") as f:
            cfg = yaml.safe_load(f) or {}
    mode = load_mode_env()
    api_key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER","true").lower()=="true"

    print(f"[MODE] {mode} | paper={paper} | symbol={args.symbol}")

    if USE_PAPER_SIM or not api_key or not secret:
        print("[WARN] PaperSim mode (no live orders).")
        broker = PaperSimBroker()
        data_client = None
    else:
        broker = AlpacaBroker(api_key, secret, paper=paper)
        data_client = StockHistoricalDataClient(api_key, secret)

    print("[INFO] Account:", broker.get_account())

    watchlist = load_watchlist(cfg.get("universe",{}).get("watchlist_file","watchlists/symbols.txt"),
                               cfg.get("universe",{}).get("fallback_symbols",["AAPL","MSFT","SPY"]))
    symbols = [args.symbol] if args.symbol else watchlist

    def one_cycle():
        now = datetime.now(timezone.utc)
        tw = cfg.get("engine",{}).get("trade_window",{})
        if not within_trade_window(now, tw.get("start_minutes_after_open",5), tw.get("end_minutes_before_close",5)):
            print("[SKIP] Out of trade window")
            return
        # market breaker check via SPY
        if data_client:
            spy = fetch_recent_bars(data_client, "SPY", minutes=min(args.minutes, 600))
            if market_circuit_breaker_triggered(spy):
                print("[HALT] Market breaker (SPY -7% intraday); pause")
                return

        # pull bars
        if data_client:
            dfs = {s: fetch_recent_bars(data_client, s, minutes=args.minutes) for s in symbols[:30]}
        else:
            # sim mode: fake data
            dfs = {}
        # momentum pick
        mom = top_momentum(dfs, min_change_pct=cfg.get("momentum_rules",{}).get("min_intraday_change_pct",5.0),
                           vol_spike_ratio=cfg.get("momentum_rules",{}).get("vol_spike_ratio",2.0),
                           limit=cfg.get("budgets",{}).get("momentum_max_positions",3))
        focus = mom or (symbols[:1])
        # simple SMA on focus symbols (demo)
        for sym in focus:
            df = dfs.get(sym)
            if df is None or df.empty:
                print(f"[WARN] No bars for {sym}")
                continue
            out = compute_sma(df, args.fast, args.slow)
            if out[["sma_fast","sma_slow"]].isna().any().any():
                print("[INFO] Not enough data for SMA yet.")
                continue
            last = out.iloc[-1]; prev = out.iloc[-2]
            price = float(last["close"])
            if hasattr(broker,"set_price"):
                broker.set_price(sym, price)
            acct = broker.get_account(); pos = broker.list_positions().get(sym, 0.0) if hasattr(broker,"list_positions") else 0.0
            buy = bool(last["sma_fast"] > last["sma_slow"] and prev["sma_fast"] <= prev["sma_slow"])
            sell= bool(last["sma_fast"] < last["sma_slow"] and prev["sma_fast"] >= prev["sma_slow"])
            print(f"[{sym}] price={price:.2f} pos={pos} buy={buy} sell={sell}")
            if buy and hasattr(broker,"submit_market_order"):
                qty = max(int((acct["cash"]*0.2)/max(price,1)), 1)
                print("BUY:", broker.submit_market_order(sym, qty, "buy"))
            if sell and hasattr(broker,"submit_market_order") and pos>0:
                print("SELL:", broker.submit_market_order(sym, int(pos), "sell"))

    if args.loop:
        while True:
            one_cycle()
            time.sleep(int(cfg.get("engine",{}).get("poll_seconds",60)))
    else:
        one_cycle()

if __name__ == "__main__":
    main()
