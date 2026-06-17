#!/usr/bin/env python3
"""
analyzer.py — 페이퍼 트레이딩 전략 파라미터 최적화 분석기

페이퍼 트레이딩 DB를 읽어 전략별 성과 지표를 계산하고,
현재 설정값과 최적값을 비교해 구체적인 파라미터 튜닝 제안을 생성합니다.
제안 사항은 recommendations.log에 기록되고, 텔레그램으로도 알림을 보냅니다.

사용법:
    python analyzer.py                          # 전체 분석 + 추천 로그
    python analyzer.py --days 14               # 최근 14일
    python analyzer.py --bucket b4             # B4 집중 분석
    python analyzer.py --notify                # 텔레그램 알림 포함
    python analyzer.py --days 30 --notify      # 조합 가능
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── pandas 의존성 체크 ────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("ERROR: pandas/numpy 미설치 → pip install pandas numpy")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML 미설치 → pip install pyyaml")
    sys.exit(1)


# ── 경로 상수 ─────────────────────────────────────────────────────────
_ROOT       = Path(__file__).parent
DB_MAIN     = str(_ROOT / "storage" / "trade.db")
DB_B4       = str(_ROOT / "storage" / "db" / "trading_data.db")
CFG_PATH    = str(_ROOT / "config.yaml")
LOG_PATH    = str(_ROOT / "recommendations.log")

# 최적 기준 (명세서)
MIN_TRADES_FOR_TUNING = 10
OPTIMAL_WIN_RATE      = 50.0   # %

# B4 현재 파라미터 기본값 (strategy_engine.py 에서 import 시도, 실패 시 사용)
_B4_DEFAULTS = {
    "rvol_min":     2.50,    # 250%
    "init_sl":      0.20,    # -20%
    "trail_dist":   0.15,    # -15%
    "partial_at":   0.50,    # +50%
    "step1_at":     1.00,
    "step1_floor":  0.70,
    "step2_at":     2.00,
    "step2_floor":  1.50,
}


# ═════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═════════════════════════════════════════════════════════════════════

@dataclass
class BucketMetrics:
    """전략/버킷 단위 성과 지표."""
    bucket:         str
    trades:         int
    wins:           int
    losses:         int
    win_rate:       float      # %
    avg_win_pct:    float      # %
    avg_loss_pct:   float      # %
    profit_factor:  float
    expectancy:     float      # avg PnL per trade ($)
    total_pnl:      float      # $
    avg_hold_min:   float
    has_enough_data: bool = True

    @property
    def is_optimal(self) -> bool:
        return self.win_rate >= OPTIMAL_WIN_RATE and self.trades >= MIN_TRADES_FOR_TUNING


@dataclass
class Recommendation:
    """파라미터 튜닝 제안."""
    bucket:       str
    parameter:    str
    current_val:  str
    suggested_val: str
    reason:       str
    expected:     str          # 기대 효과
    priority:     str          # "high" | "medium" | "low"
    is_actionable: bool = True  # N < 10 이면 False


@dataclass
class AnalysisResult:
    """전체 분석 결과."""
    generated_at:    str
    days_analyzed:   int
    metrics:         Dict[str, BucketMetrics] = field(default_factory=dict)
    b4_matrix:       Optional[pd.DataFrame]  = None
    recommendations: List[Recommendation]    = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════
# 1. 데이터 로딩
# ═════════════════════════════════════════════════════════════════════

def _connect(path: str) -> Optional[sqlite3.Connection]:
    """SQLite 연결. 파일 없으면 None."""
    p = Path(path)
    if not p.exists():
        return None
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def load_closed_trades(db_path: str = DB_MAIN, days: int = 30) -> pd.DataFrame:
    """
    storage/trade.db 의 closed_trades 테이블 조회.
    Returns: DataFrame with columns [symbol, strategy, pnl, pnl_pct, hold_minutes, exit_reason, date]
    """
    conn = _connect(db_path)
    if conn is None:
        return pd.DataFrame()
    try:
        sql = """
            SELECT symbol, strategy, sector,
                   entry_price, exit_price, qty,
                   hold_minutes, pnl, pnl_pct, exit_reason, date
            FROM closed_trades
            WHERE date >= date('now', ?)
            ORDER BY date DESC
        """
        df = pd.read_sql(sql, conn, params=(f"-{days} days",))
        return df
    except Exception as exc:
        logging.debug("[analyzer] closed_trades 조회 실패: %s", exc)
        return pd.DataFrame()
    finally:
        conn.close()


def load_b4_trades(db_path: str = DB_B4, days: int = 30) -> pd.DataFrame:
    """
    storage/db/trading_data.db 의 b4_trades 테이블 조회.
    Returns: DataFrame with columns [symbol, buy_price, sell_price, qty, result_pct, exit_reason, trade_date]
    """
    conn = _connect(db_path)
    if conn is None:
        return pd.DataFrame()
    try:
        # b4_trades 테이블 존재 여부 확인
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='b4_trades'"
        ).fetchone()
        if not tbl:
            return pd.DataFrame()

        sql = """
            SELECT symbol, buy_price, sell_price, qty, result_pct, exit_reason, trade_date
            FROM b4_trades
            WHERE trade_date >= date('now', ?)
            ORDER BY trade_date DESC
        """
        df = pd.read_sql(sql, conn, params=(f"-{days} days",))
        return df
    except Exception as exc:
        logging.debug("[analyzer] b4_trades 조회 실패: %s", exc)
        return pd.DataFrame()
    finally:
        conn.close()


def load_b4_config() -> dict:
    """strategy_engine.py 에서 B4 상수 import. 실패 시 기본값 반환."""
    try:
        from strategy.strategy_engine import (
            _B4O_RVOL_MIN, _B4O_TRAIL_DIST, _B4O_INIT_SL,
            _B4O_PARTIAL_AT, _B4O_STEP1_AT, _B4O_STEP1_FLOOR,
            _B4O_STEP2_AT, _B4O_STEP2_FLOOR,
        )
        return {
            "rvol_min":     _B4O_RVOL_MIN,
            "init_sl":      _B4O_INIT_SL,
            "trail_dist":   _B4O_TRAIL_DIST,
            "partial_at":   _B4O_PARTIAL_AT,
            "step1_at":     _B4O_STEP1_AT,
            "step1_floor":  _B4O_STEP1_FLOOR,
            "step2_at":     _B4O_STEP2_AT,
            "step2_floor":  _B4O_STEP2_FLOOR,
        }
    except ImportError:
        return _B4_DEFAULTS.copy()


def load_config(cfg_path: str = CFG_PATH) -> dict:
    """config.yaml 로드."""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logging.warning("[analyzer] config.yaml 없음: %s", cfg_path)
        return {}


# ═════════════════════════════════════════════════════════════════════
# 2. 지표 계산
# ═════════════════════════════════════════════════════════════════════

def _profit_factor(wins: pd.Series, losses: pd.Series) -> float:
    total_win  = wins[wins > 0].sum()
    total_loss = abs(losses[losses < 0].sum())
    return float(total_win / total_loss) if total_loss > 0 else float("inf")


def compute_metrics(df: pd.DataFrame, bucket_col: str = "strategy") -> Dict[str, BucketMetrics]:
    """
    DataFrame 에서 버킷별 성과 지표 계산.
    df 컬럼: pnl ($), pnl_pct (%), hold_minutes, 그리고 bucket_col.
    """
    if df.empty or "pnl" not in df.columns:
        return {}

    results: Dict[str, BucketMetrics] = {}
    for bucket, grp in df.groupby(bucket_col):
        wins   = grp[grp["pnl"] > 0]
        losses = grp[grp["pnl"] <= 0]
        n      = len(grp)
        w      = len(wins)
        pf     = _profit_factor(grp["pnl"], grp["pnl"])

        results[str(bucket)] = BucketMetrics(
            bucket          = str(bucket),
            trades          = n,
            wins            = w,
            losses          = n - w,
            win_rate        = round(w / n * 100, 1) if n > 0 else 0.0,
            avg_win_pct     = round(float(wins["pnl_pct"].mean()), 2)   if not wins.empty   else 0.0,
            avg_loss_pct    = round(float(losses["pnl_pct"].mean()), 2) if not losses.empty else 0.0,
            profit_factor   = round(pf, 2),
            expectancy      = round(float(grp["pnl"].mean()), 2),
            total_pnl       = round(float(grp["pnl"].sum()), 2),
            avg_hold_min    = round(float(grp["hold_minutes"].mean()), 0) if "hold_minutes" in grp.columns else 0.0,
            has_enough_data = (n >= MIN_TRADES_FOR_TUNING),
        )
    return results


def compute_b4_metrics(b4_df: pd.DataFrame) -> Optional[BucketMetrics]:
    """b4_trades DataFrame → B4 전용 지표 계산."""
    if b4_df.empty:
        return None

    pnl_usd   = (b4_df["sell_price"] - b4_df["buy_price"]) * b4_df["qty"] * 100
    pnl_pct   = b4_df["result_pct"] * 100   # decimal → %
    n         = len(b4_df)
    wins      = pnl_usd[pnl_usd > 0]
    losses    = pnl_usd[pnl_usd <= 0]
    pf        = _profit_factor(pnl_usd, pnl_usd)

    return BucketMetrics(
        bucket          = "B4",
        trades          = n,
        wins            = len(wins),
        losses          = len(losses),
        win_rate        = round(len(wins) / n * 100, 1) if n > 0 else 0.0,
        avg_win_pct     = round(float(pnl_pct[pnl_usd > 0].mean()),  2) if not wins.empty   else 0.0,
        avg_loss_pct    = round(float(pnl_pct[pnl_usd <= 0].mean()), 2) if not losses.empty else 0.0,
        profit_factor   = round(pf, 2),
        expectancy      = round(float(pnl_usd.mean()), 2),
        total_pnl       = round(float(pnl_usd.sum()), 2),
        avg_hold_min    = 0.0,
        has_enough_data = (n >= MIN_TRADES_FOR_TUNING),
    )


# ═════════════════════════════════════════════════════════════════════
# 3. B4 파라미터 매트릭스
# ═════════════════════════════════════════════════════════════════════

# B4 매트릭스 파라미터 후보
_RVOL_CANDIDATES   = [2.0, 2.5, 3.0]    # 200%, 250%, 300%
_TRAIL_CANDIDATES  = [0.10, 0.15, 0.20]  # -10%, -15%, -20%

_CURRENT_TRAIL = _B4_DEFAULTS["trail_dist"]   # 기본값 사용 (import 시 갱신)


def _simulate_trail(row: pd.Series, new_trail: float, current_trail: float) -> float:
    """
    트레일링 스탑 변경 시 result_pct 시뮬레이션.

    '트레일링청산' 사유인 경우:
        실제 결과 r, 현재 트레일 t → 내재 피크 = (1+r)/(1-t)
        새 트레일링 적용 → 새 결과 = 내재피크 × (1-new_trail) - 1
    기타 사유 (손절/TP/타임스탑): 결과 그대로.
    """
    r       = float(row["result_pct"])
    reason  = str(row.get("exit_reason", ""))

    if "트레일링" in reason or "trailing" in reason.lower():
        divisor = 1.0 - current_trail
        if divisor <= 0:
            return r
        implied_peak = (1.0 + r) / divisor
        return implied_peak * (1.0 - new_trail) - 1.0

    return r  # 손절/부분익절/타임스탑은 트레일링과 무관


def _apply_rvol_filter(b4_df: pd.DataFrame, rvol_thresh: float, current_rvol: float) -> pd.DataFrame:
    """
    거래량 필터 변경 시뮬레이션 (RVOL 미저장 → 근사 처리).

    rvol 데이터가 없으므로 result_pct 분포를 이용한 근사:
      - 현재(2.5x) → 기준선
      - 완화(2.0x)  → 하위 20% 성과 거래가 추가된다고 가정 (평균에 가까운 거래 +20%)
      - 강화(3.0x)  → 하위 20% 성과 거래가 제외된다고 가정
    NOTE: RVOL 미저장으로 인한 근사치이며, 실제 영향과 다를 수 있습니다.
    """
    if b4_df.empty:
        return b4_df

    n = len(b4_df)
    if rvol_thresh < current_rvol:
        # 완화: 평균 결과에 가까운 가상 거래 +20% 추가
        mean_r = float(b4_df["result_pct"].mean())
        extra_n = max(1, int(n * 0.20))
        extra = pd.DataFrame({
            "result_pct":  [mean_r * 0.8] * extra_n,
            "exit_reason": ["가상(RVOL완화)"] * extra_n,
            "buy_price":   [b4_df["buy_price"].mean()] * extra_n,
            "sell_price":  [b4_df["buy_price"].mean() * (1.0 + mean_r * 0.8)] * extra_n,
            "qty":         [1] * extra_n,
        })
        return pd.concat([b4_df, extra], ignore_index=True)
    elif rvol_thresh > current_rvol:
        # 강화: 하위 20% 거래 제거
        cutoff = b4_df["result_pct"].quantile(0.20)
        return b4_df[b4_df["result_pct"] >= cutoff].copy()
    else:
        return b4_df.copy()


def compute_b4_matrix(b4_df: pd.DataFrame, b4_cfg: dict) -> pd.DataFrame:
    """
    거래량 필터(200/250/300%) × 트레일링 스탑(-10/-15/-20%) 조합별 수익률 매트릭스.

    Returns:
        DataFrame with MultiIndex (rvol_thresh, trail_dist),
        columns: [n, win_rate, avg_return_pct, profit_factor, note]
    """
    current_rvol  = b4_cfg.get("rvol_min",   _B4_DEFAULTS["rvol_min"])
    current_trail = b4_cfg.get("trail_dist",  _B4_DEFAULTS["trail_dist"])

    rows = []
    for rvol in _RVOL_CANDIDATES:
        for trail in _TRAIL_CANDIDATES:
            # 거래량 필터 적용 (근사)
            filtered = _apply_rvol_filter(b4_df, rvol, current_rvol)
            n = len(filtered)
            is_current = (abs(rvol - current_rvol) < 0.01 and abs(trail - current_trail) < 0.01)

            if n == 0:
                rows.append({
                    "rvol_%":    f"{rvol*100:.0f}%",
                    "trail_%":   f"-{trail*100:.0f}%",
                    "n":          0,
                    "win_rate":   0.0,
                    "avg_ret_%":  0.0,
                    "pf":         0.0,
                    "현재설정":   "★" if is_current else "",
                    "note":       "N=0",
                })
                continue

            # 트레일링 스탑 시뮬레이션
            sim_results = filtered.apply(
                lambda row: _simulate_trail(row, trail, current_trail), axis=1
            )
            wins    = (sim_results > 0).sum()
            win_r   = float(wins / n * 100)
            avg_ret = float(sim_results.mean() * 100)
            gross_w = sim_results[sim_results > 0].sum()
            gross_l = abs(sim_results[sim_results < 0].sum())
            pf      = round(float(gross_w / gross_l), 2) if gross_l > 0 else float("inf")
            note    = "근사(RVOL미저장)" if abs(rvol - current_rvol) > 0.01 else ""
            if n < MIN_TRADES_FOR_TUNING:
                note = f"샘플부족(N={n})"

            rows.append({
                "rvol_%":    f"{rvol*100:.0f}%",
                "trail_%":   f"-{trail*100:.0f}%",
                "n":          n,
                "win_rate":   round(win_r, 1),
                "avg_ret_%":  round(avg_ret, 2),
                "pf":         pf,
                "현재설정":   "★" if is_current else "",
                "note":       note,
            })

    df = pd.DataFrame(rows)
    return df


# ═════════════════════════════════════════════════════════════════════
# 4. 튜닝 추천 생성
# ═════════════════════════════════════════════════════════════════════

def generate_recommendations(
    metrics:   Dict[str, BucketMetrics],
    b4_matrix: Optional[pd.DataFrame],
    config:    dict,
    b4_cfg:    dict,
) -> List[Recommendation]:
    """
    현재 설정값과 최적값 비교 → 파라미터 튜닝 제안 목록 반환.

    기준:
      - 승률 ≥ 50% AND 거래 ≥ 10 = 최적 (추천 없음)
      - 승률 < 50% AND 거래 ≥ 10 = 파라미터 조정 제안
      - 거래 < 10                 = 샘플 부족 (비실행 제안)
    """
    recs: List[Recommendation] = []
    risk_cfg  = config.get("risk",      {})
    b3_cfg    = config.get("squeeze",   {})
    b2_cfg    = config.get("etf_swing", {})
    b1_cfg    = config.get("value_long",{})

    # ── B1 가치주 분석 ───────────────────────────────────────────────
    b1 = metrics.get("value_long") or metrics.get("B1") or metrics.get("squeeze_b1")
    if b1:
        recs += _analyze_b1(b1, b1_cfg)

    # ── B2 ETF 스윙 분석 ─────────────────────────────────────────────
    b2 = metrics.get("etf_swing") or metrics.get("B2")
    if b2:
        recs += _analyze_b2(b2, b2_cfg)

    # ── B3 스퀴즈 분석 ───────────────────────────────────────────────
    b3 = metrics.get("squeeze") or metrics.get("B3")
    if b3:
        recs += _analyze_b3(b3, b3_cfg)

    # ── B4 스나이퍼 분석 ─────────────────────────────────────────────
    b4 = metrics.get("B4")
    if b4 or b4_matrix is not None:
        recs += _analyze_b4(b4, b4_matrix, b4_cfg)

    # 우선순위 정렬: high → medium → low
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (order.get(r.priority, 9), r.bucket))
    return recs


def _no_data_rec(bucket: str) -> Recommendation:
    return Recommendation(
        bucket=bucket, parameter="전체",
        current_val="N/A", suggested_val="N/A",
        reason=f"분석 데이터 없음 — {bucket} 거래 내역이 DB에 없습니다.",
        expected="없음", priority="low", is_actionable=False,
    )


def _insufficient_rec(bucket: str, m: BucketMetrics) -> Recommendation:
    needed = MIN_TRADES_FOR_TUNING - m.trades
    return Recommendation(
        bucket=bucket, parameter="전체",
        current_val=f"N={m.trades}",
        suggested_val=f"N≥{MIN_TRADES_FOR_TUNING}",
        reason=f"샘플 부족으로 튜닝 불가 (현재 {m.trades}건, {needed}건 더 필요)",
        expected="추가 페이퍼 트레이딩 후 재분석",
        priority="low", is_actionable=False,
    )


def _analyze_b1(m: BucketMetrics, cfg: dict) -> List[Recommendation]:
    if not m.has_enough_data:
        return [_insufficient_rec("B1", m)]
    if m.is_optimal:
        return []

    recs = []
    sl  = cfg.get("stop_loss_pct",  0.12)
    tp  = cfg.get("take_profit_pct", 0.38)
    rsi = cfg.get("rsi_entry_max",   70.0)

    if m.win_rate < OPTIMAL_WIN_RATE:
        # 승률 낮음 → RSI 진입 문턱 강화
        new_rsi = round(rsi - 5, 0)
        recs.append(Recommendation(
            bucket="B1", parameter="rsi_entry_max",
            current_val=f"{rsi:.0f}", suggested_val=f"{new_rsi:.0f}",
            reason=f"승률 {m.win_rate:.1f}% < {OPTIMAL_WIN_RATE}% — "
                   f"과매수 구간 진입 차단 강화",
            expected=f"과매수 진입 제거 → 승률 +3~5% 기대",
            priority="medium",
        ))

    if m.avg_loss_pct < -sl * 100:
        # 평균 손실이 손절 기준보다 크면 손절 타이트
        new_sl = round(sl * 0.85, 3)
        recs.append(Recommendation(
            bucket="B1", parameter="stop_loss_pct",
            current_val=f"{sl*100:.1f}%", suggested_val=f"{new_sl*100:.1f}%",
            reason=f"평균 손실 {m.avg_loss_pct:.1f}% — 손절가 상향 필요",
            expected=f"손절 조기 실행 → 최대 손실 {(sl-new_sl)*100:.1f}%p 개선",
            priority="high",
        ))

    return recs


def _analyze_b2(m: BucketMetrics, cfg: dict) -> List[Recommendation]:
    if not m.has_enough_data:
        return [_insufficient_rec("B2", m)]
    if m.is_optimal:
        return []

    recs = []
    sl = cfg.get("swing_sl_pct", 0.04)
    tp = cfg.get("swing_tp_pct", 0.08)

    # 손익비 분석
    pnl_ratio = abs(m.avg_win_pct / m.avg_loss_pct) if m.avg_loss_pct != 0 else 0
    if pnl_ratio < 1.5:
        new_tp = round(tp * 1.25, 3)
        recs.append(Recommendation(
            bucket="B2", parameter="swing_tp_pct",
            current_val=f"{tp*100:.1f}%", suggested_val=f"{new_tp*100:.1f}%",
            reason=f"손익비 {pnl_ratio:.2f} < 1.5 — 목표가 상향으로 손익비 개선",
            expected=f"목표가 {tp*100:.1f}% → {new_tp*100:.1f}% → 손익비 +25% 기대",
            priority="medium",
        ))

    if m.win_rate < OPTIMAL_WIN_RATE and m.avg_loss_pct < -sl * 100 * 1.2:
        new_sl = round(sl * 0.80, 3)
        recs.append(Recommendation(
            bucket="B2", parameter="swing_sl_pct",
            current_val=f"{sl*100:.1f}%", suggested_val=f"{new_sl*100:.1f}%",
            reason=f"평균 손실 {m.avg_loss_pct:.1f}% — 손절 타이트 조정",
            expected=f"손절 {sl*100:.1f}% → {new_sl*100:.1f}% → 리스크 {(sl-new_sl)*100:.1f}%p 감소",
            priority="high",
        ))

    return recs


def _analyze_b3(m: BucketMetrics, cfg: dict) -> List[Recommendation]:
    if not m.has_enough_data:
        return [_insufficient_rec("B3", m)]
    if m.is_optimal:
        return []

    recs = []
    min_rvol         = cfg.get("min_rvol", 5.0)
    min_rvol_intraday = cfg.get("min_rvol_intraday", 10.0)
    min_vol_ratio    = cfg.get("min_volume_ratio", 3.0)

    if m.win_rate < OPTIMAL_WIN_RATE:
        new_rvol = round(min_rvol * 1.20, 1)
        recs.append(Recommendation(
            bucket="B3", parameter="min_rvol_intraday",
            current_val=f"{min_rvol_intraday:.1f}x",
            suggested_val=f"{min_rvol_intraday * 1.20:.1f}x",
            reason=f"승률 {m.win_rate:.1f}% — 장중 진입 RVOL 기준 강화로 신호 품질 향상",
            expected=f"진입 기준 강화 → 거래 수 ↓, 승률 +5~8% 기대",
            priority="high",
        ))

    if m.profit_factor < 1.2:
        recs.append(Recommendation(
            bucket="B3", parameter="min_volume_ratio",
            current_val=f"{min_vol_ratio:.1f}x",
            suggested_val=f"{min_vol_ratio * 1.10:.1f}x",
            reason=f"PF {m.profit_factor:.2f} < 1.2 — 저거래량 신호 필터링 강화",
            expected=f"거래량 기준 +10% → 저품질 신호 제거 → PF +0.2 기대",
            priority="medium",
        ))

    return recs


def _analyze_b4(
    m: Optional[BucketMetrics],
    matrix: Optional[pd.DataFrame],
    b4_cfg: dict,
) -> List[Recommendation]:
    recs = []

    if m is None and (matrix is None or matrix.empty):
        return [_no_data_rec("B4")]

    cur_rvol  = b4_cfg.get("rvol_min",   _B4_DEFAULTS["rvol_min"])
    cur_trail = b4_cfg.get("trail_dist",  _B4_DEFAULTS["trail_dist"])

    # 샘플 부족 체크
    if m is not None and not m.has_enough_data:
        return [_insufficient_rec("B4", m)]

    # 매트릭스에서 최적 조합 찾기
    if matrix is not None and not matrix.empty:
        eligible = matrix[matrix["n"] >= MIN_TRADES_FOR_TUNING].copy()
        if not eligible.empty:
            # 승률 ≥ 50% 이면서 avg_ret 최대인 조합 선택
            optimal = eligible[eligible["win_rate"] >= OPTIMAL_WIN_RATE]
            if not optimal.empty:
                best = optimal.loc[optimal["avg_ret_%"].idxmax()]
                # RVOL 제안
                best_rvol_str = best["rvol_%"].replace("%", "")
                try:
                    best_rvol = float(best_rvol_str) / 100
                except Exception:
                    best_rvol = cur_rvol

                if abs(best_rvol - cur_rvol) > 0.01:
                    direction = "하향 (완화)" if best_rvol < cur_rvol else "상향 (강화)"
                    # Win rate change vs current
                    cur_row = matrix[
                        (matrix["rvol_%"] == f"{cur_rvol*100:.0f}%") &
                        (matrix["trail_%"] == f"-{cur_trail*100:.0f}%")
                    ]
                    cur_wr  = float(cur_row["win_rate"].iloc[0]) if not cur_row.empty else 0
                    best_wr = float(best["win_rate"])
                    delta   = best_wr - cur_wr

                    recs.append(Recommendation(
                        bucket="B4", parameter="VOLUME_FILTER (rvol_min)",
                        current_val=f"{cur_rvol*100:.0f}%",
                        suggested_val=f"{best_rvol*100:.0f}%",
                        reason=f"거래량 필터 {direction} 시 매트릭스 최적 조합 달성",
                        expected=(
                            f"거래량 필터 {cur_rvol*100:.0f}% → {best_rvol*100:.0f}% "
                            f"→ 승률 {delta:+.1f}% 개선 기대 "
                            f"(N={best['n']}, WR={best_wr:.1f}%)"
                        ),
                        priority="high" if abs(delta) >= 5 else "medium",
                    ))

                # 트레일링 스탑 제안
                best_trail_str = best["trail_%"].replace("-", "").replace("%", "")
                try:
                    best_trail = float(best_trail_str) / 100
                except Exception:
                    best_trail = cur_trail

                if abs(best_trail - cur_trail) > 0.01:
                    cur_row = matrix[
                        (matrix["rvol_%"] == f"{cur_rvol*100:.0f}%") &
                        (matrix["trail_%"] == f"-{cur_trail*100:.0f}%")
                    ]
                    cur_ret  = float(cur_row["avg_ret_%"].iloc[0]) if not cur_row.empty else 0
                    best_ret = float(best["avg_ret_%"])
                    delta    = best_ret - cur_ret

                    recs.append(Recommendation(
                        bucket="B4", parameter="TRAILING_STOP (trail_dist)",
                        current_val=f"-{cur_trail*100:.0f}%",
                        suggested_val=f"-{best_trail*100:.0f}%",
                        reason=(
                            f"트레일링 스탑 변경 시 평균 수익률 최대화 "
                            f"(시뮬레이션 기반)"
                        ),
                        expected=(
                            f"트레일링 -{cur_trail*100:.0f}% → -{best_trail*100:.0f}% "
                            f"→ 평균 수익 {delta:+.2f}%p 개선 기대"
                        ),
                        priority="medium",
                    ))

    # 실제 B4 성과 추가 진단
    if m is not None and not m.is_optimal:
        recs.append(Recommendation(
            bucket="B4", parameter="전략 전반",
            current_val=f"WR={m.win_rate:.1f}% PF={m.profit_factor:.2f}",
            suggested_val=f"WR≥{OPTIMAL_WIN_RATE}% PF≥1.5",
            reason=(
                f"B4 실적: 승률 {m.win_rate:.1f}%, PF {m.profit_factor:.2f}, "
                f"기대값 ${m.expectancy:+.2f}/거래"
            ),
            expected="위 파라미터 제안 적용 후 재분석",
            priority="high" if m.win_rate < 40 else "medium",
        ))

    return recs


# ═════════════════════════════════════════════════════════════════════
# 5. 출력 — recommendations.log + 텔레그램
# ═════════════════════════════════════════════════════════════════════

def write_recommendations_log(
    result: AnalysisResult,
    log_path: str = LOG_PATH,
) -> None:
    """추천 사항을 recommendations.log에 append."""
    sep  = "=" * 70
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{sep}\n")
        f.write(f"[{result.generated_at}] 분석 기간: 최근 {result.days_analyzed}일\n")
        f.write(f"{sep}\n\n")

        # 버킷별 성과 요약
        f.write("■ 버킷별 성과 요약\n")
        for name, m in result.metrics.items():
            status = "✅ 최적" if m.is_optimal else ("⚠️ 샘플부족" if not m.has_enough_data else "🔴 개선필요")
            f.write(
                f"  [{name}] {status} | "
                f"N={m.trades} 승률={m.win_rate:.1f}% "
                f"PF={m.profit_factor:.2f} 기대값=${m.expectancy:+.2f}\n"
            )
        f.write("\n")

        # B4 매트릭스
        if result.b4_matrix is not None and not result.b4_matrix.empty:
            f.write("■ B4 파라미터 매트릭스 (RVOL × 트레일링)\n")
            f.write(result.b4_matrix.to_string(index=False))
            f.write("\n  * ★ = 현재 설정값 | 근사(RVOL미저장) = RVOL 실측 없이 추정\n\n")

        # 추천 사항
        f.write("■ 파라미터 튜닝 제안\n")
        if not result.recommendations:
            f.write("  → 현재 모든 버킷이 최적 기준(승률 ≥50%, N≥10)을 충족합니다.\n")
        else:
            for i, rec in enumerate(result.recommendations, 1):
                actionable = "" if rec.is_actionable else " [비실행 — 샘플부족]"
                f.write(
                    f"  [{i}] [{rec.priority.upper()}] {rec.bucket} — {rec.parameter}{actionable}\n"
                    f"       현재: {rec.current_val}  →  제안: {rec.suggested_val}\n"
                    f"       근거: {rec.reason}\n"
                    f"       기대: {rec.expected}\n\n"
                )

        f.write(f"{sep}\n")

    print(f"[analyzer] 추천 로그 저장: {log_path}")


def build_telegram_message(result: AnalysisResult) -> str:
    """텔레그램 알림용 요약 메시지 생성."""
    lines = [
        f"📊 <b>전략 튜닝 분석 리포트</b>",
        f"기간: 최근 {result.days_analyzed}일 | {result.generated_at[:16]}",
        "",
    ]

    # 버킷 상태 한 줄 요약
    for name, m in result.metrics.items():
        if not m.has_enough_data:
            icon = "⚠️"
        elif m.is_optimal:
            icon = "✅"
        else:
            icon = "🔴"
        lines.append(
            f"{icon} <b>{name}</b>: 승률 {m.win_rate:.0f}%"
            f" | N={m.trades} | PF={m.profit_factor:.1f}"
        )

    # 실행 가능한 추천만 표시
    actionable = [r for r in result.recommendations if r.is_actionable]
    if actionable:
        lines.append("")
        lines.append("🔧 <b>전략 튜닝 제안</b>")
        for rec in actionable[:5]:   # 텔레그램 과부하 방지 — 최대 5개
            lines.append(
                f"  • <b>{rec.bucket}</b> {rec.parameter}: "
                f"{rec.current_val} → {rec.suggested_val}"
            )
            lines.append(f"    └ {rec.expected}")
    else:
        lines.append("")
        lines.append("✅ 현재 모든 버킷이 최적 기준을 충족합니다.")

    # 샘플 부족 경고
    insufficient = [r for r in result.recommendations if not r.is_actionable]
    if insufficient:
        lines.append("")
        for rec in insufficient:
            lines.append(f"⏳ {rec.bucket}: 샘플 부족으로 튜닝 불가 ({rec.current_val})")

    lines.append("")
    lines.append(f"📁 상세 내역: recommendations.log")
    return "\n".join(lines)


def send_telegram_alert(message: str, config: dict) -> bool:
    """telegram_notifier 를 통해 알림 전송."""
    try:
        from notify.telegram_notifier import send_telegram
        send_telegram(message)
        return True
    except Exception:
        pass
    # 직접 requests 로 폴백
    try:
        import requests
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")
        if not token or not chat_id:
            return False
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.ok
    except Exception as exc:
        print(f"[analyzer] 텔레그램 전송 실패: {exc}")
        return False


# ═════════════════════════════════════════════════════════════════════
# 6. 콘솔 출력
# ═════════════════════════════════════════════════════════════════════

def print_report(result: AnalysisResult) -> None:
    """콘솔에 분석 결과 출력."""
    W = 68
    print(f"\n{'='*W}")
    print(f"  전략 파라미터 최적화 분석기")
    print(f"  기간: 최근 {result.days_analyzed}일  |  생성: {result.generated_at[:16]}")
    print(f"{'='*W}")

    # ── 버킷별 성과 ──────────────────────────────────────────────────
    print(f"\n  ■ 버킷별 성과 요약")
    print(f"  {'─'*66}")
    hdr = f"  {'버킷':<12} {'N':>4} {'승률':>7} {'PF':>6} {'기대값':>10} {'평균보유':>8}  상태"
    print(hdr)
    print(f"  {'─'*66}")
    for name, m in result.metrics.items():
        status = "✅ 최적" if m.is_optimal else (
            "⚠️ 샘플부족" if not m.has_enough_data else "🔴 개선필요"
        )
        print(
            f"  {name:<12} {m.trades:>4} {m.win_rate:>6.1f}% {m.profit_factor:>6.2f}"
            f" ${m.expectancy:>+9.2f} {m.avg_hold_min:>6.0f}분  {status}"
        )
    print(f"  {'─'*66}")

    # ── B4 매트릭스 ──────────────────────────────────────────────────
    if result.b4_matrix is not None and not result.b4_matrix.empty:
        print(f"\n  ■ B4 파라미터 매트릭스 (RVOL 필터 × 트레일링 스탑)")
        print(f"  {'─'*66}")
        matrix_str = result.b4_matrix.to_string(index=False)
        for line in matrix_str.split("\n"):
            print(f"  {line}")
        print(f"  ★ = 현재 설정 | 근사(RVOL미저장) = RVOL 실측값 없이 추정한 근사치")

    # ── 튜닝 제안 ────────────────────────────────────────────────────
    print(f"\n  ■ 파라미터 튜닝 제안")
    print(f"  {'─'*66}")

    if not result.recommendations:
        print("  → 현재 모든 버킷이 최적 기준을 만족합니다. 튜닝 제안 없음.")
    else:
        for i, rec in enumerate(result.recommendations, 1):
            priority_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(rec.priority, "⚪")
            actionable    = "" if rec.is_actionable else " [비실행]"
            print(f"\n  [{i}] {priority_icon} {rec.bucket} — {rec.parameter}{actionable}")
            print(f"       현재값: {rec.current_val}")
            print(f"       제안값: {rec.suggested_val}")
            print(f"       근거  : {rec.reason}")
            print(f"       기대  : {rec.expected}")

    print(f"\n{'='*W}\n")


# ═════════════════════════════════════════════════════════════════════
# 7. 통합 실행
# ═════════════════════════════════════════════════════════════════════

def run_analysis(
    db_path:    str  = DB_MAIN,
    b4_db_path: str  = DB_B4,
    cfg_path:   str  = CFG_PATH,
    log_path:   str  = LOG_PATH,
    days:       int  = 30,
    bucket:     Optional[str] = None,
    notify:     bool = False,
) -> AnalysisResult:
    """
    전체 분석 파이프라인 실행.

    Args:
        db_path:    storage/trade.db 경로
        b4_db_path: storage/db/trading_data.db 경로
        cfg_path:   config.yaml 경로
        log_path:   recommendations.log 저장 경로
        days:       분석 기간 (일)
        bucket:     특정 버킷만 분석 ("b1"~"b4", None = 전체)
        notify:     True 시 텔레그램 알림 전송

    Returns:
        AnalysisResult
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    config  = load_config(cfg_path)
    b4_cfg  = load_b4_config()

    # ── 데이터 로딩 ──────────────────────────────────────────────────
    closed_df = load_closed_trades(db_path,    days=days)
    b4_df     = load_b4_trades    (b4_db_path, days=days)

    # ── 지표 계산 ────────────────────────────────────────────────────
    all_metrics: Dict[str, BucketMetrics] = {}

    if not closed_df.empty:
        bucket_filter = bucket.lower() if bucket else None
        for strat, m in compute_metrics(closed_df, bucket_col="strategy").items():
            if bucket_filter and bucket_filter not in strat.lower():
                continue
            all_metrics[strat] = m

    if not b4_df.empty and (bucket is None or "b4" in bucket.lower()):
        b4_m = compute_b4_metrics(b4_df)
        if b4_m:
            all_metrics["B4"] = b4_m

    # ── B4 매트릭스 ──────────────────────────────────────────────────
    b4_matrix = None
    if (bucket is None or "b4" in (bucket or "").lower()) and not b4_df.empty:
        b4_matrix = compute_b4_matrix(b4_df, b4_cfg)

    # ── 추천 생성 ────────────────────────────────────────────────────
    recs = generate_recommendations(all_metrics, b4_matrix, config, b4_cfg)

    result = AnalysisResult(
        generated_at    = now_str,
        days_analyzed   = days,
        metrics         = all_metrics,
        b4_matrix       = b4_matrix,
        recommendations = recs,
    )

    # ── 콘솔 출력 ────────────────────────────────────────────────────
    print_report(result)

    # ── 로그 파일 저장 ───────────────────────────────────────────────
    write_recommendations_log(result, log_path)

    # ── 텔레그램 알림 ────────────────────────────────────────────────
    if notify:
        msg = build_telegram_message(result)
        ok  = send_telegram_alert(msg, config)
        print(f"[analyzer] 텔레그램 알림: {'전송 완료' if ok else '전송 실패'}")

    return result


