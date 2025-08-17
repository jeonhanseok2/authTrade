from datetime import datetime, timedelta, timezone
import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def yf_fallback(symbol: str, minutes: int) -> pd.DataFrame:
    try:
        import yfinance as yf
    except Exception:
        return pd.DataFrame(columns=[ "timestamp","open","high","low","close","volume"]).set_index("timestamp")
    data = yf.download(symbol, period="1d", interval="1m", progress=False)
    if data is None or data.empty:
        return pd.DataFrame(columns=[ "timestamp","open","high","low","close","volume"]).set_index("timestamp")
    df = data.rename(columns=str.lower).reset_index().rename(columns={"Datetime":"timestamp","datetime":"timestamp"})
    cols = {"timestamp","open","high","low","close","volume"}
    if not cols.issubset(df.columns):
        return pd.DataFrame(columns=[ "timestamp","open","high","low","close","volume"]).set_index("timestamp")
    return df[["timestamp","open","high","low","close","volume"]].tail(minutes).set_index("timestamp")

def fetch_recent_bars(data_client: StockHistoricalDataClient, symbol: str, minutes: int = 600) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes + 5)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            limit=minutes + 10,
            adjustment="raw",
            feed="iex",
        )
        bars = data_client.get_stock_bars(req)
    except Exception as e:
        print("[WARN] Alpaca data error:", e, "| Using yfinance fallback")
        return yf_fallback(symbol, minutes)

    if hasattr(bars, "df") and bars.df is not None:
        df = bars.df.copy()
        if df.index.names and "timestamp" in (df.index.names or []):
            df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        if "timestamp" not in df.columns and "time" in df.columns:
            df = df.rename(columns={"time":"timestamp"})
        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
        need = {"timestamp","open","high","low","close","volume"}
        if not need.issubset(df.columns) or df.empty:
            print("[INFO] Alpaca bars empty/missing, using yfinance fallback")
            return yf_fallback(symbol, minutes)
        out = df[["timestamp","open","high","low","close","volume"]].tail(minutes)
        return out.set_index("timestamp") if not out.empty else yf_fallback(symbol, minutes)

    data = getattr(bars, "data", None)
    seq = data.get(symbol, []) if isinstance(data, dict) else (data or [])
    rows = [{
        "timestamp": getattr(b, "timestamp", None),
        "open": float(getattr(b, "open", 0.0)),
        "high": float(getattr(b, "high", 0.0)),
        "low":  float(getattr(b, "low", 0.0)),
        "close":float(getattr(b, "close", 0.0)),
        "volume":int(getattr(b, "volume", 0)),
    } for b in seq or []]
    df = pd.DataFrame(rows).dropna(subset=["timestamp"])
    return df.set_index("timestamp") if not df.empty else yf_fallback(symbol, minutes)
