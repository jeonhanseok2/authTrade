# strategy/exit_strategy.py
"""
가변 ATR 트레일링 스탑 + 개미 털기 방어 통합 청산 전략 엔진

기존 strategy/exits.py 와 strategy/squeeze.py 의 청산 로직을 포괄하며,
B3 급등주에 특화된 4-레이어 최종 청산 판단을 제공합니다.

청산 판단 우선순위:
  1. 본절가 트랩 (고점 +10% 이후 진입가 이탈) — 최우선, 모든 추세 지표 무시
  2. 오더플로우 매도 압력 폭증 (매도/매수 >= 1.5x) — ATR 스탑 무관 즉시 청산
  3. 가변 가속도 ATR 트레일링 스탑 이탈 → 개미 털기 방어 필터 통과 후 청산
     - 저거래량(<평균×0.5) 이탈 시 1분 대기, 복귀 시 포지션 유지 + 텔레그램
     - 고거래량 이탈 or 1분 대기 만료 시 즉시 청산
  4. 정상 보유 (HOLD)

가변 ATR 배수:
  수익 < 50%  → ATR × 3.0  (초기 변동성 여유, 큰 추세 보유)
  수익 >= 50% → ATR × 1.5  (고수익 구간 타이트 보호, 고점 근처 청산)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional

import pandas as pd


# ─────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────
_MULT_NORMAL           = 3.0    # 수익 < 50%
_MULT_HIGH             = 1.5    # 수익 >= 50%
_SHAKEOUT_WAIT_SECONDS = 60.0   # 개미 털기 의심 시 대기 시간
_ORDER_FLOW_LOOKBACK   = 100    # 오더플로우 분석 봉 수
_SELL_PRESSURE_THRESH  = 1.5    # 매도/매수 비율 임계치
_BREAKEVEN_TRIGGER_PCT = 0.10   # 본절가 트랩 발동 최소 수익률


# ─────────────────────────────────────────────────────────────────────
# 반환 타입
# ─────────────────────────────────────────────────────────────────────
class ExitDecision(Enum):
    HOLD          = "hold"
    SELL          = "sell"
    SHAKEOUT_WAIT = "shakeout_wait"


@dataclass
class ExitSignal:
    decision:    ExitDecision
    reason:      str
    is_shakeout: bool = False


# ─────────────────────────────────────────────────────────────────────
# 1. 가변 가속도 ATR 트레일링 스탑
# ─────────────────────────────────────────────────────────────────────
def update_trailing_stop(
    entry_price:  float,
    current_high: float,
    current_atr:  float,
    profit_pct:   float,
) -> float:
    """
    가변 가속도 ATR 트레일링 스탑 계산.

    trailing_stop = current_high - (current_atr × multiplier)

    수익률에 따라 배수 자동 조정:
      - 수익 < 50%:  ATR × 3.0 (여유 있게 추세 확보)
      - 수익 >= 50%: ATR × 1.5 (고수익 구간 타이트 보호)

    ATR=0 이면 진입가 -10% 하드플로어 반환.
    """
    if current_atr <= 0 or current_high <= 0:
        return entry_price * 0.90

    mult = _MULT_HIGH if profit_pct >= 0.50 else _MULT_NORMAL
    stop = current_high - (current_atr * mult)
    return max(stop, entry_price * 0.90)   # 진입가 -10% 하드플로어


# ─────────────────────────────────────────────────────────────────────
# 2. 개미 털기 방어 (Shake-out Defense)
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _ShakeoutState:
    detected_at:   float   # time.monotonic() 기준
    trailing_stop: float


class ShakeoutDefense:
    """
    ATR 스탑 이탈 후 저거래량 여부로 '개미 털기' 패턴을 구분.

    - 저거래량(<평균×0.5) 이탈 → 60초 대기
        · 복귀: HOLD (개미 털기로 판단, 포지션 유지)
        · 만료: SELL (진짜 이탈로 최종 판정)
    - 고거래량 이탈 → 즉시 SELL (세력이 털고 나감)
    """

    def __init__(self) -> None:
        self._pending: Dict[str, _ShakeoutState] = {}

    def assess(
        self,
        symbol:         str,
        current_price:  float,
        trailing_stop:  float,
        current_volume: float,
        avg_volume_20d: float,
    ) -> ExitDecision:
        """
        ATR 스탑 이탈 여부 + 개미 털기 판별.

        Returns:
          HOLD          — 스탑 이탈 없음 (또는 복귀)
          SHAKEOUT_WAIT — 저거래량 이탈, 대기 중
          SELL          — 고거래량 이탈 또는 대기 만료
        """
        # 가격이 스탑 위 → 정상 (대기 중이었으면 해제)
        if current_price >= trailing_stop:
            self._pending.pop(symbol, None)
            return ExitDecision.HOLD

        # 이미 대기 중
        if symbol in self._pending:
            elapsed = time.monotonic() - self._pending[symbol].detected_at
            if elapsed >= _SHAKEOUT_WAIT_SECONDS:
                del self._pending[symbol]
                return ExitDecision.SELL   # 1분 경과 → 진짜 이탈
            return ExitDecision.SHAKEOUT_WAIT

        # 신규 이탈
        if avg_volume_20d > 0 and current_volume < avg_volume_20d * 0.5:
            # 저거래량 → 개미 털기 의심, 대기 시작
            self._pending[symbol] = _ShakeoutState(
                detected_at=time.monotonic(),
                trailing_stop=trailing_stop,
            )
            return ExitDecision.SHAKEOUT_WAIT
        else:
            # 고거래량 이탈 → 즉시 매도
            return ExitDecision.SELL

    def clear(self, symbol: str) -> None:
        self._pending.pop(symbol, None)

    def is_pending(self, symbol: str) -> bool:
        return symbol in self._pending

    def remaining_wait(self, symbol: str) -> float:
        if symbol not in self._pending:
            return 0.0
        elapsed = time.monotonic() - self._pending[symbol].detected_at
        return max(0.0, _SHAKEOUT_WAIT_SECONDS - elapsed)


# ─────────────────────────────────────────────────────────────────────
# 3. 오더플로우 매도 압력 (100봉 근사치)
# ─────────────────────────────────────────────────────────────────────
def analyze_sell_pressure(df: pd.DataFrame) -> float:
    """
    최근 N봉(최대 100) 기준 매도 압력 비율 반환.

    반환값 >= 1.5 → ATR 스탑과 무관하게 즉시 매도 트리거.
    내부적으로 strategy.squeeze.analyze_order_flow() 재사용.
    """
    if df is None or df.empty:
        return 0.0
    from strategy.squeeze import analyze_order_flow
    n    = min(_ORDER_FLOW_LOOKBACK, len(df))
    flow = analyze_order_flow(df, lookback_bars=n)
    buy  = float(flow.get("buy_vol",  1.0) or 1.0)
    sell = float(flow.get("sell_vol", 0.0) or 0.0)
    return sell / max(buy, 1.0)


# ─────────────────────────────────────────────────────────────────────
# 4. 본절가 트랩 (Breakeven Trap)
# ─────────────────────────────────────────────────────────────────────
def check_breakeven_trap(
    entry_price:     float,
    current_price:   float,
    peak_profit_pct: float,
    trigger_pct:     float = _BREAKEVEN_TRIGGER_PCT,
) -> bool:
    """
    고점 수익이 trigger_pct 이상 도달한 후 현재가가 진입가 아래로 떨어지면 True.

    어떤 추세 지표보다 우선순위가 높음 — 이 경우 즉시 전량 청산.
    예) 진입 $100, 고점 $115(+15%), 현재 $99 → True
    """
    if entry_price <= 0 or peak_profit_pct < trigger_pct:
        return False
    return current_price < entry_price


# ─────────────────────────────────────────────────────────────────────
# 5. 통합 청산 판단 엔진
# ─────────────────────────────────────────────────────────────────────
class ExitStrategyEngine:
    """
    B3 급등주 포지션 최종 청산 판단 엔진.

    Orchestrator._exit_cycle 에서 squeeze 포지션에 대해 호출.
    기존 _check_exit_reason 의 hard_stop / stop_loss / distribution 등은
    그대로 유지되며, 이 엔진은 trailing_stop 판단을 대체합니다.

    사용 예:
        engine = ExitStrategyEngine(notify=orchestrator._notify)
        signal = engine.assess(symbol, entry, current, high, atr, df, vol, avg_vol, peak_pct)
        if signal.decision == ExitDecision.SELL:
            _do_exit(...)
        elif signal.decision == ExitDecision.SHAKEOUT_WAIT:
            pass  # 이번 사이클 스킵
    """

    def __init__(self, notify: Optional[Callable[[str], None]] = None) -> None:
        self.shakeout = ShakeoutDefense()
        self._notify  = notify or (lambda _: None)

    def assess(
        self,
        symbol:          str,
        entry_price:     float,
        current_price:   float,
        current_high:    float,   # 포지션 보유 중 최고가 (peak)
        current_atr:     float,
        df:              Optional[pd.DataFrame],
        current_volume:  float = 0.0,
        avg_volume_20d:  float = 0.0,
        peak_profit_pct: float = 0.0,
    ) -> ExitSignal:
        """
        4-레이어 청산 판단 실행.

        Args:
            symbol:          종목 코드
            entry_price:     진입가
            current_price:   현재가
            current_high:    포지션 최고가 (peak_price)
            current_atr:     ATR 값
            df:              최근 봉 데이터프레임 (오더플로우 분석용)
            current_volume:  현재봉 거래량
            avg_volume_20d:  20일 평균 거래량
            peak_profit_pct: 최고가 기준 수익률 (peak-entry)/entry

        Returns:
            ExitSignal
        """
        profit_pct = (
            (current_price - entry_price) / entry_price
            if entry_price > 0 else 0.0
        )

        # ── Layer 1: 본절가 트랩 ─────────────────────────────────────
        if check_breakeven_trap(entry_price, current_price, peak_profit_pct):
            reason = (
                f"본절가 이탈 — 고점 +{peak_profit_pct*100:.0f}% 이후 "
                f"진입가(${entry_price:.2f}) 아래로 하락 (현재 ${current_price:.2f})"
            )
            self._notify(
                f"⛔ [{symbol}] 본절가 트랩 청산\n"
                f"매도 사유: {reason}"
            )
            self.shakeout.clear(symbol)
            logging.warning("[ExitEngine][%s] 본절가 트랩: %s", symbol, reason)
            return ExitSignal(ExitDecision.SELL, f"breakeven_trap:{reason}")

        # ── Layer 2: 오더플로우 매도 압력 폭증 ───────────────────────
        sell_pressure = analyze_sell_pressure(df)
        if sell_pressure >= _SELL_PRESSURE_THRESH:
            reason = (
                f"세력 이탈 — 오더플로우 매도 압력 {sell_pressure:.1f}x 폭증 "
                f"(기준 {_SELL_PRESSURE_THRESH}x)"
            )
            self._notify(
                f"🚨 [{symbol}] 세력 이탈 감지 — 즉시 청산\n"
                f"매도 사유: {reason}"
            )
            self.shakeout.clear(symbol)
            logging.warning("[ExitEngine][%s] 오더플로우: %s", symbol, reason)
            return ExitSignal(ExitDecision.SELL, f"orderflow_pressure:{reason}")

        # ── Layer 3: 가변 ATR 트레일링 스탑 + 개미 털기 방어 ─────────
        trailing_stop = update_trailing_stop(
            entry_price, current_high, current_atr, peak_profit_pct
        )

        decision = self.shakeout.assess(
            symbol, current_price, trailing_stop, current_volume, avg_volume_20d
        )

        if decision == ExitDecision.HOLD:
            return ExitSignal(ExitDecision.HOLD, "")

        if decision == ExitDecision.SHAKEOUT_WAIT:
            remaining = self.shakeout.remaining_wait(symbol)
            vol_ratio = current_volume / max(avg_volume_20d, 1.0)
            reason = (
                f"개미 털기 감지 — 저거래량({vol_ratio:.1f}x) ATR 스탑 이탈, "
                f"{remaining:.0f}초 후 재판정"
            )
            self._notify(
                f"🛡️ [{symbol}] 개미 털기 감지 - 포지션 유지\n"
                f"상세: {reason}"
            )
            logging.info("[ExitEngine][%s] 개미 털기 대기: %s", symbol, reason)
            return ExitSignal(ExitDecision.SHAKEOUT_WAIT, reason, is_shakeout=True)

        # SELL — 고거래량 이탈 or 대기 만료
        mult = _MULT_HIGH if peak_profit_pct >= 0.50 else _MULT_NORMAL
        reason = (
            f"추세 이탈 — ATR×{mult:.1f} 트레일링 스탑 (${trailing_stop:.2f}) 하향 돌파 "
            f"(현재 ${current_price:.2f}, 수익 {profit_pct*100:+.1f}%)"
        )
        self._notify(
            f"📉 [{symbol}] 추세 이탈 청산\n"
            f"매도 사유: {reason}"
        )
        self.shakeout.clear(symbol)
        logging.warning("[ExitEngine][%s] 트레일링 스탑: %s", symbol, reason)
        return ExitSignal(ExitDecision.SELL, f"trailing_stop:{reason}")
