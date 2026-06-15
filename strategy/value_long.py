# strategy/value_long.py
"""
버킷 1: 가치주 장기투자 전략.

진입 기준:
  - 펀더멘털 점수 >= 55점 (PER/PBR/ROE/배당/부채/EPS 종합)
  - DCF 안전마진 >= 10% (현주가 < 내재가치)
  - 뉴스 감성 bearish 아닐 것
  - 레짐: panic 제외 (bear에서도 분할매수 허용)
  - RSI < 70 (과매수 구간 진입 자제)

청산 기준:
  - 손절: 진입가 대비 -8% (가치주는 조금 더 여유)
  - 목표가: 진입가 대비 +25% (또는 DCF 내재가치 도달)
  - 펀더멘털 점수 < 40점으로 하락 시 재평가 후 청산
  - 레짐이 panic으로 바뀌면 포지션 절반 청산 (리스크 축소)
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from analysis.fundamental import FundamentalScore, analyze_fundamental, is_value_stock
from analysis.news import analyze_sentiment, fetch_articles
from strategy.signals import compute_indicators, latest_rsi


# ─────────────────────────────────────────────────────────────────────
# 가치주 진입 판단
# ─────────────────────────────────────────────────────────────────────
def value_long_entry(
    symbol:    str,
    df:        pd.DataFrame,
    regime:    str = "bull",
    min_score: float = 55.0,
    min_safety_margin: float = 10.0,
) -> tuple[bool, str]:
    """
    가치주 진입 여부 판단.

    Returns:
        (should_enter: bool, reason: str)
    """
    # panic 레짐에서는 신규 진입 금지
    if regime == "panic":
        return False, "레짐=panic: 신규 진입 금지"

    # ── 펀더멘털 점수 확인 ────────────────────────────────────────────
    fs = analyze_fundamental(symbol)
    if not is_value_stock(fs, min_score):
        return False, f"펀더멘털 점수 부족 ({fs.score:.1f}/{min_score})"

    # ── DCF 안전마진 확인 ─────────────────────────────────────────────
    if fs.intrinsic_value > 0 and fs.margin_of_safety < min_safety_margin:
        return False, f"DCF 안전마진 부족 ({fs.margin_of_safety:.1f}% < {min_safety_margin}%)"

    # ── RSI 과매수 확인 ───────────────────────────────────────────────
    rsi = latest_rsi(compute_indicators(df))
    if rsi > 70:
        return False, f"RSI 과매수 ({rsi:.1f} > 70)"

    # ── 뉴스 감성 확인 ────────────────────────────────────────────────
    articles  = fetch_articles(symbol, hours=48, limit=10)
    sentiment = analyze_sentiment(articles)
    if sentiment["label"] == "bearish" and sentiment["score"] < -0.3:
        return False, f"뉴스 부정적 (점수: {sentiment['score']:.2f})"

    reason = (
        f"가치주 진입 조건 충족 — 점수:{fs.score:.1f}, "
        f"안전마진:{fs.margin_of_safety:.1f}%, RSI:{rsi:.1f}"
    )
    return True, reason


# ─────────────────────────────────────────────────────────────────────
# 가치주 청산 판단
# ─────────────────────────────────────────────────────────────────────
def value_long_exit(
    symbol:       str,
    entry_price:  float,
    current_price: float,
    peak_price:   float,
    regime:       str = "bull",
    stop_pct:     float = 0.08,
    target_pct:   float = 0.25,
    fs:           Optional[FundamentalScore] = None,
) -> tuple[bool, str]:
    """
    가치주 청산 여부 판단.

    Returns:
        (should_exit: bool, reason: str)
    """
    if entry_price <= 0:
        return False, "진입가 없음"

    pnl_pct = (current_price - entry_price) / entry_price

    # ── 손절 (-8%) ───────────────────────────────────────────────────
    if pnl_pct <= -stop_pct:
        return True, f"손절 ({pnl_pct*100:.1f}% <= -{stop_pct*100:.0f}%)"

    # ── 목표가 도달 (+25%) ───────────────────────────────────────────
    if pnl_pct >= target_pct:
        return True, f"목표가 도달 ({pnl_pct*100:.1f}% >= +{target_pct*100:.0f}%)"

    # ── DCF 내재가치 도달 ─────────────────────────────────────────────
    if fs and fs.intrinsic_value > 0 and current_price >= fs.intrinsic_value:
        return True, f"DCF 내재가치 도달 (현재 ${current_price:.2f} >= 내재 ${fs.intrinsic_value:.2f})"

    # ── panic 레짐으로 전환 시 절반 청산 신호 ────────────────────────
    if regime == "panic" and pnl_pct > 0:
        return True, "레짐=panic 전환: 수익 실현"

    # ── 펀더멘털 악화 ─────────────────────────────────────────────────
    if fs and fs.score < 40:
        return True, f"펀더멘털 점수 급락 ({fs.score:.1f})"

    return False, ""


# ─────────────────────────────────────────────────────────────────────
# 가치주 후보 종목 필터링
# ─────────────────────────────────────────────────────────────────────
def scan_value_candidates(
    symbols:   list[str],
    min_score: float = 55.0,
    max_per:   float = 20.0,
    min_roe:   float = 10.0,
) -> list[tuple[str, FundamentalScore]]:
    """
    종목 리스트에서 가치주 후보 선별.
    Returns: [(symbol, FundamentalScore), ...] — 점수 내림차순
    """
    candidates = []
    for sym in symbols:
        try:
            fs = analyze_fundamental(sym)
            if fs.score < min_score:
                continue
            if max_per > 0 and 0 < fs.per > max_per:
                continue
            if fs.roe < min_roe:
                continue
            candidates.append((sym, fs))
            logging.info("[value_long] 후보: %s 점수=%.1f PER=%.1f ROE=%.1f%%",
                         sym, fs.score, fs.per, fs.roe)
        except Exception as exc:
            logging.debug("[value_long] %s 스캔 실패: %s", sym, exc)

    candidates.sort(key=lambda x: x[1].score, reverse=True)
    return candidates
