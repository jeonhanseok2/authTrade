# analysis/governance.py
"""
거버넌스 분석 모듈.
기관 투자자·내부자·대주주 비중과 공매도를 분석해
'스마트 머니'가 어느 방향으로 움직이는지 파악한다.

전문 트레이더 관점:
  - 기관 비중 70%+ → 안정적 수요층 존재 (가치주에 유리)
  - 내부자 매수 증가 → 경영진의 긍정적 전망 신호
  - 공매도 비율 상승 → 하방 압박 증가 (스퀴즈 후보 종목 탐색에도 활용)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yfinance as yf

# 캐시 (15분 TTL — 거버넌스 데이터는 자주 바뀌지 않음)
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 900


def _cached(key: str, fn, *args):
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _CACHE_TTL:
        return _CACHE[key][1]
    val = fn(*args)
    _CACHE[key] = (now, val)
    return val


@dataclass
class GovernanceInfo:
    """종목 거버넌스 분석 결과."""
    symbol:               str   = ""
    institutional_pct:    float = 0.0    # 기관 보유 비중 (%)
    insider_pct:          float = 0.0    # 내부자 보유 비중 (%)
    top_holder:           str   = ""     # 최대 주주 이름
    top_holder_pct:       float = 0.0    # 최대 주주 비중 (%)
    short_pct_float:      float = 0.0    # 공매도 비율 (유동주식 대비 %)
    insider_buy_count:    int   = 0      # 최근 내부자 매수 건수
    insider_sell_count:   int   = 0      # 최근 내부자 매도 건수
    insider_net_signal:   str   = "neutral"  # bullish / bearish / neutral
    score:                float = 0.0    # 거버넌스 점수 (0~100)
    notes:                List[str] = field(default_factory=list)


def _fetch_governance(symbol: str) -> GovernanceInfo:
    """yfinance를 통해 거버넌스 정보 수집."""
    info = GovernanceInfo(symbol=symbol)
    try:
        ticker = yf.Ticker(symbol)

        # ── 기관/내부자 비중 ──────────────────────────────────────────
        major = ticker.major_holders  # DataFrame
        if major is not None and not major.empty:
            for _, row in major.iterrows():
                desc  = str(row.iloc[1]).lower() if len(row) > 1 else ""
                val   = row.iloc[0]
                # yfinance 컬럼 형식: 숫자/문자열 혼재, '%' 포함
                pct_val = 0.0
                try:
                    pct_val = float(str(val).replace("%", "").strip())
                except ValueError:
                    pass
                if "institutional" in desc and "insider" not in desc:
                    info.institutional_pct = round(pct_val, 2)
                elif "insider" in desc:
                    info.insider_pct = round(pct_val, 2)

        # ── 최대 기관 주주 ────────────────────────────────────────────
        inst = ticker.institutional_holders
        if inst is not None and not inst.empty:
            top = inst.iloc[0]
            info.top_holder     = str(top.get("Holder", "")) if hasattr(top, "get") else ""
            pct_raw = top.get("% Out", 0.0) if hasattr(top, "get") else 0.0
            info.top_holder_pct = round(float(pct_raw or 0.0) * 100, 2)

        # ── 공매도 비율 ───────────────────────────────────────────────
        ticker_info = ticker.info or {}
        short_pct   = ticker_info.get("shortPercentOfFloat", 0.0) or 0.0
        info.short_pct_float = round(float(short_pct) * 100, 2)

        # ── 내부자 거래 방향 ──────────────────────────────────────────
        insider_txn = ticker.insider_transactions
        if insider_txn is not None and not insider_txn.empty:
            for _, row in insider_txn.iterrows():
                txn_type = str(row.get("Transaction", "") or "").lower()
                if "purchase" in txn_type or "buy" in txn_type:
                    info.insider_buy_count += 1
                elif "sale" in txn_type or "sell" in txn_type:
                    info.insider_sell_count += 1
            # 신호 판단
            if info.insider_buy_count > info.insider_sell_count:
                info.insider_net_signal = "bullish"
            elif info.insider_sell_count > info.insider_buy_count * 2:
                info.insider_net_signal = "bearish"
            else:
                info.insider_net_signal = "neutral"

    except Exception as exc:
        logging.debug("[governance] %s 수집 실패: %s", symbol, exc)

    # ── 거버넌스 점수 산출 (0~100) ──────────────────────────────────
    info.score  = _compute_score(info)
    info.notes  = _generate_notes(info)
    return info


def _compute_score(g: GovernanceInfo) -> float:
    """
    거버넌스 점수 (0~100).
    - 기관 비중 60%+     : +30점
    - 내부자 순매수 신호  : +25점
    - 공매도 비율 < 5%   : +20점
    - 최대 주주 비중 < 30% : +15점 (분산 구조 선호)
    - 내부자 비중 5~20%  : +10점 (적절한 내부자 참여)
    """
    score = 0.0
    if g.institutional_pct >= 60:
        score += 30
    elif g.institutional_pct >= 40:
        score += 15

    if g.insider_net_signal == "bullish":
        score += 25
    elif g.insider_net_signal == "neutral":
        score += 10

    if g.short_pct_float < 5:
        score += 20
    elif g.short_pct_float < 15:
        score += 10

    if 0 < g.top_holder_pct < 30:
        score += 15
    elif g.top_holder_pct >= 30:
        score += 5   # 대주주 집중은 감점 없되 가점 감소

    if 5 <= g.insider_pct <= 20:
        score += 10
    elif g.insider_pct > 0:
        score += 5

    return round(min(score, 100), 1)


def _generate_notes(g: GovernanceInfo) -> List[str]:
    """거버넌스 경고/긍정 신호 텍스트 생성."""
    notes = []
    if g.institutional_pct >= 70:
        notes.append(f"기관 비중 높음({g.institutional_pct}%) — 안정적 수요층")
    if g.short_pct_float > 20:
        notes.append(f"공매도 비율 과다({g.short_pct_float}%) — 숏 스퀴즈 가능성 또는 하방 위험")
    elif g.short_pct_float > 10:
        notes.append(f"공매도 비율 주의({g.short_pct_float}%)")
    if g.insider_net_signal == "bullish":
        notes.append(f"내부자 순매수 ({g.insider_buy_count}건) — 경영진 긍정 전망")
    elif g.insider_net_signal == "bearish":
        notes.append(f"내부자 순매도 ({g.insider_sell_count}건) — 경영진 하방 헤지 의심")
    return notes


def analyze_governance(symbol: str) -> GovernanceInfo:
    """거버넌스 분석 (캐시 15분)."""
    return _cached(f"gov_{symbol}", _fetch_governance, symbol)


def format_governance_report(g: GovernanceInfo) -> str:
    """텔레그램 전송용 거버넌스 분석 텍스트."""
    signal_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "⚖️"}.get(g.insider_net_signal, "⚖️")
    lines = [
        f"🏛 <b>{g.symbol} 거버넌스 분석</b>",
        f"거버넌스 점수: <b>{g.score}/100</b>",
        f"기관 보유: {g.institutional_pct}%  |  내부자: {g.insider_pct}%",
        f"공매도 비율: {g.short_pct_float}%",
        f"내부자 신호: {signal_emoji} {g.insider_net_signal.upper()} (매수 {g.insider_buy_count}건 / 매도 {g.insider_sell_count}건)",
    ]
    if g.top_holder:
        lines.append(f"최대 주주: {g.top_holder} ({g.top_holder_pct}%)")
    for note in g.notes:
        lines.append(f"⚠️ {note}")
    return "\n".join(lines)
