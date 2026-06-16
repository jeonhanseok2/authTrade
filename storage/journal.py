# storage/journal.py
"""
일일 매매 일지 자동 생성 + Gemini Pro 심층 분석.

흐름:
  장 마감(16:00 ET) → generate_and_save(db, date) 호출
  → 당일 closed_trades 집계
  → 버킷별/전략별 통계 산출
  → Gemini Pro 분석 (손익 패턴, 내일 전략 방향)
  → daily_journal 저장
  → 텔레그램 전송용 포맷 반환

주간 분석:
  매주 금요일 장 마감 후 → generate_weekly(db, week_start) 호출
  → 최근 5거래일 일지 집계
  → Gemini Pro 주간 심층 분석 (전략 파라미터 조정 권고 포함)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from storage.db import PositionDB


# ─────────────────────────────────────────────────────────────────────
# 통계 계산
# ─────────────────────────────────────────────────────────────────────

def _calc_stats(trades: List[Dict]) -> Dict:
    """closed_trades 리스트에서 핵심 통계 산출."""
    if not trades:
        return {
            "trades_cnt": 0, "win_cnt": 0, "lose_cnt": 0,
            "realized_pnl": 0.0, "win_rate": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "profit_factor": 0.0, "best_trade": {}, "worst_trade": {},
        }

    wins  = [t for t in trades if t["pnl"] > 0]
    loses = [t for t in trades if t["pnl"] <= 0]

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in loses))

    best  = max(trades, key=lambda t: t["pnl_pct"])
    worst = min(trades, key=lambda t: t["pnl_pct"])

    return {
        "trades_cnt":    len(trades),
        "win_cnt":       len(wins),
        "lose_cnt":      len(loses),
        "realized_pnl":  round(sum(t["pnl"] for t in trades), 2),
        "win_rate":      round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win_pct":   round(sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins  else 0.0, 2),
        "avg_loss_pct":  round(sum(t["pnl_pct"] for t in loses) / len(loses) if loses else 0.0, 2),
        "profit_factor": round(gross_win / gross_loss if gross_loss > 0 else 0.0, 2),
        "best_trade":    {"symbol": best["symbol"],  "pnl_pct": best["pnl_pct"],  "strategy": best["strategy"],  "reason": best["exit_reason"]},
        "worst_trade":   {"symbol": worst["symbol"], "pnl_pct": worst["pnl_pct"], "strategy": worst["strategy"], "reason": worst["exit_reason"]},
    }


def _bucket_stats(trades: List[Dict]) -> Dict:
    """버킷별 통계."""
    result = {}
    for strategy in ("value_long", "etf_swing", "squeeze"):
        subset = [t for t in trades if t["strategy"] == strategy]
        if not subset:
            continue
        wins = [t for t in subset if t["pnl"] > 0]
        result[strategy] = {
            "cnt":      len(subset),
            "win_rate": round(len(wins) / len(subset) * 100, 1),
            "pnl":      round(sum(t["pnl"] for t in subset), 2),
            "avg_hold": round(sum(t["hold_minutes"] for t in subset) / len(subset), 0),
        }
    return result


# ─────────────────────────────────────────────────────────────────────
# Gemini 프롬프트 빌더
# ─────────────────────────────────────────────────────────────────────

def _build_daily_prompt(date: str, stats: Dict, bucket: Dict, trades: List[Dict]) -> str:
    trade_lines = []
    for t in sorted(trades, key=lambda x: x["pnl"], reverse=True):
        sign = "+" if t["pnl"] >= 0 else ""
        trade_lines.append(
            f"  {t['symbol']:<6} [{t['strategy']:<12}] "
            f"{sign}{t['pnl_pct']:+.1f}%  ${t['pnl']:+.2f}  "
            f"보유={t['hold_minutes']}분  청산={t['exit_reason']}"
        )

    bucket_lines = []
    for bkt, s in bucket.items():
        bucket_lines.append(
            f"  {bkt:<12}: {s['cnt']}건  승률={s['win_rate']}%  손익=${s['pnl']:+.2f}  평균보유={s['avg_hold']:.0f}분"
        )

    return f"""당신은 미국 주식 단기매매 전문 애널리스트입니다. Wall Street 시니어 애널리스트처럼 데이터 기반으로만 판단하세요.

오늘({date}) 매매 데이터를 분석하고 내일 전략 방향을 제시하세요.

=== 오늘 매매 요약 ===
총 거래: {stats['trades_cnt']}건 | 승: {stats['win_cnt']} / 패: {stats['lose_cnt']}
승률: {stats['win_rate']}% | 실현손익: ${stats['realized_pnl']:+.2f}
평균수익: +{stats['avg_win_pct']:.2f}% | 평균손실: {stats['avg_loss_pct']:.2f}%
수익팩터: {stats['profit_factor']:.2f}

