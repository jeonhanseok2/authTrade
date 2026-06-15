# core/bucket_capital.py
"""
버킷별 자금 격리 + 성과 기반 동적 비중 조정.

초기 공격적 비중 (소액 계좌 성장 최우선):
  B1 가치주:  10%   (min 5%,  max 30%)  ← 안전망 수준만 유지
  B2 ETF:     25%   (min 15%, max 40%)  ← 중간 빈도 수익
  B3 급등주:  65%   (min 40%, max 80%)  ← 핵심 성장 엔진

동적 비중 조정 원칙:
  - 수익률 높은 버킷 → 비중 상향 (min/max 범위 내)
  - 공식: blended = 0.7 × 성과비중 + 0.3 × 기본비중
  - 편차 >= 15% 시 즉시 리밸런싱, 그 외 60분마다 체크
  - 계좌가 $25,000 초과 시 B1 비중을 점진적으로 상향 권장

자금 격리 원칙:
  - 각 버킷은 allocated() 한도 내에서만 주문 가능
  - 한 버킷 손실이 다른 버킷 예산을 침범 불가
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict


BUCKETS = ("value_long", "etf_swing", "squeeze")

BASE_WEIGHTS: Dict[str, float] = {"value_long": 0.10, "etf_swing": 0.25, "squeeze": 0.65}
MIN_WEIGHTS:  Dict[str, float] = {"value_long": 0.05, "etf_swing": 0.15, "squeeze": 0.40}
MAX_WEIGHTS:  Dict[str, float] = {"value_long": 0.30, "etf_swing": 0.40, "squeeze": 0.80}


@dataclass
class BucketCapitalManager:
    total_equity: float
    weights: Dict[str, float] = field(default_factory=lambda: BASE_WEIGHTS.copy())

    # 버킷별 누적 수익률 — 성과 기반 리밸런싱 기준
    _returns: Dict[str, float] = field(
        default_factory=lambda: {b: 0.0 for b in BUCKETS}, init=False,
    )

    def allocated(self, bucket: str) -> float:
        """버킷에 할당된 최대 사용 가능 금액 (자금 격리 한도)."""
        return self.total_equity * self.weights.get(bucket, 0.0)

    def update_equity(self, new_equity: float) -> None:
        self.total_equity = new_equity

    def record_return(self, bucket: str, return_pct: float) -> None:
        """버킷 수익률 누적 기록 (청산 시 호출)."""
        self._returns[bucket] = self._returns.get(bucket, 0.0) + return_pct

    def rebalance(self) -> Dict[str, float]:
        """
        성과 비례 동적 비중 재산출.

        수익률 양수인 버킷에 성과 비중을 부여,
        기본 비중 30% + 성과 비중 70%로 블렌딩 후 min/max 클램프 → 합계=1 정규화.
        """
        pos_sum = sum(max(0.0, v) for v in self._returns.values())
        new_w: Dict[str, float] = {}

        for b in BUCKETS:
            perf = self._returns.get(b, 0.0)
            perf_w = (max(0.0, perf) / pos_sum) if pos_sum > 0 else BASE_WEIGHTS[b]
            blended = 0.7 * perf_w + 0.3 * BASE_WEIGHTS[b]
            new_w[b] = max(MIN_WEIGHTS[b], min(MAX_WEIGHTS[b], blended))

        total = sum(new_w.values())
        self.weights = {k: v / total for k, v in new_w.items()}

        logging.info(
            "[BucketCapital] 리밸런싱: B1=%.1f%% B2=%.1f%% B3=%.1f%%",
            self.weights["value_long"] * 100,
            self.weights["etf_swing"]  * 100,
            self.weights["squeeze"]    * 100,
        )
        return self.weights

    def check_and_rebalance(self, divergence_threshold: float = 0.15) -> bool:
        """
        현재 비중과 기본 비중의 최대 편차가 threshold 이상이면 즉시 리밸런싱.
        """
        max_drift = max(abs(self.weights.get(b, 0) - BASE_WEIGHTS[b]) for b in BUCKETS)
        if max_drift >= divergence_threshold:
            self.rebalance()
            return True
        return False

    def summary(self) -> str:
        lines = ["버킷 자금 현황:"]
        for b in BUCKETS:
            amt = self.allocated(b)
            pct = self.weights.get(b, 0) * 100
            ret = self._returns.get(b, 0.0) * 100
            lines.append(f"  {b:15s} ${amt:>12,.0f}  ({pct:.1f}%)  누적수익: {ret:+.1f}%")
        return "\n".join(lines)
