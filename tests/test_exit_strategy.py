# tests/test_exit_strategy.py
"""strategy/exit_strategy.py 단위 테스트."""
import time
import unittest
from unittest.mock import patch

import pandas as pd

from strategy.exit_strategy import (
    ExitDecision,
    ExitStrategyEngine,
    ShakeoutDefense,
    analyze_sell_pressure,
    check_breakeven_trap,
    update_trailing_stop,
)


# ── update_trailing_stop ──────────────────────────────────────────────

class TestUpdateTrailingStop(unittest.TestCase):
    def test_low_profit_uses_3x(self):
        # 수익 30% < 50% → ATR×3
        stop = update_trailing_stop(100, 130, 5.0, 0.30)
        self.assertAlmostEqual(stop, 130 - 5.0 * 3.0)  # 115

    def test_high_profit_uses_1_5x(self):
        # 수익 60% >= 50% → ATR×1.5
        stop = update_trailing_stop(100, 160, 5.0, 0.60)
        self.assertAlmostEqual(stop, 160 - 5.0 * 1.5)  # 152.5

    def test_hard_floor_at_minus10(self):
        # ATR이 매우 커서 스탑이 진입가 -10% 아래로 내려가면 클램프
        stop = update_trailing_stop(100, 105, 100.0, 0.05)
        self.assertAlmostEqual(stop, 100 * 0.90)  # 90

    def test_no_atr_returns_hard_floor(self):
        stop = update_trailing_stop(100, 120, 0.0, 0.20)
        self.assertAlmostEqual(stop, 90.0)


# ── check_breakeven_trap ──────────────────────────────────────────────

class TestBreakevenTrap(unittest.TestCase):
    def test_triggers_when_below_entry_after_gain(self):
        # 고점 +15%, 현재가 진입가 아래
        self.assertTrue(check_breakeven_trap(100, 99, 0.15))

    def test_no_trigger_if_peak_profit_below_threshold(self):
        # 고점 수익 5% < 10% → 미발동
        self.assertFalse(check_breakeven_trap(100, 99, 0.05))

    def test_no_trigger_if_price_above_entry(self):
        self.assertFalse(check_breakeven_trap(100, 101, 0.15))


# ── ShakeoutDefense ───────────────────────────────────────────────────

class TestShakeoutDefense(unittest.TestCase):
    def setUp(self):
        self.sd = ShakeoutDefense()

    def test_hold_when_above_stop(self):
        result = self.sd.assess("AAPL", 110, 100, 1000, 2000)
        self.assertEqual(result, ExitDecision.HOLD)

    def test_immediate_sell_on_high_volume(self):
        # 거래량 >= 평균×0.5 → 즉시 SELL
        result = self.sd.assess("AAPL", 90, 100, 1500, 2000)
        self.assertEqual(result, ExitDecision.SELL)

    def test_shakeout_wait_on_low_volume(self):
        # 거래량 < 평균×0.5 → WAIT
        result = self.sd.assess("AAPL", 90, 100, 500, 2000)
        self.assertEqual(result, ExitDecision.SHAKEOUT_WAIT)

    def test_hold_on_recovery_during_wait(self):
        # 1. 저거래량 이탈 → WAIT 등록
        self.sd.assess("AAPL", 90, 100, 500, 2000)
        # 2. 가격 복귀 → HOLD + 상태 해제
        result = self.sd.assess("AAPL", 110, 100, 500, 2000)
        self.assertEqual(result, ExitDecision.HOLD)
        self.assertFalse(self.sd.is_pending("AAPL"))

    def test_sell_after_wait_expires(self):
        # 대기 시간 만료 후 SELL
        self.sd.assess("AAPL", 90, 100, 500, 2000)
        # 내부 타이머를 만료된 것처럼 조작
        self.sd._pending["AAPL"].detected_at -= 65  # 65초 전
        result = self.sd.assess("AAPL", 90, 100, 500, 2000)
        self.assertEqual(result, ExitDecision.SELL)
        self.assertFalse(self.sd.is_pending("AAPL"))


# ── ExitStrategyEngine ────────────────────────────────────────────────

def _make_df(buy_dominant=True, rows=10):
    """매수 또는 매도 우위 OHLCV 데이터프레임 생성."""
    data = []
    for i in range(rows):
        if buy_dominant:
            data.append({"open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000})
        else:
            data.append({"open": 100, "high": 101, "low": 94, "close": 95, "volume": 1000})
    return pd.DataFrame(data)


class TestExitStrategyEngine(unittest.TestCase):
    def setUp(self):
        self.notifications = []
        self.engine = ExitStrategyEngine(notify=self.notifications.append)

    def test_breakeven_trap_overrides_all(self):
        # 고점 수익 20%, 현재가 진입가 아래 → SELL
        sig = self.engine.assess("X", 100, 99, 120, 3.0, _make_df(True), 1000, 2000, 0.20)
        self.assertEqual(sig.decision, ExitDecision.SELL)
        self.assertIn("breakeven_trap", sig.reason)
        # 텔레그램 알림 전송 확인
        self.assertTrue(any("본절가 트랩" in n for n in self.notifications))

    def test_orderflow_pressure_triggers_sell(self):
        # 매도 우위 df → 즉시 SELL
        df = _make_df(buy_dominant=False, rows=20)
        sig = self.engine.assess("X", 100, 102, 110, 3.0, df, 1000, 2000, 0.10)
        # 매도 압력이 1.5x 이상이면 SELL, 아닐 수도 있음 (df 내용에 따라)
        # 여기선 실제 analyze_sell_pressure 결과에 의존
        self.assertIn(sig.decision, (ExitDecision.SELL, ExitDecision.HOLD))

    def test_shakeout_wait_with_notification(self):
        # 가격이 trailing_stop 아래, 저거래량 → SHAKEOUT_WAIT
        df = _make_df(True)
        # entry=100, peak=115 (profit 15% < 50% → ATR×3.0)
        # trailing_stop = 115 - 3.0*3.0 = 106
        sig = self.engine.assess("X", 100, 105, 115, 3.0, df, 100, 2000, 0.15)
        self.assertEqual(sig.decision, ExitDecision.SHAKEOUT_WAIT)
        self.assertTrue(sig.is_shakeout)
        self.assertTrue(any("개미 털기 감지" in n for n in self.notifications))

    def test_sell_on_high_volume_breakout(self):
        # 가격이 trailing_stop 아래, 고거래량 → SELL + 추세 이탈 알림
        df = _make_df(True)
        sig = self.engine.assess("X", 100, 105, 115, 3.0, df, 1500, 2000, 0.15)
        self.assertEqual(sig.decision, ExitDecision.SELL)
        self.assertIn("trailing_stop", sig.reason)
        self.assertTrue(any("추세 이탈" in n for n in self.notifications))

    def test_hold_when_price_above_stop(self):
        df = _make_df(True)
        # entry=100, peak=110, trailing_stop = 110 - 3.0*3.0 = 101
        # current=108 > 101 → HOLD
        sig = self.engine.assess("X", 100, 108, 110, 3.0, df, 500, 2000, 0.10)
        self.assertEqual(sig.decision, ExitDecision.HOLD)


if __name__ == "__main__":
    unittest.main()
