import pandas as pd

def normalize_df(df: pd.DataFrame):
    if df is None: 
        return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"]).set_index("timestamp")
    if "timestamp" in df.columns:
        return df.set_index("timestamp")
    if df.index.name and str(df.index.name).lower() in {"timestamp","time"}:
        df = df.reset_index().rename(columns={"time":"timestamp"})
        return df.set_index("timestamp")
    if "time" in df.columns:
        df = df.rename(columns={"time":"timestamp"})
        return df.set_index("timestamp")
    return df
