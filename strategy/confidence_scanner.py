# strategy/confidence_scanner.py
"""
신뢰도 기반 실시간 진입 스코어러 (100점 만점)

채점 기준:
  RVOL  30점 — 거래량 폭발 여부 (5x 미만 → 0점)
  Alpha 40점 — 나스닥(QQQ) 대비 상대 강도 (5.0% 미만 → 0점)
  VWAP  30점 — VWAP 상단 돌파 여부

판단 기준:
  ≥ 90점: 활성 그룹 전액 투입
  70~89점: 절반 투입
  < 70점: 진입 금지 (블랙리스트 등록)

장 시작 전 Finviz 필터 → 장 중 Alpaca 실시간 점수 갱신.
QQQ 기준값은 1분 캐시로 API 호출 최소화.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional, Set

import pandas as pd

# ── 점수 임계치 ───────────────────────────────────────────────────────
SCORE_FULL_CAPITAL  = 90   # 전액 투입
SCORE_HALF_CAPITAL  = 70   # 절반 투입
SCORE_BLACKLIST     = 70   # 미달 시 블랙리스트

# ── RVOL 구간 (30점 만점) ─────────────────────────────────────────────
_RVOL_TIERS = [
    (20.0, 30),   # 20x+: 30점
    (10.0, 25),   # 10~20x: 25점
    (5.0,  20),   # 5~10x: 20점
    (3.0,  10),   # 3~5x: 10점 (미달이지만 부분 점수)
    (0.0,   0),
]

# ── Alpha 구간 (40점 만점) ────────────────────────────────────────────
_ALPHA_TIERS = [
    (10.0, 40),   # 10%+: 40점
    (7.0,  30),   # 7~10%: 30점
    (5.0,  20),   # 5~7%: 20점
    (2.0,  10),   # 2~5%: 10점
    (0.0,   0),
]

# ── VWAP 구간 (30점 만점) ─────────────────────────────────────────────
_VWAP_ABOVE_PCT_FULL  = 0.005   # VWAP +0.5% 이상: 30점
_VWAP_ABOVE_PCT_HALF  = 0.000   # VWAP 위: 15점
# VWAP 아래: 0점

# ── QQQ 캐시 TTL ─────────────────────────────────────────────────────
_QQQ_CACHE_SECONDS = 60


@dataclass
class ConfidenceScore:
    """종목 신뢰도 점수 + 상세 내역."""
    symbol:      str
    total:       int   = 0
    rvol_score:  int   = 0
    alpha_score: int   = 0
    vwap_score:  int   = 0
    rvol:        float = 0.0
    alpha:       float = 0.0     # symbol_return - qqq_return (%)
    vwap:        float = 0.0
    price:       float = 0.0
    notes:       List[str] = field(default_factory=list)

    @property
    def capital_ratio(self) -> float:
        """투입 자금 비율 (0.0 / 0.5 / 1.0)."""
        if self.total >= SCORE_FULL_CAPITAL:
            return 1.0
        if self.total >= SCORE_HALF_CAPITAL:
            return 0.5
        return 0.0

    @property
    def is_tradeable(self) -> bool:
        return self.total >= SCORE_HALF_CAPITAL

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.total}점 "
            f"[RVOL={self.rvol_score}({self.rvol:.1f}x) "
            f"Alpha={self.alpha_score}({self.alpha:+.1f}%) "
            f"VWAP={self.vwap_score}] → "
            f"{'전액' if self.capital_ratio >= 1.0 else '절반' if self.capital_ratio > 0 else '진입금지'}"
        )


def _tiered(value: float, tiers: list) -> int:
    for threshold, pts in tiers:
        if value >= threshold:
            return pts
    return 0


def _accumulation_bonus(df: pd.DataFrame) -> tuple[int, str]:
    """
    세력 매집 패턴 보너스 점수 (최대 +15점).

    세력이 조용히 사모으는 중: 거래량 폭발 but 가격은 아직 횡보.
    이 신호가 있으면 곧 급등 가능성 높음 → 진입 신뢰도 상향.

    3개 조건 각 5점:
      1. 거래량 선행 (RVOL ≥3x but 가격 변화 <3%) — 세력 매집 중
      2. 연속 양봉 (최근 5봉 중 4봉+ 상승봉) — 지속 매수세
      3. 풀백 거래량 감소 (하락봉 거래량 < 상승봉 × 0.6) — 약한 매도
    """
    if df is None or len(df) < 5:
        return 0, ""

    try:
        recent   = df.tail(10)
        first_c  = float(recent.iloc[0].get("close", 0) or 0)
        last_c   = float(recent.iloc[-1].get("close", 0) or 0)
        avg_vol  = float(recent["volume"].mean() or 1)
        last_vol = float(recent.iloc[-1].get("volume", 0) or 0)

        score = 0
        notes = []

        # 조건 1: 거래량 선행 — RVOL 높지만 가격 변화 작음
        price_chg_pct = abs(last_c - first_c) / max(first_c, 0.01) * 100
        rvol = last_vol / max(avg_vol, 1)
        if rvol >= 3.0 and price_chg_pct < 3.0:
            score += 5
            notes.append(f"매집신호(RVOL {rvol:.1f}x/가격변화{price_chg_pct:.1f}%)")

        # 조건 2: 연속 양봉 (지속 매수세)
        up_bars = sum(
            1 for _, r in recent.tail(5).iterrows()
            if float(r.get("close", 0) or 0) > float(r.get("open", 0) or 0)
        )
        if up_bars >= 4:
            score += 5
            notes.append(f"연속양봉{up_bars}/5")

        # 조건 3: 풀백 거래량 감소 (약한 매도 = 강한 손들이 홀딩)
        up_vols   = [float(r.get("volume", 0) or 0) for _, r in recent.iterrows()
                     if float(r.get("close", 0) or 0) > float(r.get("open", 0) or 0)]
        down_vols = [float(r.get("volume", 0) or 0) for _, r in recent.iterrows()
                     if float(r.get("close", 0) or 0) <= float(r.get("open", 0) or 0)]
        if up_vols and down_vols:
            avg_up   = sum(up_vols)   / len(up_vols)
            avg_down = sum(down_vols) / len(down_vols)
            if avg_down < avg_up * 0.6:
                score += 5
                notes.append(f"풀백거래량감소({avg_down/avg_up:.1f}x)")

        if score > 0:
            return score, f"매집패턴+{score}pt " + " ".join(notes)
    except Exception:
        pass

    return 0, ""


class _QQQCache:
    """QQQ 당일 수익률 캐시 (1분 TTL)."""

    def __init__(self) -> None:
        self._return:   float = 0.0
        self._ts:       float = 0.0
        self._date:     _date = _date.min

    def get(self) -> float:
        today = _date.today()
        if today != self._date or time.monotonic() - self._ts > _QQQ_CACHE_SECONDS:
            self._refresh(today)
        return self._return

    def _refresh(self, today: _date) -> None:
        try:
            from data.alpaca_bars import fetch_bars
            df = fetch_bars("QQQ", "1Min", 390)
            if df is not None and len(df) >= 2:
                open_px  = float(df["close"].iloc[0])
                last_px  = float(df["close"].iloc[-1])
                if open_px > 0:
                    self._return = (last_px - open_px) / open_px * 100
            self._ts   = time.monotonic()
            self._date = today
        except Exception as exc:
            logging.debug("[ConfidenceScanner] QQQ 조회 실패: %s", exc)


_qqq_cache = _QQQCache()


class ConfidenceBlacklist:
    """당일 점수 미달 종목 블랙리스트 (장 시작 시 자동 초기화)."""

    def __init__(self) -> None:
        self._blacklisted: Set[str] = set()
        self._date: _date = _date.min

    def _reset_if_new_day(self) -> None:
        today = _date.today()
        if today != self._date:
            self._blacklisted.clear()
            self._date = today

    def add(self, symbol: str) -> None:
        self._reset_if_new_day()
        self._blacklisted.add(symbol.upper())
        logging.info("[ConfidenceScanner] 블랙리스트 등록: %s", symbol)

    def is_blacklisted(self, symbol: str) -> bool:
        self._reset_if_new_day()
        return symbol.upper() in self._blacklisted

    def clear(self, symbol: str) -> None:
        self._blacklisted.discard(symbol.upper())


class ConfidenceScanner:
    """
    장중 실시간 신뢰도 스코어러.

    on_bar() 또는 진입 직전에 score() 호출 → ConfidenceScore 반환.
    70점 미만은 자동 블랙리스트 등록.
    """

    def __init__(self) -> None:
        self.blacklist = ConfidenceBlacklist()

    def score(
        self,
        symbol:     str,
        df:         pd.DataFrame,   # 당일 분봉 (open/high/low/close/volume)
        avg_vol_20d: float = 0.0,   # 20일 평균 거래량 (0이면 df 내부에서 계산)
    ) -> ConfidenceScore:
        """
        RVOL / Alpha / VWAP 3가지 기준으로 0~100점 산출.

        Args:
            symbol:      종목 코드
            df:          당일 분봉 데이터프레임
            avg_vol_20d: 20일 평균 거래량 (0이면 df 전체 거래량 평균으로 근사)

        Returns:
            ConfidenceScore
        """
        result = ConfidenceScore(symbol=symbol)
        if df is None or len(df) < 2:
            return result

        last     = df.iloc[-1]
        price    = float(last.get("close", 0) or 0)
        cur_vol  = float(last.get("volume", 0) or 0)
        result.price = price

        # ── RVOL 점수 (30점) ─────────────────────────────────────────
        if avg_vol_20d <= 0:
            avg_vol_20d = float(df["volume"].mean() or 1)
        rvol = cur_vol / avg_vol_20d if avg_vol_20d > 0 else 0.0
        result.rvol       = round(rvol, 1)
        result.rvol_score = _tiered(rvol, _RVOL_TIERS)
        if rvol >= 5:
            result.notes.append(f"RVOL {rvol:.1f}x")
        else:
            result.notes.append(f"RVOL {rvol:.1f}x (약함)")

        # ── Alpha 점수 (40점) — 나스닥(QQQ) 대비 상대 강도 ─────────
        open_px = float(df.iloc[0].get("open", price) or price)
        sym_ret = (price - open_px) / open_px * 100 if open_px > 0 else 0.0
        qqq_ret = _qqq_cache.get()
        alpha   = sym_ret - qqq_ret
        result.alpha       = round(alpha, 2)
        result.alpha_score = _tiered(alpha, _ALPHA_TIERS)
        result.notes.append(f"Alpha vs QQQ {alpha:+.1f}% (종목{sym_ret:+.1f}% QQQ{qqq_ret:+.1f}%)")

        # ── VWAP 점수 (30점) ─────────────────────────────────────────
        from strategy.scanner import compute_vwap
        vwap_series = compute_vwap(df)
        vwap = float(vwap_series.iloc[-1]) if not vwap_series.empty else 0.0
        result.vwap = round(vwap, 2)

        if vwap > 0:
            above_pct = (price - vwap) / vwap
            if above_pct >= _VWAP_ABOVE_PCT_FULL:
                result.vwap_score = 30
                result.notes.append(f"VWAP 상단 돌파 +{above_pct*100:.1f}%")
            elif above_pct >= _VWAP_ABOVE_PCT_HALF:
                result.vwap_score = 15
                result.notes.append(f"VWAP 위 (근접 +{above_pct*100:.1f}%)")
            else:
                result.vwap_score = 0
                result.notes.append(f"VWAP 하방 {above_pct*100:.1f}%")

        # ── 세력 매집 패턴 보너스 (최대 +15점) ──────────────────────────
        accum_bonus, accum_note = _accumulation_bonus(df)
        result.total = result.rvol_score + result.alpha_score + result.vwap_score + accum_bonus
        if accum_note:
            result.notes.append(accum_note)

        # ── 블랙리스트 자동 등록 ─────────────────────────────────────
        if result.total < SCORE_BLACKLIST:
            self.blacklist.add(symbol)

        logging.info("[ConfidenceScanner] %s", result.summary())
        return result

    def is_blacklisted(self, symbol: str) -> bool:
        return self.blacklist.is_blacklisted(symbol)
