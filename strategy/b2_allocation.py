# strategy/b2_allocation.py
"""
B2 동적 자산 배분 엔진 — 사계절 레버리지/지수 자동 전환

B2_SWING 모드 내에서 시장 환경(QQQ/SPY vs MA20)에 따라
레버리지 ETF ↔ 지수 ETF ↔ 현금 사이를 자동 전환합니다.

내부 모드:
  BULL_LEVERAGE — 현재가 > MA20: TQQQ/SOXL/FNGU 상위 2개 × 50%씩
  DEFENSE_INDEX — 현재가 ≤ MA20: QQQ/SPY 중 모멘텀 강한 1개 × 100%
  CASH          — QQQ + SPY 모두 MA20 하향 이탈: 전량 청산, 현금 대기

전환 규칙:
  BULL→DEFENSE  : QQQ or SPY가 MA20 이탈
  DEFENSE→BULL  : QQQ or SPY가 MA20 회복
  ANY→CASH      : QQQ AND SPY 모두 MA20 하향
  방어 모드 청산: 주봉 20주 이동평균선 이탈 시

ATR 조정 수익률 (레버리지 순위 결정):
  = 최근 5일 수익률 / ATR  (= 리스크 1 단위당 수익 — 높을수록 우선)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf


# ── 유니버스 ─────────────────────────────────────────────────────────
LEVERAGE_ETFS: List[str] = ["TQQQ", "SOXL", "FNGU", "LABU"]
DEFENSE_ETFS:  List[str] = ["QQQ", "SPY"]
BENCHMARK_ETF: str       = "QQQ"   # 내부 모드 진단 기준

# ── 파라미터 ─────────────────────────────────────────────────────────
MA20_PERIOD          = 20    # 일봉 MA 기간
WEEKLY_MA_PERIOD     = 20    # 주봉 이동평균 기간 (= 약 20주 = 100 거래일)
ATR_TRAIL_MULT       = 1.5   # ATR 트레일링 배수
LEVERAGE_SPLIT       = 0.50  # 레버리지 2종목 각 50% 배분
DATA_CACHE_SEC       = 60    # yfinance 캐시 TTL (초)
REBALANCE_HOUR_ET    = 9     # 리밸런싱 시각 (ET, 장 시작 전)
REBALANCE_MINUTE_ET  = 15


class B2AllocMode(Enum):
    BULL_LEVERAGE  = "BULL_LEVERAGE"   # 레버리지 ETF 2종 공격
    DEFENSE_INDEX  = "DEFENSE_INDEX"   # 지수 ETF 스윙
    CASH           = "CASH"            # 전량 청산, 현금 보호


_MODE_DESC = {
    B2AllocMode.BULL_LEVERAGE: "🚀 공격 모드 (레버리지 ETF)",
    B2AllocMode.DEFENSE_INDEX: "🛡️ 방어 모드 (지수 ETF)",
    B2AllocMode.CASH:          "💰 현금 보호 모드",
}


@dataclass
class AllocationTarget:
    """오늘의 목표 포트폴리오."""
    mode:    B2AllocMode
    symbols: List[str]         = field(default_factory=list)   # 매수 대상
    weights: Dict[str, float]  = field(default_factory=dict)   # symbol → 배분 비율 (합=1.0)
    reasons: Dict[str, str]    = field(default_factory=dict)   # symbol → 선정 근거

    def summary(self) -> str:
        if not self.symbols:
            return f"{_MODE_DESC[self.mode]} — 포지션 없음"
        entries = ", ".join(
            f"{s}({self.weights.get(s, 0)*100:.0f}%)" for s in self.symbols
        )
        return f"{_MODE_DESC[self.mode]} → {entries}"


# ── 데이터 유틸 ──────────────────────────────────────────────────────

def _fetch_daily(symbol: str, period: str = "60d") -> Optional[pd.DataFrame]:
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as exc:
        logging.debug("[B2Alloc] %s 일봉 조회 실패: %s", symbol, exc)
        return None


def _fetch_weekly(symbol: str) -> Optional[pd.DataFrame]:
    try:
        df = yf.download(symbol, period="2y", interval="1wk",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as exc:
        logging.debug("[B2Alloc] %s 주봉 조회 실패: %s", symbol, exc)
        return None


def _ma20(df: pd.DataFrame) -> float:
    if df is None or len(df) < 5:
        return 0.0
    return float(df["close"].rolling(MA20_PERIOD, min_periods=5).mean().iloc[-1] or 0)


def _weekly_ma20(df: pd.DataFrame) -> float:
    if df is None or len(df) < 5:
        return 0.0
    return float(df["close"].rolling(WEEKLY_MA_PERIOD, min_periods=5).mean().iloc[-1] or 0)


def _atr14(df: pd.DataFrame) -> float:
    if df is None or len(df) < 5:
        return 0.0
    hi = df["high"].tail(14)
    lo = df["low"].tail(14)
    cl = df["close"].shift(1).tail(14)
    tr = pd.concat([hi - lo, (hi - cl).abs(), (lo - cl).abs()], axis=1).max(axis=1)
    return float(tr.mean() or 0.0)


def _momentum_5d(df: pd.DataFrame) -> float:
    """최근 5일 수익률 (%)."""
    if df is None or len(df) < 6:
        return 0.0
    c = df["close"]
    return float((c.iloc[-1] - c.iloc[-6]) / c.iloc[-6] * 100 if c.iloc[-6] > 0 else 0.0)


def _atr_adjusted_return(df: pd.DataFrame) -> float:
    """ATR 조정 수익률 = 5일 수익률 / ATR (높을수록 리스크 대비 수익 우수)."""
    mom = _momentum_5d(df)
    atr = _atr14(df)
    return mom / atr if atr > 0 else 0.0


# ── 핵심 엔진 ────────────────────────────────────────────────────────

class B2AllocationEngine:
    """
    B2 스윙 모드 내 동적 자산 배분 엔진.

    RegimeEngine이 B2_SWING을 결정한 이후,
    이 엔진이 BULL_LEVERAGE / DEFENSE_INDEX / CASH를 매일 갱신합니다.

    사용 예:
        engine = B2AllocationEngine(notify=orch._notify)
        target = engine.rebalance()   # 장 시작 전 호출
        # target.symbols, target.weights 기반으로 주문 실행
    """

    def __init__(self, notify: Optional[Callable[[str], None]] = None) -> None:
        self._notify      = notify or (lambda _: None)
        self._mode        = B2AllocMode.CASH
        self._target:     Optional[AllocationTarget] = None
        self._last_rebal: float = 0.0

    @property
    def current_mode(self) -> B2AllocMode:
        return self._mode

    @property
    def current_target(self) -> Optional[AllocationTarget]:
        return self._target

    # ── 시장 상태 진단 ───────────────────────────────────────────────

    def _detect_mode(self) -> Tuple[B2AllocMode, Dict[str, float], Dict[str, float]]:
        """
        QQQ/SPY 기준 내부 레짐 결정.

        Returns:
            (mode, prices, ma20s)
        """
        prices: Dict[str, float] = {}
        ma20s:  Dict[str, float] = {}

        for sym in DEFENSE_ETFS:
            df = _fetch_daily(sym)
            if df is None or df.empty:
                continue
            prices[sym] = float(df["close"].iloc[-1])
            ma20s[sym]  = _ma20(df)

        if not prices:
            return B2AllocMode.CASH, {}, {}

        above = {s: prices[s] > ma20s.get(s, 0) for s in prices}

        # 모두 MA20 하향 → CASH
        if not any(above.values()):
            return B2AllocMode.CASH, prices, ma20s

        # 하나라도 MA20 상회 → 모드 결정
        qqq_above = above.get("QQQ", False)
        spy_above = above.get("SPY", False)

        if qqq_above and spy_above:
            return B2AllocMode.BULL_LEVERAGE, prices, ma20s
        else:
            return B2AllocMode.DEFENSE_INDEX, prices, ma20s

    # ── 레버리지 ETF 순위 ────────────────────────────────────────────

    def _rank_leverage_etfs(self) -> List[Tuple[str, float]]:
        """TQQQ/SOXL/FNGU를 ATR 조정 수익률로 순위 매김."""
        scored = []
        for sym in LEVERAGE_ETFS:
            df = _fetch_daily(sym, period="30d")
            if df is None or len(df) < 6:
                continue
            score = _atr_adjusted_return(df)
            scored.append((sym, score))
            logging.debug("[B2Alloc] %s ATR조정수익률=%.3f", sym, score)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # ── 방어 ETF 모멘텀 선정 ─────────────────────────────────────────

    def _select_defense_etf(self) -> Tuple[str, float]:
        """QQQ vs SPY 중 5일 모멘텀 강한 1개 선정."""
        best, best_mom = "QQQ", -999.0
        for sym in DEFENSE_ETFS:
            df = _fetch_daily(sym, period="30d")
            if df is None:
                continue
            mom = _momentum_5d(df)
            if mom > best_mom:
                best, best_mom = sym, mom
        return best, best_mom

    # ── 방어 모드 주봉 청산 체크 ────────────────────────────────────

    def check_weekly_exit(self, symbol: str) -> Tuple[bool, str]:
        """
        방어 모드 전용: 주봉 20주 이동평균선 이탈 시 청산 신호.

        Returns:
            (should_exit, reason)
        """
        df_w = _fetch_weekly(symbol)
        if df_w is None or df_w.empty:
            return False, ""
        current  = float(df_w["close"].iloc[-1])
        wma20    = _weekly_ma20(df_w)
        if wma20 > 0 and current < wma20:
            return True, f"주봉 MA20 이탈 (${current:.2f} < 주봉MA20 ${wma20:.2f})"
        return False, ""

    # ── ATR 트레일링 스탑 계산 ──────────────────────────────────────

    def atr_trailing_stop(self, symbol: str, current_price: float) -> float:
        """현재가 기준 ATR×1.5 트레일링 스탑 가격."""
        df = _fetch_daily(symbol, period="30d")
        atr = _atr14(df) if df is not None else current_price * 0.02
        stop = current_price - atr * ATR_TRAIL_MULT
        return max(stop, current_price * 0.90)   # 최소 -10% 하드플로어

    # ── 메인 리밸런싱 ────────────────────────────────────────────────

    def rebalance(self) -> AllocationTarget:
        """
        일일 포트폴리오 리밸런싱.

        1. 시장 레짐 진단 (QQQ/SPY vs MA20)
        2. BULL: 레버리지 ETF 상위 2개 × 50%
        3. DEFENSE: 모멘텀 강한 지수 ETF × 100%
        4. CASH: 포지션 없음

        Returns:
            AllocationTarget — 오늘의 목표 포트폴리오
        """
        mode, prices, ma20s = self._detect_mode()
        prev_mode = self._mode

        target = AllocationTarget(mode=mode)

        if mode == B2AllocMode.BULL_LEVERAGE:
            ranked = self._rank_leverage_etfs()
            top2   = ranked[:2]
            for sym, score in top2:
                target.symbols.append(sym)
                target.weights[sym] = LEVERAGE_SPLIT
                target.reasons[sym] = f"ATR조정수익률 {score:.3f} (Top{top2.index((sym,score))+1})"

        elif mode == B2AllocMode.DEFENSE_INDEX:
            sym, mom = self._select_defense_etf()
            target.symbols.append(sym)
            target.weights[sym] = 1.0
            target.reasons[sym] = f"5일 모멘텀 {mom:+.2f}%"

        # CASH: symbols/weights 비움 → 포지션 청산

        self._mode   = mode
        self._target = target
        self._last_rebal = time.monotonic()

        # ── 텔레그램 리포트 ──────────────────────────────────────────
        mode_changed = (mode != prev_mode)
        self._report_rebalance(target, prices, ma20s, mode_changed)

        return target

    def _report_rebalance(
        self,
        target:       AllocationTarget,
        prices:       Dict[str, float],
        ma20s:        Dict[str, float],
        mode_changed: bool,
    ) -> None:
        ma_status = " / ".join(
            f"{s}: ${prices.get(s,0):.1f} vs MA20 ${ma20s.get(s,0):.1f} "
            f"({'상회' if prices.get(s,0) > ma20s.get(s,0) else '하회'})"
            for s in prices
        )

        if target.mode == B2AllocMode.CASH:
            msg = (
                f"💰 B2 현금 보호 모드 전환\n"
                f"QQQ/SPY 모두 MA20 하향 이탈 → 전량 청산 후 현금 대기\n"
                f"지수 상태: {ma_status}"
            )
        elif target.mode == B2AllocMode.DEFENSE_INDEX:
            sym = target.symbols[0] if target.symbols else "-"
            reason = target.reasons.get(sym, "")
            if mode_changed:
                msg = (
                    f"🛡️ 전략 전환: 방어 모드 활성화\n"
                    f"시장 지수 MA20 하회 판단 → 인덱스 ETF 대피 완료\n"
                    f"자산 보존 전략 가동. 인덱스 ETF {sym}으로 대피 완료.\n"
                    f"지수 상태: {ma_status}"
                )
            else:
                msg = (
                    f"📊 B2 방어 모드 리밸런싱\n"
                    f"포트폴리오: {target.summary()}\n"
                    f"선정 근거: {sym} — {reason}"
                )
        else:  # BULL_LEVERAGE
            entries = "\n".join(
                f"  {s} ({target.weights.get(s,0)*100:.0f}%): {target.reasons.get(s,'')}"
                for s in target.symbols
            )
            if mode_changed:
                msg = (
                    f"🚀 전략 전환: 공격 모드 활성화\n"
                    f"시장 지수 20일선 상회 판단 → 레버리지 ETF 진입\n"
                    f"포트폴리오 리밸런싱:\n{entries}\n"
                    f"ATR×{ATR_TRAIL_MULT} 트레일링 스탑 적용"
                )
            else:
                msg = (
                    f"🚀 B2 공격 모드 리밸런싱\n"
                    f"포트폴리오:\n{entries}"
                )

        self._notify(msg)
        logging.info("[B2Alloc] %s", target.summary())
