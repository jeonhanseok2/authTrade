# data/fetch.py
from datetime import datetime, timedelta, timezone
import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def _empty_df():
    return pd.DataFrame(
        columns=["timestamp","open","high","low","close","volume"]
    ).set_index("timestamp")


def yf_fallback(symbol: str, minutes: int) -> pd.DataFrame:
    """yfinance 단일 티커를 1번만 시도. 실패 시 빈 DF 반환."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        # 1분봉은 7일 제한 → 5일 충분, 장전/장후 포함
        df = t.history(period="5d", interval="1m",
                       actions=False, auto_adjust=False, prepost=True, timeout=10)
        if df is None or df.empty:
            return _empty_df()
        df = df.rename(columns=str.lower).reset_index()
        ts = "Datetime" if "Datetime" in df.columns else ("datetime" if "datetime" in df.columns else None)
        if ts:
            df = df.rename(columns={ts: "timestamp"})
        elif df.index.name and str(df.index.name).lower() in ("datetime","date","time"):
            df = df.rename_axis("timestamp").reset_index()
        else:
            return _empty_df()
        need = {"timestamp","open","high","low","close","volume"}
        if not need.issubset(df.columns):
            return _empty_df()
        return df[["timestamp","open","high","low","close","volume"]].tail(int(minutes)).set_index("timestamp")
    except Exception:
        return _empty_df()


def fetch_recent_bars(data_client: StockHistoricalDataClient, symbol: str, minutes: int = 600) -> pd.DataFrame:
    """Alpaca IEX → 실패하면 yfinance 1회 폴백."""
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
        if hasattr(bars, "df") and bars.df is not None and not bars.df.empty:
            df = bars.df.copy()
            # index 정리
            if df.index.names and "timestamp" in (df.index.names or []):
                df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            if "time" in df.columns and "timestamp" not in df.columns:
                df = df.rename(columns={"time":"timestamp"})
            if "symbol" in df.columns:
                df = df[df["symbol"] == symbol]
            need = {"timestamp","open","high","low","close","volume"}
            if need.issubset(df.columns) and not df.empty:
                out = df[["timestamp","open","high","low","close","volume"]].tail(minutes)
                if not out.empty:
                    return out.set_index("timestamp")
    except Exception as e:
        # 구독/권한 오류 등은 조용히 폴백
        pass

    # yfinance 폴백 (1회)
    return yf_fallback(symbol, minutes)
