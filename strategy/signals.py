# strategy/signals.py
"""
pandas/numpy 기반 기술적 지표 계산.
외부 TA 라이브러리 없이 순수 pandas/numpy로 구현.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────
# RSI (Relative Strength Index) — 상대강도지수
# 70 이상 과매수, 30 이하 과매도
# ─────────────────────────────────────────────────────────────────────
def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


# ─────────────────────────────────────────────────────────────────────
# MACD — 이동평균 수렴·발산
# histogram > 0 이면 상승 모멘텀
# ─────────────────────────────────────────────────────────────────────
def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(macd_line, signal_line, histogram) 반환."""
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


# ─────────────────────────────────────────────────────────────────────
# ATR (Average True Range) — 평균 진폭, 변동성 측정
# ─────────────────────────────────────────────────────────────────────
def compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


# ─────────────────────────────────────────────────────────────────────
# Bollinger Bands — 가격 변동 범위 (과매수/과매도 판단)
# ─────────────────────────────────────────────────────────────────────
def compute_bollinger(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(upper, mid, lower) 반환."""
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)
    upper = mid + std_mult * sigma
    lower = mid - std_mult * sigma
    return upper, mid, lower


# ─────────────────────────────────────────────────────────────────────
# Keltner Channel — ATR 기반 채널 (스퀴즈 감지에 사용)
# ─────────────────────────────────────────────────────────────────────
def compute_keltner_channel(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
    atr_mult: float = 1.5,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(upper, mid, lower) 반환."""
    ema = close.ewm(span=period, adjust=False).mean()
    atr = compute_atr(high, low, close, period)
    return ema + atr_mult * atr, ema, ema - atr_mult * atr


# ─────────────────────────────────────────────────────────────────────
# 선형 회귀 (Linear Regression) — 스퀴즈 모멘텀 방향 판단용
# ─────────────────────────────────────────────────────────────────────
def _linreg_series(series: pd.Series, period: int) -> pd.Series:
    """각 바의 선형 회귀 종단값 시리즈. 스퀴즈 모멘텀 내부 사용."""
    result = series * np.nan
    arr    = series.values.astype(float)
    x      = np.arange(period, dtype=float)
    for i in range(period - 1, len(arr)):
        y = arr[i - period + 1 : i + 1]
        if np.isnan(y).any():
            continue
        slope, intercept = np.polyfit(x, y, 1)
        result.iloc[i]   = intercept + slope * (period - 1)
    return result


# ─────────────────────────────────────────────────────────────────────
# TTM Squeeze Momentum Indicator
# (John Carter 방식 — 미국 단기 급등주/옵션 트레이더 표준 도구)
#
# 원리:
#   BB가 Keltner Channel 안으로 수렴 → 에너지 축적 (스퀴즈 ON)
#   BB가 KC 밖으로 이탈 → 브레이크아웃 발생 (스퀴즈 OFF)
#   Momentum 히스토그램:
#     양수 & 증가 → 강한 상승 돌파
#     음수 & 감소 → 강한 하락 돌파
# ─────────────────────────────────────────────────────────────────────
def compute_squeeze_momentum(
    df: pd.DataFrame,
    bb_period: int   = 20,
    bb_std:    float = 2.0,
    kc_period: int   = 20,
    kc_mult:   float = 1.5,
    mom_period: int  = 14,
) -> pd.DataFrame:
    """
    스퀴즈 모멘텀 지표 계산 후 df에 컬럼 추가.

    추가 컬럼:
      squeeze_on    : bool  — BB가 KC 안에 있으면 True (에너지 축적 중)
      squeeze_off   : bool  — 직전 squeeze_on이 True였다가 False로 전환 (발사 시점)
      squeeze_mom   : float — 모멘텀 히스토그램 (양수=상승, 음수=하락)
      squeeze_rising: bool  — 모멘텀이 증가 중 (상승 돌파 확인)
    """
    out   = df.copy()
    close = out["close"]
    high  = out["high"]
    low   = out["low"]

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = compute_bollinger(close, bb_period, bb_std)

    # Keltner Channel
    kc_upper, kc_mid, kc_lower = compute_keltner_channel(close, high, low, kc_period, kc_mult)

    # 스퀴즈 ON/OFF
    out["squeeze_on"]  = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    out["squeeze_off"] = out["squeeze_on"].shift(1, fill_value=False) & ~out["squeeze_on"]

    # 모멘텀 계산 (TTM 공식)
    highest_high = high.rolling(mom_period).max()
    lowest_low   = low.rolling(mom_period).min()
    midpoint     = (highest_high + lowest_low) / 2
    delta        = close - (midpoint + close.rolling(mom_period).mean()) / 2
    out["squeeze_mom"] = _linreg_series(delta, mom_period)

    # 모멘텀 증가 여부 (방향 확인)
    out["squeeze_rising"] = out["squeeze_mom"] > out["squeeze_mom"].shift(1)

    return out


# ─────────────────────────────────────────────────────────────────────
# SMA (단순 이동평균)
# ─────────────────────────────────────────────────────────────────────
def compute_sma(df: pd.DataFrame, fast: int = 10, slow: int = 30) -> pd.DataFrame:
    out = df.copy()
    out["sma_fast"] = out["close"].rolling(fast).mean()
    out["sma_slow"] = out["close"].rolling(slow).mean()
    return out


# ─────────────────────────────────────────────────────────────────────
# 통합 지표 — 한 번에 모든 지표 계산
# ─────────────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    RSI, MACD, ATR, Bollinger Bands, SMA, Squeeze Momentum을
    df 컬럼으로 추가해 반환.
    입력 df: [open, high, low, close, volume] + DatetimeIndex (오름차순).
    """
    out   = df.copy()
    close = out["close"]

    # RSI
    out["rsi_14"] = compute_rsi(close)

    # MACD
    macd, sig, hist = compute_macd(close)
    out["macd"]        = macd
    out["macd_signal"] = sig
    out["macd_hist"]   = hist

    # ATR (고/저/종가 필요)
    if {"high", "low"}.issubset(out.columns):
        out["atr_14"] = compute_atr(out["high"], out["low"], close)
        # Squeeze Momentum (OHLC 필요)
        out = compute_squeeze_momentum(out)
    else:
        out["atr_14"] = np.nan

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = compute_bollinger(close)
    out["bb_upper"] = bb_upper
    out["bb_mid"]   = bb_mid
    out["bb_lower"] = bb_lower

    # 이동평균 (단기/장기)
    out["sma_10"]  = close.rolling(10).mean()
    out["sma_20"]  = close.rolling(20).mean()
    out["sma_50"]  = close.rolling(50).mean()
    out["sma_200"] = close.rolling(200).mean()

    # 거래량 이동평균 (20봉 기준)
    if "volume" in out.columns:
        out["vol_ma20"] = out["volume"].rolling(20).mean()

    return out


# ─────────────────────────────────────────────────────────────────────
# 편의 헬퍼
# ─────────────────────────────────────────────────────────────────────

def latest_rsi(df: pd.DataFrame) -> float:
    """최신 RSI_14 값. 데이터 부족 시 50.0 반환."""
    col = "rsi_14"
    if col not in df.columns:
        df = compute_indicators(df)
    val = df[col].dropna()
    return float(val.iloc[-1]) if not val.empty else 50.0


def atr_for_sizing(df: pd.DataFrame, period: int = 14) -> float:
    """ATR 최신값. 데이터 부족 시 0.0 반환."""
    col = "atr_14"
    if col not in df.columns:
        df = compute_indicators(df)
    val = df[col].dropna()
    return float(val.iloc[-1]) if not val.empty else 0.0


def is_squeeze_fired(df: pd.DataFrame) -> tuple[bool, bool]:
    """
    스퀴즈 발사 여부 + 방향 반환.
    Returns:
        (fired: bool, is_upward: bool)
        fired=True + is_upward=True  → 상승 돌파 예상
        fired=True + is_upward=False → 하락 돌파 예상
    """
    if "squeeze_off" not in df.columns:
        df = compute_indicators(df)
    sq = df.dropna(subset=["squeeze_off", "squeeze_mom"])
    if sq.empty:
        return False, False
    last_row   = sq.iloc[-1]
    fired      = bool(last_row.get("squeeze_off", False))
    is_upward  = bool(last_row.get("squeeze_rising", False)) and float(last_row.get("squeeze_mom", 0)) > 0
    return fired, is_upward