# ═════════════════════════════════════════════════════════════════════
# 8. CLI 진입점
# ═════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="페이퍼 트레이딩 전략 파라미터 최적화 분석기",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--days",   type=int,  default=30,
                        help="분석 기간 (기본: 30일)")
    parser.add_argument("--bucket", type=str,  default=None,
                        metavar="BUCKET",
                        help="특정 버킷만 분석 (b1/b2/b3/b4, 기본: 전체)")
    parser.add_argument("--notify", action="store_true",
                        help="분석 완료 후 텔레그램 알림 전송")
    parser.add_argument("--db",     default=DB_MAIN,
                        help=f"B1~B3 DB 경로 (기본: {DB_MAIN})")
    parser.add_argument("--b4db",   default=DB_B4,
                        help=f"B4 DB 경로 (기본: {DB_B4})")
    parser.add_argument("--config", default=CFG_PATH,
                        help=f"설정 파일 경로 (기본: {CFG_PATH})")
    parser.add_argument("--log",    default=LOG_PATH,
                        help=f"추천 로그 경로 (기본: {LOG_PATH})")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    run_analysis(
        db_path    = args.db,
        b4_db_path = args.b4db,
        cfg_path   = args.config,
        log_path   = args.log,
        days       = args.days,
        bucket     = args.bucket,
        notify     = args.notify,
    )


if __name__ == "__main__":
    main()