=== 버킷별 성과 ===
{chr(10).join(bucket_lines) if bucket_lines else "  (거래 없음)"}

=== 거래 상세 ===
{chr(10).join(trade_lines) if trade_lines else "  (거래 없음)"}

=== 1단계: 데이터 기반 패턴 분석 ===
다음을 한국어로 분석하세요:

1. **오늘 패턴** (2-3줄)
   - 수익 전략 vs 손실 전략의 공통점
   - 청산 사유(exit_reason) 분포에서 보이는 시스템 편향
   - 보유 시간과 수익률의 상관관계

2. **리스크 신호** (1-2줄)
   - 수익팩터 < 1.5 또는 승률 < 45% 시 구체적 원인 진단
   - 분할청산(partial_exit) 효과가 있었는지

3. **내일 전략 방향** (2-3줄)
   - 집중할 버킷과 근거 (데이터 수치 인용)
   - VWAP/RSI/볼린저 관점에서 조심할 시장 조건

4. **파라미터 조정** (데이터 근거 있을 때만 — 수치 포함)
   - 손절%/익절%/분할청산 트리거 중 1가지만

=== 2단계: 자기 검증 ===
위 1-4 분석 중 데이터 없이 추측한 내용이 있으면 "(데이터 부족)" 표시 후 생략하세요.

답변은 총 12줄 이내로 간결하게 작성하세요."""


def _build_weekly_prompt(week_start: str, journals: List[Dict], all_trades: List[Dict]) -> str:
    daily_lines = []
    for j in journals:
        daily_lines.append(
            f"  {j['date']}: {j['trades_cnt']}건  승률={j['win_rate']}%  손익=${j['realized_pnl']:+.2f}  PF={j['profit_factor']:.2f}"
        )

    # 전략별 집계
    strategy_totals: Dict[str, Dict] = {}
    for t in all_trades:
        s = t["strategy"]
        if s not in strategy_totals:
            strategy_totals[s] = {"cnt": 0, "wins": 0, "pnl": 0.0}
        strategy_totals[s]["cnt"] += 1
        if t["pnl"] > 0:
            strategy_totals[s]["wins"] += 1
        strategy_totals[s]["pnl"] += t["pnl"]

    strat_lines = []
    for s, v in strategy_totals.items():
        wr = v["wins"] / v["cnt"] * 100 if v["cnt"] else 0
        strat_lines.append(f"  {s:<12}: {v['cnt']}건  승률={wr:.1f}%  총손익=${v['pnl']:+.2f}")

    total_pnl = sum(j["realized_pnl"] for j in journals)
    total_wr  = sum(j["win_rate"] for j in journals) / len(journals) if journals else 0

    return f"""당신은 퀀트 펀드 포트폴리오 매니저입니다. 이번 주 미국 주식 자동매매 성과를 심층 분석하고 다음 주 전략을 수립하세요.

=== 주간 요약 ({week_start} ~ ) ===
총 거래일: {len(journals)}일 | 평균 승률: {total_wr:.1f}% | 주간 총손익: ${total_pnl:+.2f}

=== 일별 성과 ===
{chr(10).join(daily_lines) if daily_lines else "  (데이터 없음)"}

=== 전략별 누계 ===
{chr(10).join(strat_lines) if strat_lines else "  (데이터 없음)"}

=== 1단계: 수익 구조 분석 ===
다음을 한국어로 답하세요:

1. **핵심 수익 드라이버** (3줄)
   - 수익팩터 > 1.5인 전략의 공통 조건 (시간대/섹터/청산 방식)
   - 손실이 집중된 요일/전략/청산 사유 패턴
   - 분할청산(partial_exit) vs 일괄청산 수익률 비교 (데이터 기반)

2. **리스크 평가** (포지션 사이징 / 집중도 / 킬스위치 작동 여부)

3. **버킷 비중 조정 권고** (구체적 숫자 필수)
   - 현재: B1(value_long) 10% / B2(etf_swing) 40% / B3(squeeze) 50%
   - 다음 주 권장 비중 및 데이터 근거 (성과 없으면 현행 유지 권고)

4. **파라미터 조정** (수익팩터 또는 승률 근거 있을 때만)
   - 손절%, 익절%, 분할청산 트리거, RSI 임계값 중 최대 2가지

5. **다음 주 집중 전략** (1가지만 — 근거 수치 포함)

=== 2단계: 자기 검증 ===
위 분석 중 실거래 데이터 없이 일반론으로 쓴 문장에 "(추측)" 표시하세요.
데이터가 없으면 "데이터 부족으로 판단 유보"라고 명시하세요.

