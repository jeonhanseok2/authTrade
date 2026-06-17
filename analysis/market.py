# analysis/market.py
"""
시장 전체 레짐(국면) 분석.
투자 전문가처럼 지수·VIX·수익률 곡선·섹터 강도를 종합해
현재 시장이 어떤 국면인지 판단한다.

레짐 정의:
  BULL       : S&P 500 > 200MA + VIX < 20            (위험자산 매수 환경)
  CORRECTION : S&P 500 > 200MA + VIX 20~30           (조정, 선별적 매수)
  BEAR       : S&P 500 < 200MA + VIX > 25            (주식 비중 축소)
  PANIC      : VIX > 35                              (극단적 공포, 역발상 매수 고려)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

# 지수 티커 매핑
_INDEX_TICKERS = {
    "SP500":   "^GSPC",   # S&P 500
    "NASDAQ":  "^IXIC",   # NASDAQ 종합
    "DOW":     "^DJI",    # 다우존스
    "VIX":     "^VIX",    # 공포지수
    "T10Y":    "^TNX",    # 미국 10년물 국채금리
    "T13W":    "^IRX",    # 미국 13주 T-Bill (단기 금리 프록시 — Yahoo Finance에 2년물 티커 없음)
}

# 섹터 ETF 매핑 (SPDR 기준)
_SECTOR_ETFS = {
    "기술":       "XLK",
    "금융":       "XLF",
    "에너지":     "XLE",
    "헬스케어":   "XLV",
    "산업":       "XLI",
    "커뮤니케이션": "XLC",
    "임의소비재": "XLY",
    "필수소비재": "XLP",
    "부동산":     "XLRE",
    "유틸리티":   "XLU",
    "소재":       "XLB",
}

# 캐시 (5분 TTL) — double-checked locking으로 스레드 안전 보장
_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL  = 300


def _cached(key: str, fn, *args):
    now = time.time()
    # 빠른 경로: 락 없이 캐시 확인
    entry = _CACHE.get(key)
    if entry and (now - entry[0]) < _CACHE_TTL:
        return entry[1]
    # 느린 경로: 락 획득 후 이중 확인 — 대기 중 다른 스레드가 채웠을 수 있음
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (now - entry[0]) < _CACHE_TTL:
            return entry[1]
        val = fn(*args)
        _CACHE[key] = (time.time(), val)
        return val


@dataclass
class MarketRegime:
    """시장 레짐 분석 결과."""
    regime:      str   = "unknown"  # bull / correction / bear / panic
    spx_price:   float = 0.0
    spx_vs_200ma: float = 0.0       # SPX 대비 200MA 괴리율 (%)
    vix:         float = 0.0
    yield_curve: float = 0.0        # 10Y - 2Y (bp) — 역전이면 음수
    trend_bias:  str   = "neutral"  # bullish / bearish / neutral
    summary:     str   = ""         # 한 줄 요약
    sector_strength: Dict[str, float] = field(default_factory=dict)


def _fetch_price_series(ticker: str, period: str = "1y", interval: str = "1d") -> Optional[pd.Series]:
    """단일 티커 종가 시리즈 반환. 실패 시 None."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, timeout=8)
        if df is not None and not df.empty:
            return df["Close"].dropna()
    except Exception as exc:
        logging.debug("[market] %s fetch failed: %s", ticker, exc)
    return None


def _fetch_last_price(ticker: str) -> float:
    """최신 종가 반환. 실패 시 0.0."""
    s = _fetch_price_series(ticker, period="5d", interval="1d")
    return float(s.iloc[-1]) if s is not None and not s.empty else 0.0


def _fetch_vix() -> float:
    """VIX 최신값."""
    return _cached("vix", _fetch_last_price, "^VIX")


def _fetch_spx_series() -> Optional[pd.Series]:
    """S&P 500 1년 일봉."""
    return _cached("spx_series", _fetch_price_series, "^GSPC", "1y", "1d")


def _compute_yield_curve() -> float:
    """
    10Y - 13W 스프레드 (bp). yf.download() 배치로 2개 티커를 1회 요청.
    역전(음수) = 경기침체 선행 지표.
    """
    try:
        raw = yf.download(
            ["^TNX", "^IRX"], period="5d", interval="1d",
            progress=False, auto_adjust=True, timeout=10,
        )
        if raw.empty:
            return 0.0
        closes = raw["Close"] if "Close" in raw.columns else raw
        t10 = float(closes["^TNX"].dropna().iloc[-1]) if "^TNX" in closes else 0.0
        t2  = float(closes["^IRX"].dropna().iloc[-1]) if "^IRX" in closes else 0.0
        if t10 > 0 and t2 > 0:
            return round(t10 - t2, 3)
    except Exception as exc:
        logging.debug("[market] yield curve error: %s", exc)
    return 0.0


