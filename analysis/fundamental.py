# analysis/fundamental.py
"""
펀더멘털 심층 분석 모듈.
PER·PBR·ROE·배당·부채비율·EPS성장을 종합해 가치주 점수를 산출하고,
간이 DCF(현금흐름할인)로 내재가치 추정까지 수행.

점수 기준 (총 100점):
  PER    < 15 → 20점  (저평가)
  PBR    < 2  → 20점  (자산 대비 저평가)
  ROE    > 15 → 20점  (자본 효율성)
  배당수익률 > 2% → 15점
  부채비율  < 50% → 15점  (재무 안정)
  EPS성장률 > 10% → 10점
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yfinance as yf

# 캐시 (4시간 TTL — 일중 업데이트 없음)
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 14_400


def _cached(key: str, fn, *args):
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _CACHE_TTL:
        return _CACHE[key][1]
    val = fn(*args)
    _CACHE[key] = (now, val)
    return val


@dataclass
class FundamentalScore:
    """펀더멘털 분석 결과."""
    symbol:           str   = ""
    score:            float = 0.0    # 종합 점수 (0~100)
    per:              float = 0.0    # Price-to-Earnings
    pbr:              float = 0.0    # Price-to-Book
    roe:              float = 0.0    # Return on Equity (%)
    dividend_yield:   float = 0.0    # 배당 수익률 (%)
    debt_ratio:       float = 0.0    # 부채비율 (%)
    eps_growth:       float = 0.0    # YoY EPS 성장률 (%)
    market_cap_b:     float = 0.0    # 시가총액 (십억 달러)
    sector:           str   = ""
    intrinsic_value:  float = 0.0    # 간이 DCF 내재가치 (주당)
    current_price:    float = 0.0    # 현재 주가
    margin_of_safety: float = 0.0    # 안전마진 (%)
    score_detail:     Dict[str, float] = field(default_factory=dict)
    notes:            List[str]        = field(default_factory=list)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        result = float(val or 0)
        return result if result == result else default  # NaN 방지
    except (TypeError, ValueError):
        return default


def _fetch_eps_growth(ticker: yf.Ticker) -> float:
    """
    최근 2개 연도 BasicEPS 비교로 YoY EPS 성장률 계산.
    실패 시 0.0 반환.
    """
    try:
        stmt = ticker.get_income_stmt(freq="yearly")
        if stmt is None or stmt.empty:
            return 0.0
        # 행 인덱스에서 'Basic EPS' 또는 'BasicEPS' 찾기
        eps_row = None
        for idx in stmt.index:
            if "basic" in str(idx).lower() and "eps" in str(idx).lower():
                eps_row = stmt.loc[idx]
                break
        if eps_row is None or len(eps_row) < 2:
            return 0.0
        eps_sorted = eps_row.dropna().sort_index(ascending=False)  # 최신 → 오래된
        if len(eps_sorted) < 2:
            return 0.0
        latest  = float(eps_sorted.iloc[0])
        prev    = float(eps_sorted.iloc[1])
        if prev == 0:
            return 0.0
        return round((latest - prev) / abs(prev) * 100, 2)
    except Exception as exc:
        logging.debug("[fundamental] EPS growth 수집 실패: %s", exc)
    return 0.0


def _fetch_fundamental(symbol: str) -> FundamentalScore:
    """yfinance로 펀더멘털 지표 전체 수집 및 점수 산출."""
    fs = FundamentalScore(symbol=symbol)
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info or {}

        # ── 지표 수집 ────────────────────────────────────────────────
        fs.per           = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
        fs.pbr           = _safe_float(info.get("priceToBook"))
        fs.roe           = _safe_float(info.get("returnOnEquity", 0.0)) * 100  # 0.xx → %
        fs.dividend_yield = _safe_float(info.get("dividendYield", 0.0)) * 100
        fs.sector        = str(info.get("sector", "") or "")
        fs.current_price = _safe_float(
            info.get("currentPrice") or info.get("regularMarketPrice") or
            (ticker.fast_info.last_price if hasattr(ticker, "fast_info") else None)
        )
        fs.market_cap_b = _safe_float(info.get("marketCap", 0.0)) / 1e9

        # 부채비율: totalDebt / totalEquity (%)
        total_debt   = _safe_float(info.get("totalDebt"))
        total_equity = _safe_float(info.get("totalStockholderEquity") or
                                   info.get("bookValue", 0.0) * _safe_float(info.get("sharesOutstanding", 0.0)))
        if total_equity > 0:
            fs.debt_ratio = round(total_debt / total_equity * 100, 2)

        # EPS 성장률 (연간 손익계산서에서 계산)
        fs.eps_growth = _fetch_eps_growth(ticker)

        # ── 간이 DCF 내재가치 ────────────────────────────────────────
        fs.intrinsic_value = _dcf_simple(
            eps          = _safe_float(info.get("trailingEps")),
            growth_rate  = fs.eps_growth / 100 if fs.eps_growth > 0 else 0.05,
            discount_rate= 0.10,   # 할인율 10%
            terminal_pe  = 15.0,   # 터미널 PER 15배
            years        = 5,
        )
        if fs.intrinsic_value > 0 and fs.current_price > 0:
            fs.margin_of_safety = round(
                (fs.intrinsic_value - fs.current_price) / fs.intrinsic_value * 100, 2
            )

    except Exception as exc:
        logging.debug("[fundamental] %s 수집 실패: %s", symbol, exc)

    # ── 점수 산출 ────────────────────────────────────────────────────
    fs.score_detail = _compute_subscores(fs)
    fs.score        = round(sum(fs.score_detail.values()), 1)
    fs.notes        = _generate_notes(fs)
    return fs


def _dcf_simple(eps: float, growth_rate: float, discount_rate: float,
                terminal_pe: float, years: int) -> float:
    """
    간이 DCF: 향후 N년 EPS를 성장률로 추정, 할인율로 현재가치화,
    마지막 해에 터미널 PER 적용.
    """
    if eps <= 0:
        return 0.0
    pv_total = 0.0
    for t in range(1, years + 1):
        future_eps = eps * ((1 + growth_rate) ** t)
        pv = future_eps / ((1 + discount_rate) ** t)
        pv_total += pv
    # 터미널 가치 = 마지막 EPS × 터미널 PER, 현재가치화
    terminal_eps = eps * ((1 + growth_rate) ** years)
    terminal_pv  = (terminal_eps * terminal_pe) / ((1 + discount_rate) ** years)
    return round(pv_total + terminal_pv, 2)


def _compute_subscores(fs: FundamentalScore) -> Dict[str, float]:
    """항목별 점수 산출."""
    d: Dict[str, float] = {}

    # PER (낮을수록 저평가)
    if 0 < fs.per < 10:
        d["per"] = 20
    elif 0 < fs.per < 15:
        d["per"] = 15
    elif 0 < fs.per < 25:
        d["per"] = 8
    else:
        d["per"] = 0

    # PBR
    if 0 < fs.pbr < 1:
        d["pbr"] = 20
    elif 0 < fs.pbr < 2:
        d["pbr"] = 15
    elif 0 < fs.pbr < 3:
        d["pbr"] = 8
    else:
        d["pbr"] = 0

    # ROE
    if fs.roe > 20:
        d["roe"] = 20
    elif fs.roe > 15:
        d["roe"] = 15
    elif fs.roe > 10:
        d["roe"] = 8
    else:
        d["roe"] = 0

    # 배당수익률
    if fs.dividend_yield > 3:
        d["dividend"] = 15
    elif fs.dividend_yield > 2:
        d["dividend"] = 10
    elif fs.dividend_yield > 0:
        d["dividend"] = 5
    else:
        d["dividend"] = 0

    # 부채비율 (낮을수록 좋음)
    if fs.debt_ratio < 30:
        d["debt"] = 15
    elif fs.debt_ratio < 50:
        d["debt"] = 10
    elif fs.debt_ratio < 100:
        d["debt"] = 5
    else:
        d["debt"] = 0

    # EPS 성장률
    if fs.eps_growth > 20:
        d["eps_growth"] = 10
    elif fs.eps_growth > 10:
        d["eps_growth"] = 7
    elif fs.eps_growth > 0:
        d["eps_growth"] = 3
    else:
        d["eps_growth"] = 0

    return d


def _generate_notes(fs: FundamentalScore) -> List[str]:
    """펀더멘털 경고/신호 텍스트 생성."""
    notes = []
    if fs.per > 40 and fs.per > 0:
        notes.append(f"PER {fs.per:.1f} — 고평가 주의")
    if fs.debt_ratio > 150:
        notes.append(f"부채비율 {fs.debt_ratio:.0f}% — 재무 레버리지 과다")
    if fs.eps_growth < 0:
        notes.append(f"EPS 역성장 ({fs.eps_growth:.1f}%) — 실적 하락 추세")
    if fs.margin_of_safety > 20:
        notes.append(f"DCF 안전마진 {fs.margin_of_safety:.1f}% — 저평가 가능성")
    elif fs.margin_of_safety < -20:
        notes.append(f"현주가 DCF 대비 {abs(fs.margin_of_safety):.1f}% 고평가")
    return notes


def analyze_fundamental(symbol: str) -> FundamentalScore:
    """펀더멘털 분석 (캐시 4시간)."""
    return _cached(f"fund_{symbol}", _fetch_fundamental, symbol)


def is_value_stock(fs: FundamentalScore, min_score: float = 55.0) -> bool:
    """최소 점수 이상이면 가치주로 분류."""
    return fs.score >= min_score


def format_fundamental_report(fs: FundamentalScore) -> str:
    """텔레그램 전송용 펀더멘털 분석 텍스트."""
    lines = [
        f"📈 <b>{fs.symbol} 펀더멘털 분석</b>",
        f"종합 점수: <b>{fs.score}/100</b>  |  섹터: {fs.sector}",
        f"PER {fs.per:.1f} | PBR {fs.pbr:.1f} | ROE {fs.roe:.1f}%",
        f"배당 {fs.dividend_yield:.2f}% | 부채 {fs.debt_ratio:.1f}% | EPS성장 {fs.eps_growth:+.1f}%",
    ]
    if fs.intrinsic_value > 0:
        lines.append(
            f"DCF 내재가치: ${fs.intrinsic_value:.2f}  "
            f"(현재 ${fs.current_price:.2f}, 안전마진 {fs.margin_of_safety:+.1f}%)"
        )
    for note in fs.notes:
        lines.append(f"⚠️ {note}")
    return "\n".join(lines)