답변은 15줄 이내로 작성하세요."""


# ─────────────────────────────────────────────────────────────────────
# 일일 일지 생성
# ─────────────────────────────────────────────────────────────────────

def generate_and_save(
    db: PositionDB,
    date: Optional[str] = None,
    send_telegram: bool = True,
) -> Optional[str]:
    """
    일일 매매 일지 생성 + DB 저장 + 텔레그램 전송.

    Args:
        db:             PositionDB 인스턴스
        date:           YYYY-MM-DD (None이면 오늘 ET 기준)
        send_telegram:  True면 telegram_notifier로 전송

    Returns:
        텔레그램용 포맷 문자열 (실패 시 None)
    """
    from zoneinfo import ZoneInfo
    if date is None:
        date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    trades = db.get_closed_trades(date_str=date)
    stats  = _calc_stats(trades)
    bucket = _bucket_stats(trades)

    # Gemini Pro 분석
    ai_text = ""
    if trades:
        try:
            from ai.gemini_helper import call_gemini, GeminiTask
            prompt  = _build_daily_prompt(date, stats, bucket, trades)
            ai_text = call_gemini(
                prompt, task=GeminiTask.LARGE_LOSS,   # Pro 모델 사용
                temperature=0.15, max_tokens=600,
            ) or ""
        except Exception as exc:
            logging.warning("[journal] Gemini 분석 실패: %s", exc)

    # DB 저장
    db.save_daily_journal(
        date          = date,
        trades_cnt    = stats["trades_cnt"],
        win_cnt       = stats["win_cnt"],
        lose_cnt      = stats["lose_cnt"],
        realized_pnl  = stats["realized_pnl"],
        win_rate      = stats["win_rate"],
        avg_win_pct   = stats["avg_win_pct"],
        avg_loss_pct  = stats["avg_loss_pct"],
        profit_factor = stats["profit_factor"],
        best_trade    = json.dumps(stats["best_trade"],  ensure_ascii=False),
        worst_trade   = json.dumps(stats["worst_trade"], ensure_ascii=False),
        bucket_stats  = json.dumps(bucket,               ensure_ascii=False),
        ai_analysis   = ai_text,
    )

    msg = _format_journal_message(date, stats, bucket, ai_text)

    if send_telegram:
        try:
            from notify.telegram_notifier import send_telegram as tg
            tg(msg)
        except Exception as exc:
            logging.warning("[journal] 텔레그램 전송 실패: %s", exc)

    logging.info("[journal] %s 일지 저장 완료 (%d건)", date, stats["trades_cnt"])
    return msg


# ─────────────────────────────────────────────────────────────────────
# 주간 분석 생성
# ─────────────────────────────────────────────────────────────────────

def generate_weekly(
    db: PositionDB,
    week_start: Optional[str] = None,
    send_telegram: bool = True,
) -> Optional[str]:
    """
    주간 매매 분석 생성 + DB 저장.

    Args:
        week_start: 해당 주 월요일 (None이면 이번 주 월요일)
    """
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York"))

    if week_start is None:
        monday = today - timedelta(days=today.weekday())
        week_start = monday.strftime("%Y-%m-%d")

    week_end_dt = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=4)
    week_end    = week_end_dt.strftime("%Y-%m-%d")

    all_trades = db.get_closed_trades_range(week_start, week_end)
    journals   = db.get_recent_journals(days=7)
    journals   = [j for j in journals if week_start <= j["date"] <= week_end]

    stats = _calc_stats(all_trades)

    # 최대 낙폭 계산
    cumulative, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(all_trades, key=lambda x: x["exit_ts"]):
        cumulative += t["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = (peak - cumulative) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # 최고 전략
    bucket  = _bucket_stats(all_trades)
    best_bkt = max(bucket, key=lambda b: bucket[b]["pnl"]) if bucket else ""

    # Gemini Pro 주간 분석
    ai_text = ""
    if all_trades:
        try:
            from ai.gemini_helper import call_gemini, GeminiTask
            prompt  = _build_weekly_prompt(week_start, journals, all_trades)
            ai_text = call_gemini(
                prompt, task=GeminiTask.PORTFOLIO_REBALANCE,
                temperature=0.1, max_tokens=700,
            ) or ""
        except Exception as exc:
            logging.warning("[journal] 주간 Gemini 분석 실패: %s", exc)

    db.save_weekly_analysis(
        week_start       = week_start,
        total_trades     = stats["trades_cnt"],
        win_rate         = stats["win_rate"],
        total_pnl        = stats["realized_pnl"],
        max_drawdown_pct = round(max_dd * 100, 2),
        best_strategy    = best_bkt,
        worst_setup      = json.dumps(stats.get("worst_trade", {}), ensure_ascii=False),
        ai_analysis      = ai_text,
    )

    msg = _format_weekly_message(week_start, week_end, stats, bucket, max_dd, ai_text)

    if send_telegram:
        try:
            from notify.telegram_notifier import send_telegram as tg
            tg(msg)
        except Exception as exc:
            logging.warning("[journal] 주간 텔레그램 전송 실패: %s", exc)

    logging.info("[journal] %s 주간 분석 저장 완료", week_start)
    return msg


# ─────────────────────────────────────────────────────────────────────
# 메시지 포맷
# ─────────────────────────────────────────────────────────────────────

def _format_journal_message(date: str, stats: Dict, bucket: Dict, ai_text: str) -> str:
    pnl_sign = "+" if stats["realized_pnl"] >= 0 else ""
    emoji    = "🟢" if stats["realized_pnl"] >= 0 else "🔴"

    lines = [
        f"{emoji} <b>{date} 매매 일지</b>",
        "",
        f"총 {stats['trades_cnt']}건  승률 {stats['win_rate']}%  손익 {pnl_sign}${stats['realized_pnl']:.2f}",
        f"평균수익 +{stats['avg_win_pct']:.1f}%  평균손실 {stats['avg_loss_pct']:.1f}%  PF {stats['profit_factor']:.2f}",
    ]

    if bucket:
        lines.append("")
        lines.append("📊 버킷별")
        for bkt, s in bucket.items():
            bkt_label = {"value_long": "B1 가치주", "etf_swing": "B2 ETF", "squeeze": "B3 급등주"}.get(bkt, bkt)
            sign = "+" if s["pnl"] >= 0 else ""
            lines.append(f"  {bkt_label}: {s['cnt']}건 승률{s['win_rate']}% {sign}${s['pnl']:.2f}")

    if stats.get("best_trade") and stats["best_trade"].get("symbol"):
        b = stats["best_trade"]
        lines.append(f"\n최고: {b['symbol']} {b['pnl_pct']:+.1f}% ({b['strategy']})")

    if stats.get("worst_trade") and stats["worst_trade"].get("symbol"):
        w = stats["worst_trade"]
        lines.append(f"최저: {w['symbol']} {w['pnl_pct']:+.1f}% ({w['strategy']})")

    if ai_text:
        lines.append("")
        lines.append("🤖 <b>AI 분석</b>")
        lines.append(ai_text[:800])

    return "\n".join(lines)


def _format_weekly_message(
    week_start: str, week_end: str,
    stats: Dict, bucket: Dict,
    max_dd: float, ai_text: str,
) -> str:
    pnl_sign = "+" if stats["realized_pnl"] >= 0 else ""
    emoji    = "🟢" if stats["realized_pnl"] >= 0 else "🔴"

    lines = [
        f"{emoji} <b>주간 분석 ({week_start} ~ {week_end})</b>",
        "",
        f"총 {stats['trades_cnt']}건  승률 {stats['win_rate']}%",
        f"주간손익 {pnl_sign}${stats['realized_pnl']:.2f}  최대낙폭 -{max_dd*100:.1f}%  PF {stats['profit_factor']:.2f}",
    ]

    if bucket:
        lines.append("")
        lines.append("📊 전략별 누계")
        for bkt, s in bucket.items():
            bkt_label = {"value_long": "B1 가치주", "etf_swing": "B2 ETF", "squeeze": "B3 급등주"}.get(bkt, bkt)
            sign = "+" if s["pnl"] >= 0 else ""
            lines.append(f"  {bkt_label}: {s['cnt']}건 승률{s['win_rate']}% {sign}${s['pnl']:.2f}")

    if ai_text:
        lines.append("")
        lines.append("🤖 <b>주간 AI 분석</b>")
        lines.append(ai_text[:1000])

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# 빠른 통계 요약 (텔레그램 /stats 용)
# ─────────────────────────────────────────────────────────────────────

def format_stats_message(db: PositionDB, days: int = 30) -> str:
    strat_stats  = db.get_strategy_stats(days=days)
    reason_stats = db.get_exit_reason_stats(days=days)
    journals     = db.get_recent_journals(days=days)

    total_pnl = sum(j["realized_pnl"] for j in journals)
    avg_wr    = sum(j["win_rate"] for j in journals) / len(journals) if journals else 0

    lines = [
        f"📈 <b>최근 {days}일 누계 통계</b>",
        "",
        f"총 손익: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}  평균 승률: {avg_wr:.1f}%",
        "",
        "📊 전략별 성과",
    ]
    for s in strat_stats:
        wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
        lines.append(
            f"  {s['strategy']:<12}: {s['total']}건  승률{wr}%  손익${s['total_pnl']:+.2f}  평균보유{s['avg_hold']:.0f}분"
        )

    if reason_stats:
        lines.append("")
        lines.append("🚪 청산 사유별")
        for r in reason_stats[:6]:
            lines.append(
                f"  {r['exit_reason']:<16}: {r['cnt']}건  평균{r['avg_pnl_pct']:+.1f}%  합계${r['total_pnl']:+.2f}"
            )

    return "\n".join(lines)
