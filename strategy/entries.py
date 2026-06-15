# strategy/entries.py
"""
진입 조건 함수 모음.

변경 내역:
  value_entry     : RSI 임계치 40 → 30 (과매도 구간만 진입)
  gap_and_go_entry: 볼륨 팩터 추가 — 첫 5분봉 거래량 >= 프리마켓 평균 × 5배
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from strategy.signals import compute_indicators, latest_rsi


def momentum_entry(df: pd.DataFrame, rules: Dict[str, Any]) -> bool:
    """급등주: 변동률 + 거래량 스파이크 + RSI/MACD 진입 확인."""
    if df is None or df.empty or not {"close", "volume"}.issubset(df.columns):
        return False

    look = int(rules.get("lookback_minutes", 120))
    df   = df.tail(max(look, 30))
    if df.empty:
        return False

    first = float(df.iloc[0]["close"])
    last  = float(df.iloc[-1]["close"])
    if first <= 0:
        return False

    change_pct = (last - first) / first * 100.0
    if change_pct < float(rules.get("min_intraday_change_pct", 5.0)):
        return False

    avg_vol = (
        df["volume"].rolling(20).mean().iloc[-1]
        if len(df) >= 20
        else df["volume"].mean()
    )
    if df["volume"].iloc[-1] < float(rules.get("vol_spike_ratio", 2.0)) * (avg_vol or 1.0):
        return False

    if last < float(rules.get("min_price_usd", 3.0)):
        return False

    # RSI 과매수 진입 차단
    rsi_max = float(rules.get("rsi_entry_max", 75.0))
    df_ind  = compute_indicators(df)
    rsi_val = latest_rsi(df_ind)
    if rsi_val > rsi_max:
        return False

    # MACD 히스토그램 양수 확인 (상승 모멘텀)
    if rules.get("require_macd_positive", True):
        hist_col = df_ind["macd_hist"].dropna()
        if not hist_col.empty and float(hist_col.iloc[-1]) <= 0:
            return False

    return True


def value_entry(
    info:  Dict[str, Any],
    rules: Dict[str, Any],
    df:    Optional[pd.DataFrame] = None,
) -> bool:
    """
    저평가 가치주: 시총/PER/EPS성장/유동성 + RSI 과매도 확인.

    RSI 기준: < 30 (과매도 구간만 진입 — 고점 매수 완전 차단)
    """
    mcap = float(info.get("marketCap") or 0)
    if mcap <= 0 or mcap >= float(rules.get("max_market_cap_usd", 5e9)):
        return False

    pe = info.get("trailingPE")
    if not pe or pe <= 0:
        return False

    group_pe = float(info.get("groupPe") or pe * 2)
    if pe >= float(rules.get("max_per_vs_group", 0.7)) * group_pe:
        return False

    if float(info.get("epsGrowth") or 0.0) < float(rules.get("min_eps_growth", 0.10)):
        return False

    if float(info.get("avgDollarVolume") or 0.0) < float(rules.get("min_liquidity_usd", 1_000_000)):
        return False

    # RSI < 30 — 과매도 구간만 진입 (40→30으로 강화)
    if df is not None and not df.empty:
        rsi_threshold = float(rules.get("rsi_entry_threshold", 30))  # 기존 40 → 30
        rsi_val       = latest_rsi(compute_indicators(df))
        if rsi_val > rsi_threshold:
            return False

    return True


def gap_and_go_volume_factor(
    df_5min:          pd.DataFrame,
    premarket_avg_vol: float,
    factor:           float = 5.0,
) -> bool:
    """
    Gap&Go 볼륨 팩터 — 첫 5분봉 거래량이 프리마켓 평균 거래량의 N배 이상인지 확인.

    진짜 Gap&Go 조건: 장 시작 직후 첫 캔들에 세력이 몰리는 것을 확인.
    기준: 첫 5분봉 volume >= premarket_avg_vol × factor

    Args:
        df_5min:          5분봉 데이터 (장 시작 후 데이터 포함)
        premarket_avg_vol: 프리마켓 평균 거래량 (GapCandidate에서 추출)
        factor:            배수 기준 (기본 5배)

    Returns:
        True → 볼륨 팩터 충족 (진입 가능)
    """
    if df_5min is None or df_5min.empty:
        return False
    if premarket_avg_vol <= 0:
        return True  # 프리마켓 거래량 정보 없으면 차단하지 않음

    # 장 시작 후 첫 캔들 (9:30 ET 기준)
    first_candle_vol = float(df_5min.iloc[0]["volume"]) if "volume" in df_5min.columns else 0.0
    return first_candle_vol >= premarket_avg_vol * factor
