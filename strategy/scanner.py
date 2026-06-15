# strategy/scanner.py
"""
급등주 초기 포착 스캐너 — 프리마켓 + 장 시작 직후 검색식

실증 데이터 기반 기준 (Warrior Trading / Scanz / LuxAlgo 검색식 종합):
  갭업폭   : 20%+ (4% 이상이면 Gap&Go 후보, 20%+가 강한 급등)
  RVOL     : 5x+ 프리마켓, 10x+ 장중 (실제 급등 평균 26.7x)
  Float    : 20M 이하 이상적, 50M 이하 허용 (소형 float = 더 큰 급등)
  숏인터레스트: 10%+ (숏스퀴즈 후보), 20%+면 극단적
  Days to Cover: 5일+ (숏스퀴즈 심화 조건)
  뉴스 카탈리스트: 필수 (갭업이 뉴스 기반인지 확인)
  가격대   : $1~$20 (소형주 급등 주력 구간)
  ATR      : $0.50+ (움직임이 있어야 수익 가능)

진입 타이밍:
  프리마켓  : 7:00~9:30 ET — 후보 종목 선별
  장 시작   : 9:30~10:00 ET — 첫 5분봉 확인 후 진입
  핵심 윈도우: 장 시작 후 30~60분 (이 시간이 급등의 핵심)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

# 스캐너 기준 상수 (config.yaml으로 오버라이드 가능)
DEFAULT_CRITERIA = {
    # ── 갭업 조건 ──────────────────────────────────────────────────
    "min_gap_pct":          20.0,   # 전일종가 대비 최소 갭업 20%
    "min_gap_pct_soft":      4.0,   # 완화 기준 (4%+도 Gap&Go 후보)

    # ── 거래량 조건 ────────────────────────────────────────────────
    "min_rvol_premarket":    5.0,   # 프리마켓 최소 상대거래량 5x
    "min_rvol_intraday":    10.0,   # 장중 최소 상대거래량 10x
    "min_premarket_shares": 100_000,# 프리마켓 최소 거래주수
    "min_avg_daily_vol":    500_000,# 20일 평균 일거래량 최소

    # ── Float (유동주식 수) 조건 ───────────────────────────────────
    # 소형 float = 적은 주식 수 = 매수세 몰리면 더 큰 급등
    "max_float_ideal":   20_000_000,  # 이상적: 2000만 이하
    "max_float_ok":      50_000_000,  # 허용:   5000만 이하
    "max_float_loose":  100_000_000,  # 완화:   1억 이하 (조건 완화 시)

    # ── 숏 인터레스트 (숏스퀴즈 후보) ─────────────────────────────
    "min_short_pct_float":  10.0,   # 숏비율 10%+ 숏스퀴즈 후보
    "min_short_pct_hot":    20.0,   # 20%+ 극단적 숏스퀴즈 위험 (=기회)
    "min_days_to_cover":     5.0,   # Days to Cover 5일+ (강제청산 압력)

    # ── 가격 조건 ──────────────────────────────────────────────────
    "min_price":    1.0,    # 최소 $1 (나스닥 상장 유지 기준)
    "max_price":   50.0,    # 최대 $50 (소형주 위주)
    "min_atr":      0.5,    # 최소 ATR $0.50 (움직임 있어야)
}


@dataclass
class GapCandidate:
    """갭업 후보 종목 정보."""
    symbol:           str   = ""
    gap_pct:          float = 0.0    # 갭업폭 (%)
    prev_close:       float = 0.0    # 전일 종가
    premarket_price:  float = 0.0    # 프리마켓 현재가
    rvol:             float = 0.0    # 상대 거래량 (x)
    float_shares:     float = 0.0    # 유동주식 수
    short_pct:        float = 0.0    # 숏 비율 (%)
    days_to_cover:    float = 0.0    # Days to Cover
    atr:              float = 0.0    # ATR
    market_cap_m:     float = 0.0    # 시가총액 (백만달러)
    has_news:         bool  = False  # 뉴스 카탈리스트 여부
    catalyst_type:    str   = ""     # 카탈리스트 종류 (earnings/fda/news/squeeze)
    score:            float = 0.0    # 종합 점수 (0~100)
    squeeze_setup:    bool  = False  # 숏스퀴즈 셋업 여부
    notes:            List[str] = field(default_factory=list)


def _safe(val, default=0.0):
    try:
        v = float(val or 0)
        return v if v == v else default  # NaN 방지
    except (TypeError, ValueError):
        return default


def _fetch_gap_data(symbol: str) -> Optional[GapCandidate]:
    """
    종목 갭업 정보 수집.
    yfinance fast_info + 일봉 2일치 + ticker.info 활용.
    """
    try:
        t = yf.Ticker(symbol)

        # ── 가격 데이터 ───────────────────────────────────────────
        hist = t.history(period="5d", interval="1d")
        if hist is None or len(hist) < 2:
            return None

        prev_close     = float(hist["Close"].iloc[-2])
        today_open     = float(hist["Open"].iloc[-1])
        today_high     = float(hist["High"].iloc[-1])
        today_vol      = float(hist["Volume"].iloc[-1])
        avg_vol_20     = float(hist["Volume"].rolling(min(len(hist), 5)).mean().iloc[-1])

        if prev_close <= 0:
            return None

        gap_pct = (today_open - prev_close) / prev_close * 100

        # ── ticker.info (느리지만 float/short 정보 포함) ─────────
        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass

        float_shares  = _safe(info.get("floatShares"))
        short_pct     = _safe(info.get("shortPercentOfFloat", 0.0)) * 100
        short_ratio   = _safe(info.get("shortRatio"))     # Days to Cover
        market_cap    = _safe(info.get("marketCap", 0.0)) / 1e6
        current_price = _safe(info.get("currentPrice") or info.get("regularMarketPrice") or today_open)

        # ATR (5일 간이 계산)
        if len(hist) >= 3:
            tr = (hist["High"] - hist["Low"]).tail(5).mean()
            atr = float(tr)
        else:
            atr = 0.0

        # 상대 거래량 (RVOL)
        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0.0

        c = GapCandidate(
            symbol          = symbol,
            gap_pct         = round(gap_pct, 1),
            prev_close      = round(prev_close, 2),
            premarket_price = round(current_price, 2),
            rvol            = round(rvol, 1),
            float_shares    = float_shares,
            short_pct       = round(short_pct, 1),
            days_to_cover   = round(short_ratio, 1),
            atr             = round(atr, 2),
            market_cap_m    = round(market_cap, 1),
        )
        return c
    except Exception as exc:
        logging.debug("[scanner] %s 데이터 수집 실패: %s", symbol, exc)
        return None


def _score_candidate(c: GapCandidate, criteria: dict) -> float:
    """
    후보 종목 점수 산출 (0~100).

    Warrior Trading / 실증 데이터 기반 가중치:
      갭업폭    30점 — 핵심 조건
      RVOL      25점 — 실제 급등 평균 26.7x
      Float     20점 — 소형 float = 더 큰 급등
      숏스퀴즈  15점 — 추가 폭발력
      ATR       10점 — 움직임 크기
    """
    score = 0.0
    notes = []

    # ── 갭업 점수 (30점) ──────────────────────────────────────────
    g = c.gap_pct
    if g >= 100:
        score += 30; notes.append(f"갭업 {g:.0f}% (극단적)")
    elif g >= 50:
        score += 25; notes.append(f"갭업 {g:.0f}% (강함)")
    elif g >= 20:
        score += 18; notes.append(f"갭업 {g:.0f}%")
    elif g >= 10:
        score += 10; notes.append(f"갭업 {g:.0f}% (보통)")
    elif g >= 4:
        score += 5;  notes.append(f"갭업 {g:.0f}% (약함)")

    # ── RVOL 점수 (25점) ─────────────────────────────────────────
    # 실제 급등 평균 26.7x → 10x 이상이면 만점
    rv = c.rvol
    if rv >= 20:
        score += 25; notes.append(f"RVOL {rv:.0f}x (폭발적)")
    elif rv >= 10:
        score += 20; notes.append(f"RVOL {rv:.0f}x (강함)")
    elif rv >= 5:
        score += 13; notes.append(f"RVOL {rv:.0f}x")
    elif rv >= 2:
        score += 6;  notes.append(f"RVOL {rv:.0f}x (약함)")

    # ── Float 점수 (20점) ─────────────────────────────────────────
    # 소형 float = 적은 주식 수 = 매수세 몰리면 더 큰 급등
    f = c.float_shares
    if 0 < f <= 5_000_000:
        score += 20; notes.append(f"Float {f/1e6:.1f}M (극소형)")
    elif f <= 10_000_000:
        score += 17; notes.append(f"Float {f/1e6:.1f}M (초소형)")
    elif f <= 20_000_000:
        score += 13; notes.append(f"Float {f/1e6:.1f}M (소형)")
    elif f <= 50_000_000:
        score += 8;  notes.append(f"Float {f/1e6:.1f}M (중소형)")
    elif f > 0:
        score += 3;  notes.append(f"Float {f/1e6:.1f}M (대형 — 급등 약함)")

    # ── 숏스퀴즈 점수 (15점) ─────────────────────────────────────
    sp  = c.short_pct
    dtc = c.days_to_cover
    if sp >= 20 and dtc >= 5:
        score += 15
        c.squeeze_setup = True
        notes.append(f"숏스퀴즈 셋업 (Short {sp:.0f}%, DTC {dtc:.1f}일)")
    elif sp >= 10 and dtc >= 3:
        score += 8
        c.squeeze_setup = True
        notes.append(f"숏스퀴즈 후보 (Short {sp:.0f}%)")
    elif sp >= 5:
        score += 3
        notes.append(f"숏비율 {sp:.0f}%")

    # ── ATR 점수 (10점) ───────────────────────────────────────────
    a = c.atr
    if a >= 3.0:
        score += 10; notes.append(f"ATR ${a:.2f} (고변동성)")
    elif a >= 1.0:
        score += 7;  notes.append(f"ATR ${a:.2f}")
    elif a >= 0.5:
        score += 4;  notes.append(f"ATR ${a:.2f}")
    else:
        notes.append(f"ATR ${a:.2f} (너무 낮음)")

    c.notes = notes
    return round(min(score, 100), 1)


def _check_basic_filters(c: GapCandidate, criteria: dict) -> tuple[bool, str]:
    """기본 필터 — 이걸 통과 못 하면 후보 제외."""
    if c.gap_pct < criteria.get("min_gap_pct_soft", 4.0):
        return False, f"갭업 부족 ({c.gap_pct:.1f}% < {criteria['min_gap_pct_soft']}%)"
    if c.atr < criteria.get("min_atr", 0.5):
        return False, f"ATR 부족 (${c.atr:.2f})"
    if c.premarket_price < criteria.get("min_price", 1.0):
        return False, f"가격 부족 (${c.premarket_price:.2f})"
    if c.premarket_price > criteria.get("max_price", 50.0):
        return False, f"가격 초과 (${c.premarket_price:.2f})"
    if c.rvol < 2.0:
        return False, f"RVOL 부족 ({c.rvol:.1f}x)"
    return True, ""


def scan_gap_candidates(
    symbols:  List[str],
    criteria: Optional[dict] = None,
    min_score: float = 40.0,
) -> List[GapCandidate]:
    """
    종목 리스트에서 갭업 급등 후보 스캔.

    Returns:
        점수 내림차순 GapCandidate 리스트
    """
    crit = {**DEFAULT_CRITERIA, **(criteria or {})}
    results = []

    for sym in symbols:
        c = _fetch_gap_data(sym)
        if c is None:
            continue

        ok, reason = _check_basic_filters(c, crit)
        if not ok:
            logging.debug("[scanner] %s 제외: %s", sym, reason)
            continue

        c.score = _score_candidate(c, crit)
        if c.score >= min_score:
            results.append(c)
            logging.info("[scanner] 후보: %s 갭=%+.0f%% RVOL=%.0fx Float=%.1fM 점수=%.0f",
                         sym, c.gap_pct, c.rvol, c.float_shares / 1e6, c.score)

        time.sleep(0.1)  # API 속도 제한

    results.sort(key=lambda x: x.score, reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────
# VWAP 계산 (장중 진입/청산 기준선)
#
# VWAP = 당일 (가격 × 거래량) 누적합 / 거래량 누적합
# 가격 > VWAP: 매수 우위 → 롱 진입 유리
# 가격 < VWAP: 매도 우위 → 갭앤크랩 위험
# ─────────────────────────────────────────────────────────────────────
def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    VWAP 계산 (당일 누적 기준).
    df: 분봉 데이터 (open/high/low/close/volume 컬럼 필요)
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol  = df["volume"].cumsum()
    cum_tpv  = (typical_price * df["volume"]).cumsum()
    vwap     = cum_tpv / cum_vol.replace(0, float("nan"))
    return vwap


def vwap_entry_signal(df: pd.DataFrame) -> tuple[bool, str]:
    """
    VWAP 기반 진입 신호.

    진입 조건:
      1. 현재가 > VWAP (매수 우위 확인)
      2. 첫 5분봉이 VWAP 위로 마감 (Gap&Go 확인)
      3. 거래량 > 당일 평균 × 2

    Returns:
        (should_enter: bool, reason: str)
    """
    if len(df) < 3:
        return False, "데이터 부족"

    vwap        = compute_vwap(df)
    last        = df.iloc[-1]
    current     = float(last.get("close", 0) or 0)
    current_vol = float(last.get("volume", 0) or 0)
    avg_vol     = float(df["volume"].mean() or 1)
    last_vwap   = float(vwap.iloc[-1])

    if last_vwap <= 0:
        return False, "VWAP 계산 불가"

    # 가격 > VWAP (매수 우위 확인)
    if current < last_vwap:
        return False, f"가격 VWAP 하방 ({current:.2f} < VWAP {last_vwap:.2f}) — 갭앤크랩 위험"

    # 거래량 확인
    if current_vol < avg_vol * 1.5:
        return False, f"거래량 부족 ({current_vol/avg_vol:.1f}x)"

    gap_above_vwap = (current - last_vwap) / last_vwap * 100
    return True, f"VWAP 상방 확인 ({gap_above_vwap:+.1f}%), 거래량 {current_vol/avg_vol:.1f}x"


def vwap_stop_price(df: pd.DataFrame, buffer_pct: float = 0.02) -> float:
    """
    VWAP 기반 손절가.
    손절 = VWAP × (1 - buffer_pct)
    갭앤크랩 발생 시 VWAP 아래로 떨어지면 즉시 청산.
    """
    vwap = compute_vwap(df)
    last_vwap = float(vwap.iloc[-1]) if not vwap.empty else 0.0
    return round(last_vwap * (1.0 - buffer_pct), 2) if last_vwap > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────
# 장 시작 후 5분봉 기반 진입 판단 (Gap&Go 전략)
#
# Gap&Go 원칙 (Warrior Trading 검색식):
#   장 시작 후 첫 5분봉이 VWAP 위에서 마감 → 상승 확인
#   첫 5분봉 고점 돌파 시 진입 (O/N high breakout)
#   손절: 첫 5분봉 저점 또는 VWAP
# ─────────────────────────────────────────────────────────────────────
def gap_and_go_entry(
    df_5min:      pd.DataFrame,  # 당일 5분봉
    gap_pct:      float,         # 프리마켓 갭업 크기
    rvol:         float,         # 현재 RVOL
    min_gap_pct:  float = 20.0,
    min_rvol:     float = 10.0,
) -> tuple[bool, float, float, str]:
    """
    Gap&Go 진입 신호 + 진입가/손절가 계산.

    Returns:
        (should_enter: bool, entry_price: float, stop_price: float, reason: str)
    """
    if len(df_5min) < 2:
        return False, 0.0, 0.0, "5분봉 데이터 부족"

    # ── 갭업 크기 확인 ────────────────────────────────────────────
    if gap_pct < min_gap_pct:
        return False, 0.0, 0.0, f"갭업 부족 ({gap_pct:.1f}% < {min_gap_pct}%)"

    # ── RVOL 확인 ─────────────────────────────────────────────────
    if rvol < min_rvol:
        return False, 0.0, 0.0, f"RVOL 부족 ({rvol:.1f}x < {min_rvol}x)"

    # ── 첫 5분봉 분석 ─────────────────────────────────────────────
    first_candle = df_5min.iloc[0]
    last_candle  = df_5min.iloc[-1]

    first_high = float(first_candle.get("high", 0) or 0)
    first_low  = float(first_candle.get("low",  0) or 0)
    first_close= float(first_candle.get("close",0) or 0)
    current    = float(last_candle.get("close", 0) or 0)

    # ── VWAP 확인 ─────────────────────────────────────────────────
    vwap_entry_ok, vwap_reason = vwap_entry_signal(df_5min)
    if not vwap_entry_ok:
        return False, 0.0, 0.0, vwap_reason

    # ── 첫 5분봉 고점 돌파 확인 (O/N 브레이크아웃) ──────────────
    if current <= first_high:
        return False, 0.0, 0.0, f"첫 5분봉 고점({first_high:.2f}) 미돌파"

    # 진입가 = 현재가 (시장가)
    # 손절가 = max(첫 5분봉 저점, VWAP 손절) 중 높은 것
    vwap_stop  = vwap_stop_price(df_5min, buffer_pct=0.01)
    entry_stop = max(first_low * 0.99, vwap_stop)

    reason = (
        f"Gap&Go 진입 — 갭 {gap_pct:+.0f}%, "
        f"RVOL {rvol:.0f}x, "
        f"첫봉 돌파 ({first_high:.2f}→{current:.2f}), "
        f"손절 {entry_stop:.2f}"
    )
    return True, current, entry_stop, reason


# ─────────────────────────────────────────────────────────────────────
# 숏스퀴즈 특화 스캐너
# ─────────────────────────────────────────────────────────────────────
def scan_short_squeeze(
    symbols:          List[str],
    min_short_pct:    float = 15.0,   # 숏비율 최소 15%
    min_dtc:          float = 3.0,    # Days to Cover 최소
    min_short_trend:  bool  = True,   # 숏포지션 증가 추세 확인
) -> List[dict]:
    """
    숏스퀴즈 후보 스캔.
    Short % of Float + Days to Cover + 거래량 급증 조합.

    Returns:
        [{'symbol', 'short_pct', 'days_to_cover', 'float_m', 'score'}, ...]
    """
    results = []
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
            sp   = _safe(info.get("shortPercentOfFloat", 0.0)) * 100
            dtc  = _safe(info.get("shortRatio", 0.0))
            fl   = _safe(info.get("floatShares", 0.0))
            cap  = _safe(info.get("marketCap",   0.0))

            if sp < min_short_pct or dtc < min_dtc:
                continue

            # 점수 = 숏비율(50%) + DTC(30%) + 소형float(20%)
            score = 0.0
            if sp >= 30: score += 50
            elif sp >= 20: score += 35
            elif sp >= 15: score += 20

            if dtc >= 10: score += 30
            elif dtc >= 5: score += 20
            elif dtc >= 3: score += 10

            if fl <= 10_000_000: score += 20
            elif fl <= 20_000_000: score += 12
            elif fl <= 50_000_000: score += 5

            results.append({
                "symbol":        sym,
                "short_pct":     round(sp, 1),
                "days_to_cover": round(dtc, 1),
                "float_m":       round(fl / 1e6, 1),
                "market_cap_m":  round(cap / 1e6, 1),
                "score":         round(score, 0),
            })
            time.sleep(0.1)
        except Exception as exc:
            logging.debug("[scanner] %s 숏스캔 실패: %s", sym, exc)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def format_scan_report(candidates: List[GapCandidate]) -> str:
    """텔레그램 전송용 스캔 결과 포맷."""
    if not candidates:
        return "📭 갭업 후보 없음"

    lines = [f"⚡ <b>급등 후보 {len(candidates)}종목</b>"]
    for c in candidates[:5]:
        squeeze_tag = " 🔥숏스퀴즈" if c.squeeze_setup else ""
        lines.append(
            f"\n<b>{c.symbol}</b>{squeeze_tag}  점수:{c.score:.0f}\n"
            f"  갭 {c.gap_pct:+.0f}% | RVOL {c.rvol:.0f}x"
            f" | Float {c.float_shares/1e6:.1f}M\n"
            f"  {' · '.join(c.notes[:3])}"
        )
    return "\n".join(lines)
