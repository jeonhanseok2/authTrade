# strategy/etf_swing.py
"""
버킷 2: ETF 스윙/장기/단기 전략.

ETF 유니버스:
  광의 시장: SPY(S&P500), QQQ(NASDAQ100), IWM(소형주), DIA(다우)
  섹터: XLK(기술), XLF(금융), XLE(에너지), XLV(헬스케어), XLI(산업)
  테마: GLD(금), TLT(장기채), UUP(달러), EEM(신흥국)

타임프레임 분류:
  단기 (1~3일):  모멘텀 강함 + 변동성 확대 구간
  스윙 (1~4주):  추세 전환 초기 + 섹터 로테이션
  장기 (수개월): 레짐 전환 후 포지션 누적

진입 기준 (공통):
  - 레짐 필터: bear/panic에서 역방향 ETF(SQQQ/SDS) 외 롱 금지
  - SMA 추세: 단기 > 레짐별 MA 조건
  - RSI: 과매도 회복 신호
  - 거래량 확인: 거래량 > 20일 평균

청산 기준:
  - 단기: +3% TP / -2% SL
  - 스윙: +8% TP / -4% SL + 트레일링
  - 장기: +20% TP / -8% SL + 트레일링
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from analysis.market import MarketRegime
from strategy.signals import compute_indicators, latest_rsi

# ETF 유니버스 정의
ETF_UNIVERSE = {
    # 광의 시장 (롱)
    "SPY":  {"name": "S&P 500",      "category": "broad",    "direction": "long"},
    "QQQ":  {"name": "NASDAQ 100",   "category": "broad",    "direction": "long"},
    "IWM":  {"name": "Russell 2000", "category": "broad",    "direction": "long"},
    "DIA":  {"name": "Dow Jones",    "category": "broad",    "direction": "long"},
    # 섹터 ETF (롱)
    "XLK":  {"name": "기술",         "category": "sector",   "direction": "long"},
    "XLF":  {"name": "금융",         "category": "sector",   "direction": "long"},
    "XLE":  {"name": "에너지",       "category": "sector",   "direction": "long"},
    "XLV":  {"name": "헬스케어",     "category": "sector",   "direction": "long"},
    "XLI":  {"name": "산업",         "category": "sector",   "direction": "long"},
    "XLY":  {"name": "임의소비재",   "category": "sector",   "direction": "long"},
    "XLC":  {"name": "커뮤니케이션", "category": "sector",   "direction": "long"},
    # 안전자산
    "GLD":  {"name": "금",           "category": "safe",     "direction": "long"},
    "TLT":  {"name": "장기채",       "category": "safe",     "direction": "long"},
    # 역방향 (헤지/bear 레짐용)
    "SQQQ": {"name": "QQQ 3배 인버스", "category": "inverse", "direction": "short"},
    "SDS":  {"name": "S&P 2배 인버스", "category": "inverse", "direction": "short"},
}

# 타임프레임별 목표/손절 기준
TIMEFRAME_PARAMS = {
    "short": {"tp_pct": 0.03, "sl_pct": 0.02, "trail_pct": None},
    "swing": {"tp_pct": 0.08, "sl_pct": 0.04, "trail_pct": 0.03},
    "long":  {"tp_pct": 0.20, "sl_pct": 0.08, "trail_pct": 0.06},
}


def classify_timeframe(df: pd.DataFrame, regime: str = "bull") -> str:
    """
    현재 상황에 맞는 ETF 투자 타임프레임 분류.

    판단 기준:
      - 단기:  RSI > 60 + 거래량 급증 + bull/correction 레짐
      - 스윙:  SMA10 > SMA50 (단기 추세 상승) + 레짐 안정적
      - 장기:  레짐 bull + SMA50 > SMA200 (장기 상승 추세)
    """
    if "rsi_14" not in df.columns or "sma_10" not in df.columns:
        df = compute_indicators(df)

    last    = df.iloc[-1]
    rsi     = float(last.get("rsi_14", 50))
    sma10   = float(last.get("sma_10", 0) or 0)
    sma50   = float(last.get("sma_50", 0) or 0)
    sma200  = float(last.get("sma_200", 0) or 0)
    vol     = float(last.get("volume", 0) or 0)
    vol_ma  = float(last.get("vol_ma20", 1) or 1)

    # 거래량 급증 (평균의 1.5배 이상)
    volume_surge = vol > vol_ma * 1.5

    if regime in ("bull", "correction") and rsi > 60 and volume_surge:
        return "short"
    elif sma10 > 0 and sma50 > 0 and sma10 > sma50:
        return "swing"
    elif sma50 > 0 and sma200 > 0 and sma50 > sma200:
        return "long"
    else:
        return "swing"  # 기본값


def etf_swing_entry(
    symbol:    str,
    df:        pd.DataFrame,
    market:    MarketRegime,
    timeframe: Optional[str] = None,
) -> tuple[bool, str, str]:
    """
    ETF 진입 여부 판단.

    Returns:
        (should_enter: bool, reason: str, timeframe: str)
    """
    etf_info = ETF_UNIVERSE.get(symbol)
    if etf_info is None:
        return False, f"{symbol}은 ETF 유니버스에 없음", ""

    # ── 레짐 기반 방향성 필터 ─────────────────────────────────────────
    if market.regime in ("bear", "panic"):
        # bear/panic에서는 inverse ETF만 롱 허용, 일반 ETF는 차단
        if etf_info["direction"] != "short":
            return False, f"레짐={market.regime}: 일반 ETF 롱 진입 금지", ""
    elif market.regime in ("bull", "correction"):
        # bull/correction에서는 inverse ETF 차단
        if etf_info["direction"] == "short":
            return False, "레짐=bull: 인버스 ETF 진입 불필요", ""

    # ── 지표 계산 ─────────────────────────────────────────────────────
    if "rsi_14" not in df.columns:
        df = compute_indicators(df)

    last   = df.iloc[-1]
    rsi    = float(last.get("rsi_14", 50))
    close  = float(last.get("close", 0))
    sma50  = float(last.get("sma_50", 0) or 0)
    sma200 = float(last.get("sma_200", 0) or 0)
    vol    = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma20", 1) or 1)

    # ── 타임프레임 결정 ───────────────────────────────────────────────
    tf = timeframe or classify_timeframe(df, market.regime)

    # ── 공통 진입 필터 ────────────────────────────────────────────────
    if rsi > 75:
        return False, f"RSI 과매수 ({rsi:.1f} > 75)", tf

    if vol > 0 and vol_ma > 0 and vol < vol_ma * 0.5:
        return False, "거래량 부족 (20일 평균의 50% 미만)", tf

    # ── 추세 필터 (타임프레임별) ──────────────────────────────────────
    if tf == "long":
        if sma50 > 0 and sma200 > 0 and sma50 < sma200:
            return False, f"장기 추세 하락 (SMA50 {sma50:.2f} < SMA200 {sma200:.2f})", tf
    elif tf == "swing":
        if sma50 > 0 and close < sma50 * 0.97:
            return False, f"SMA50 하방 ({close:.2f} < SMA50 {sma50:.2f})", tf

    # ── 강세 섹터 필터 (섹터 ETF의 경우) ─────────────────────────────
    if etf_info["category"] == "sector":
        korean_name = etf_info["name"]
        sector_ret  = market.sector_strength.get(korean_name, 0.0)
        if sector_ret < -2.0:
            return False, f"섹터 약세 ({korean_name}: {sector_ret:+.1f}% vs SPY)", tf

    params = TIMEFRAME_PARAMS[tf]
    reason = (
        f"ETF 진입 ({tf}) — RSI:{rsi:.1f}, "
        f"TP:{params['tp_pct']*100:.0f}%, SL:{params['sl_pct']*100:.0f}%"
    )
    return True, reason, tf


def etf_swing_exit(
    symbol:        str,
    entry_price:   float,
    current_price: float,
    peak_price:    float,
    timeframe:     str = "swing",
    regime:        str = "bull",
) -> tuple[bool, str]:
    """
    ETF 청산 여부 판단.

    Returns:
        (should_exit: bool, reason: str)
    """
    if entry_price <= 0:
        return False, ""

    params  = TIMEFRAME_PARAMS.get(timeframe, TIMEFRAME_PARAMS["swing"])
    pnl_pct = (current_price - entry_price) / entry_price

    # ── 손절 ─────────────────────────────────────────────────────────
    if pnl_pct <= -params["sl_pct"]:
        return True, f"손절 ({pnl_pct*100:.1f}% <= -{params['sl_pct']*100:.0f}%)"

    # ── 목표가 ───────────────────────────────────────────────────────
    if pnl_pct >= params["tp_pct"]:
        return True, f"목표가 ({pnl_pct*100:.1f}% >= +{params['tp_pct']*100:.0f}%)"

    # ── 트레일링 스탑 ─────────────────────────────────────────────────
    trail_pct = params.get("trail_pct")
    if trail_pct and peak_price > entry_price:
        drawdown = (current_price - peak_price) / peak_price
        if drawdown <= -trail_pct:
            return True, f"트레일링 스탑 (고점 대비 {drawdown*100:.1f}%)"

    # ── 레짐 급변 ────────────────────────────────────────────────────
    if regime == "panic" and pnl_pct > 0:
        return True, "레짐=panic 전환: 수익 실현"

    return False, ""


def get_etf_candidates(market: MarketRegime) -> list[str]:
    """
    현재 레짐에 맞는 ETF 후보 목록 반환.
    """
    candidates = []
    if market.regime in ("bull", "correction"):
        # 강세 섹터 ETF 우선 추가
        sector_etf_map = {
            "기술": "XLK", "금융": "XLF", "에너지": "XLE",
            "헬스케어": "XLV", "산업": "XLI", "임의소비재": "XLY",
        }
        strong_sectors = sorted(
            market.sector_strength.items(), key=lambda x: x[1], reverse=True
        )[:3]
        for sector_name, _ in strong_sectors:
            etf = sector_etf_map.get(sector_name)
            if etf:
                candidates.append(etf)
        # 광의 시장 ETF 추가
        candidates += ["SPY", "QQQ"]

    elif market.regime == "bear":
        # 안전자산 + 인버스
        candidates += ["GLD", "TLT", "SQQQ", "SDS"]

    elif market.regime == "panic":
        # 현금 포지션 유지 + 소량 안전자산
        candidates += ["GLD", "TLT"]

    return list(dict.fromkeys(candidates))  # 중복 제거, 순서 유지


# ─────────────────────────────────────────────────────────────────────
# B2 스윙 모드 전용 — MA20 + RSI30 과매도 반등 진입
# 외부(B3 vs B2) 레짐은 RegimeEngine 이 결정하며,
# 이 함수는 B2_SWING 내부의 구체적 진입 타점을 계산합니다.
# ─────────────────────────────────────────────────────────────────────

# B2 공격 모드: 레버리지 ETF (bull market)
B2_LEVERAGE_UNIVERSE: list[str] = ["TQQQ", "SOXL", "FNGU"]
# B2 방어 모드: 지수 ETF (correction/sideways)
B2_DEFENSE_UNIVERSE:  list[str] = ["QQQ", "SPY"]


def swing_b2_entry(
    symbol:     str,
    df:         pd.DataFrame,   # 일봉 (최소 25일 이상 권장)
    atr:        float = 0.0,    # ATR 값 (0이면 추정치 사용)
) -> tuple[bool, str, float]:
    """
    B2 스윙 진입 판단 — MA20 지지 + RSI(30) 과매도 반등.

    진입 조건:
      1. 현재가 ≥ MA20 (추세 유지 확인)
      2. 최근 RSI < 35 (과매도 구간 진입)
      3. 현재봉 close > 이전봉 close (반등 시작 확인)
      4. 거래량 > 20일 평균 (매수세 동반 확인)

    Returns:
        (should_enter, reason, atr_trailing_stop_price)
        trailing_stop = current_price - atr × 1.5
    """
    if df is None or len(df) < 20:
        return False, "데이터 부족 (20일 이상 필요)", 0.0

    if "rsi_14" not in df.columns or "sma_10" not in df.columns:
        df = compute_indicators(df)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    close      = float(last.get("close", 0) or 0)
    prev_close = float(prev.get("close", 0) or 0)
    rsi        = float(last.get("rsi_14", 50) or 50)
    prev_rsi   = float(prev.get("rsi_14", 50) or 50)
    vol        = float(last.get("volume", 0) or 0)
    vol_ma     = float(last.get("vol_ma20", 1) or 1)

    # MA20 계산 (sma_50이 없으면 rolling 직접 계산)
    if "sma_20" in df.columns:
        ma20 = float(last.get("sma_20", 0) or 0)
    else:
        ma20 = float(df["close"].rolling(20, min_periods=10).mean().iloc[-1] or 0)

    if close <= 0 or ma20 <= 0:
        return False, "가격/MA20 데이터 없음", 0.0

    # 조건 1: 현재가 >= MA20
    if close < ma20:
        return False, f"MA20 하방 (${close:.2f} < MA20 ${ma20:.2f}) — 방어 진입 차단", 0.0

    # 조건 2: 직전봉 RSI < 35 (과매도 확인)
    if prev_rsi >= 35:
        return False, f"RSI 과매도 미충족 (직전 RSI {prev_rsi:.1f} >= 35)", 0.0

    # 조건 3: 현재봉 반등 확인
    if close <= prev_close:
        return False, "반등 미확인 (현재봉 close ≤ 직전봉)", 0.0

    # 조건 4: 거래량 확인
    if vol_ma > 0 and vol < vol_ma * 0.8:
        return False, f"거래량 부족 ({vol/vol_ma:.1f}x)", 0.0

    # ATR 트레일링 스탑 (ATR×1.5)
    if atr <= 0:
        # ATR 추정: 최근 14봉 TR 평균
        try:
            hi = df["high"].tail(14)
            lo = df["low"].tail(14)
            cl = df["close"].shift(1).tail(14)
            tr = pd.concat([hi - lo, (hi - cl).abs(), (lo - cl).abs()], axis=1).max(axis=1)
            atr = float(tr.mean() or 0)
        except Exception:
            atr = close * 0.02   # 2% 추정 fallback

    trailing_stop = max(close - atr * 1.5, close * 0.92)   # 최소 -8% 하드플로어

    reason = (
        f"B2 스윙 진입 — MA20 지지(${ma20:.2f}) + RSI 반등({prev_rsi:.0f}→{rsi:.0f}), "
        f"거래량 {vol/vol_ma:.1f}x, ATR×1.5 스탑 ${trailing_stop:.2f}"
    )
    return True, reason, trailing_stop