def _fetch_sector_returns(period_days: int = 20) -> Dict[str, float]:
    """
    섹터 ETF 최근 N일 수익률(%) — yf.download() 배치로 12개 티커를 1회 요청.
    기존: Ticker().history() × 12회 → Yahoo 429 유발
    개선: download([SPY, XLK, ...]) 1회 → 요청 수 92% 감소
    """
    all_tickers = ["SPY"] + list(_SECTOR_ETFS.values())
    results: Dict[str, float] = {}
    try:
        raw = yf.download(
            all_tickers, period="3mo", interval="1d",
            progress=False, auto_adjust=True, timeout=15,
        )
        if raw.empty:
            return results
        # yf.download() 다중 티커: columns = MultiIndex (Price, Ticker)
        closes = raw["Close"] if "Close" in raw.columns else raw
        spy_s  = closes["SPY"].dropna() if "SPY" in closes else None
        spy_ret = None
        if spy_s is not None and len(spy_s) >= period_days:
            spy_ret = float(spy_s.iloc[-1] / spy_s.iloc[-period_days] - 1) * 100

        for sector_name, ticker in _SECTOR_ETFS.items():
            try:
                if ticker not in closes:
                    continue
                s = closes[ticker].dropna()
                if len(s) < period_days:
                    continue
                ret = float(s.iloc[-1] / s.iloc[-period_days] - 1) * 100
                results[sector_name] = round(ret - spy_ret, 2) if spy_ret is not None else round(ret, 2)
            except Exception:
                continue
    except Exception as exc:
        logging.debug("[market] sector returns error: %s", exc)
    return results


def analyze_market() -> MarketRegime:
    """
    시장 레짐 종합 분석 (5분 TTL 캐시 적용 — 중복 루프 호출 대비).
    - SPX 200MA 위치 확인
    - VIX 레벨 확인
    - 수익률 곡선 역전 여부
    - 섹터 강도 분석
    """
    return _cached("regime_result", _analyze_market_impl)


def _analyze_market_impl() -> MarketRegime:
    result = MarketRegime()

    # ── SPX 트렌드 ────────────────────────────────────────────────
    spx_s = _fetch_spx_series()
    if spx_s is not None and len(spx_s) >= 200:
        spx_price     = float(spx_s.iloc[-1])
        ma200         = float(spx_s.rolling(200).mean().iloc[-1])
        vs_200        = (spx_price - ma200) / ma200 * 100
        result.spx_price   = round(spx_price, 2)
        result.spx_vs_200ma = round(vs_200, 2)
    elif spx_s is not None and not spx_s.empty:
        result.spx_price = float(spx_s.iloc[-1])
        vs_200 = 0.0
    else:
        vs_200 = 0.0

    # ── VIX ──────────────────────────────────────────────────────
    vix = _fetch_vix()
    result.vix = round(vix, 2)

    # ── 수익률 곡선 ───────────────────────────────────────────────
    result.yield_curve = _fetch_yield_curve_cached()

    # ── 레짐 판단 ─────────────────────────────────────────────────
    above_200ma = vs_200 >= 0
    if vix > 35:
        result.regime     = "panic"
        result.trend_bias = "bearish"
        result.summary    = f"극단적 공포 (VIX={vix:.1f}). 역발상 매수 고려."
    elif not above_200ma and vix > 25:
        result.regime     = "bear"
        result.trend_bias = "bearish"
        result.summary    = f"하락장 (SPX 200MA 하방 {abs(vs_200):.1f}%, VIX={vix:.1f}). 현금 비중 확대."
    elif above_200ma and vix > 20:
        result.regime     = "correction"
        result.trend_bias = "neutral"
        result.summary    = f"조정 구간 (VIX={vix:.1f}). 우량주 선별 매수."
    elif above_200ma and vix <= 20:
        result.regime     = "bull"
        result.trend_bias = "bullish"
        result.summary    = f"상승장 (SPX 200MA 상방 {vs_200:.1f}%, VIX={vix:.1f}). 적극 매수."
    else:
        result.regime     = "unknown"
        result.trend_bias = "neutral"
        result.summary    = "데이터 부족."

    # 수익률 곡선 역전 경고 추가
    if result.yield_curve < 0:
        result.summary += f" ※ 수익률 곡선 역전({result.yield_curve:.2f}%) — 경기침체 선행 신호."

    # ── 섹터 강도 ─────────────────────────────────────────────────
    result.sector_strength = _fetch_sector_returns()

    return result


def _fetch_yield_curve_cached() -> float:
    return _cached("yield_curve", _compute_yield_curve)


def get_leading_sectors(market: MarketRegime, top_n: int = 3) -> list[str]:
    """
    현재 시장에서 강세인 섹터 반환.
    레짐별 기대 리더 섹터도 함께 고려.
    """
    ss = market.sector_strength
    if not ss:
        return []
    sorted_sectors = sorted(ss.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in sorted_sectors[:top_n]]


def format_market_report(market: MarketRegime) -> str:
    """텔레그램 전송용 시장 분석 요약 텍스트."""
    lines = [
        "📊 <b>시장 분석</b>",
        f"레짐: <b>{market.regime.upper()}</b>",
        f"S&P500: {market.spx_price:,.0f}  (200MA 대비 {market.spx_vs_200ma:+.1f}%)",
        f"VIX: {market.vix:.1f}",
        f"수익률 곡선(10Y-2Y): {market.yield_curve:+.2f}%",
    ]
    if market.sector_strength:
        top3 = sorted(market.sector_strength.items(), key=lambda x: x[1], reverse=True)[:3]
        lines.append("강세 섹터: " + ", ".join(f"{n}({v:+.1f}%)" for n, v in top3))
    lines.append(f"📝 {market.summary}")
    return "\n".join(lines)
