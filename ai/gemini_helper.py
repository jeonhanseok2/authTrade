# ai/gemini_helper.py
"""
Google Gemini 듀얼 모델 라우터.

SDK: google-genai (google.genai) — 구 google.generativeai 대체
  pip install google-genai

모델 선택 원칙:
  Flash  (gemini-2.0-flash) — 빠르고 저렴, 일상적 분석에 사용
    · 뉴스 요약, 시장 동향 브리핑
    · 종목 간단 분석, 실적 해설
    · 텔레그램 알림 메시지 생성

  Pro    (gemini-2.5-pro)   — 강력한 추론, 고비용 → 꼭 필요할 때만 호출
    · 매매 전략 수정 / 파라미터 조정 권고
    · 포트폴리오 리밸런싱 판단
    · 큰 손실 발생 시 원인 분석 + 대응 전략
    · 시장 레짐 전환 심층 분석 (Bull→Bear 등)
    · 여러 지표가 충돌할 때 최종 판단

환경변수:
  GEMINI_API_KEY    : Google AI Studio API 키 (필수)
  GEMINI_FLASH_MODEL: Flash 모델명 (기본: gemini-2.0-flash)
  GEMINI_PRO_MODEL  : Pro 모델명   (기본: gemini-2.5-pro)
"""
from __future__ import annotations

import logging
import os
import time
from enum import Enum
from typing import Optional

# Pro 호출 기준 상수 — 이 조건 중 하나라도 해당하면 Pro 사용
PRO_TRIGGERS = {
    "strategy_change",       # 전략 파라미터 변경 제안 요청
    "portfolio_rebalance",   # 포트폴리오 리밸런싱
    "large_loss",            # 큰 손실 발생 분석 (일 손실 -3%+)
    "regime_change",         # 시장 레짐 전환 분석
    "conflict_signals",      # 지표 충돌 최종 판단
    "risk_override",         # 리스크 한도 초과 상황 대응
}


class GeminiTask(str, Enum):
    """Gemini 호출 태스크 분류 (Flash vs Pro 자동 선택)."""
    # ── Flash 태스크 ───────────────────────────────────────────────
    NEWS_SUMMARY        = "news_summary"         # 뉴스 요약
    MARKET_BRIEF        = "market_brief"         # 시장 동향 브리핑
    STOCK_ANALYSIS      = "stock_analysis"       # 종목 간단 분석
    EARNINGS_EXPLAIN    = "earnings_explain"     # 실적 해설
    TELEGRAM_MESSAGE    = "telegram_message"     # 알림 메시지 생성
    SENTIMENT_LABEL     = "sentiment_label"      # 감성 라벨링
    # ── Pro 태스크 ────────────────────────────────────────────────
    STRATEGY_CHANGE     = "strategy_change"      # 전략 수정 권고
    PORTFOLIO_REBALANCE = "portfolio_rebalance"  # 리밸런싱
    LARGE_LOSS          = "large_loss"           # 큰 손실 분석
    REGIME_CHANGE       = "regime_change"        # 레짐 전환 분석
    CONFLICT_SIGNALS    = "conflict_signals"     # 지표 충돌 판단
    RISK_OVERRIDE       = "risk_override"        # 리스크 초과 대응


# Pro가 필요한 태스크 집합
_PRO_TASKS = {
    GeminiTask.STRATEGY_CHANGE,
    GeminiTask.PORTFOLIO_REBALANCE,
    GeminiTask.LARGE_LOSS,
    GeminiTask.REGIME_CHANGE,
    GeminiTask.CONFLICT_SIGNALS,
    GeminiTask.RISK_OVERRIDE,
}

# 시스템 프롬프트 — GenerativeModel(system_instruction=...) 에 전달
# 사용자 프롬프트와 분리하면 역할 준수율이 높아지고 토큰도 절약됨
_SYSTEM_PROMPT = (
    "당신은 미국 주식 자동매매 시스템의 AI 어드바이저입니다.\n"
    "전문 퀀트 트레이더 수준의 분석을 한국어로 제공하세요.\n"
    "규칙:\n"
    "1. 반드시 제공된 수치 데이터에 근거해서만 판단하세요.\n"
    "2. 데이터가 없는 항목은 추측하지 말고 '(데이터 부족)'으로 표기하세요.\n"
    "3. 답변은 간결하고 즉시 실행 가능한 내용으로 작성하세요.\n"
    "4. 투자 조언이 아닌 시스템 파라미터 최적화 관점으로 답하세요."
)


# 429 재시도 대기 시간(초) — 순서대로 시도
_RETRY_DELAYS = [3, 10, 30]


def _get_model_name(task: GeminiTask) -> str:
    """태스크에 따라 Flash 또는 Pro 모델명 반환."""
    if task in _PRO_TASKS:
        return os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
    return os.getenv("GEMINI_FLASH_MODEL", "gemini-2.0-flash")


# 모듈 레벨 클라이언트 캐시 (API 키당 1회 초기화)
_CLIENT_CACHE: dict = {}


def _get_client():
    """google.genai 클라이언트 반환 (지연 로딩 + 캐시)."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
    if api_key not in _CLIENT_CACHE:
        try:
            from google import genai  # type: ignore
            _CLIENT_CACHE[api_key] = genai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "google-genai 패키지가 필요합니다: pip install google-genai"
            )
    return _CLIENT_CACHE[api_key]


def call_gemini(
    prompt:      str,
    task:        GeminiTask = GeminiTask.NEWS_SUMMARY,
    temperature: float = 0.2,
    max_tokens:  int   = 512,
    context:     Optional[str] = None,  # 추가 컨텍스트 (포지션 현황 등)
) -> Optional[str]:
    """
    Gemini 호출 — 태스크에 따라 Flash/Pro 자동 선택.

    Args:
        prompt:      사용자 입력 프롬프트
        task:        태스크 분류 (Flash vs Pro 결정)
        temperature: 창의성 (0=결정적, 1=창의적)
        max_tokens:  최대 응답 토큰
        context:     추가 컨텍스트 (포지션, 계좌 정보 등)

    Returns:
        응답 텍스트 또는 None (실패 시)
    """
    model_name = _get_model_name(task)
    tier = "PRO" if task in _PRO_TASKS else "Flash"

    logging.info("[gemini] 호출: %s (%s) task=%s", model_name, tier, task.value)

    # 사용자 메시지: 컨텍스트 + 실제 프롬프트
    user_content = prompt
    if context:
        user_content = f"[현재 컨텍스트]\n{context}\n\n{prompt}"

    last_exc: Optional[Exception] = None
    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        try:
            client = _get_client()
            from google.genai import types  # type: ignore

            response = client.models.generate_content(
                model    = model_name,
                contents = user_content,
                config   = types.GenerateContentConfig(
                    system_instruction = _SYSTEM_PROMPT,
                    temperature        = temperature,
                    max_output_tokens  = max_tokens,
                ),
            )
            return response.text.strip() if response.text else None

        except Exception as exc:
            last_exc = exc
            err_str  = str(exc)
            is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str.upper()

            if is_rate_limit and delay is not None:
                logging.warning(
                    "[gemini] 429 Rate limit — %ds 후 재시도 (%d/%d)",
                    delay, attempt + 1, len(_RETRY_DELAYS),
                )
                time.sleep(delay)
                continue

            # 재시도 불가 에러이거나 재시도 소진
            logging.warning("[gemini] 호출 실패 (%s): %s", tier, exc)
            return None

    logging.warning("[gemini] 최대 재시도 소진: %s", last_exc)
    return None


# ─────────────────────────────────────────────────────────────────────
# Flash 태스크 헬퍼 함수들
# ─────────────────────────────────────────────────────────────────────

def summarize_news(symbol: str, news_text: str) -> Optional[str]:
    """종목 뉴스 한국어 요약 (Flash)."""
    prompt = (
        f"종목: {symbol}\n"
        f"뉴스:\n{news_text}\n\n"
        "다음 형식으로 요약하세요:\n"
        "• 핵심 촉매: (1줄)\n"
        "• 주요 리스크: (1줄)\n"
        "• 가격 영향: 강한상승/약한상승/중립/약한하락/강한하락\n"
        "• 보유 전략: (1줄)"
    )
    return call_gemini(prompt, task=GeminiTask.NEWS_SUMMARY, max_tokens=256)


def brief_market(market_summary: str) -> Optional[str]:
    """시장 동향 브리핑 (Flash)."""
    prompt = (
        f"현재 시장 데이터:\n{market_summary}\n\n"
        "오늘의 시장 동향을 3줄로 브리핑하고, "
        "단기 투자자에게 가장 중요한 포인트 1가지를 강조하세요."
    )
    return call_gemini(prompt, task=GeminiTask.MARKET_BRIEF, max_tokens=200)


def analyze_stock_quick(symbol: str, data_summary: str) -> Optional[str]:
    """종목 빠른 분석 (Flash)."""
    prompt = (
        f"종목 {symbol} 현황:\n{data_summary}\n\n"
        "기술적 관점에서 현재 포지션 유지/청산/추가매수 중 어떤 결정이 적절한지 "
        "근거와 함께 2줄로 답하세요."
    )
    return call_gemini(prompt, task=GeminiTask.STOCK_ANALYSIS, max_tokens=200)


# ─────────────────────────────────────────────────────────────────────
# Pro 태스크 헬퍼 함수들
# ─────────────────────────────────────────────────────────────────────

def analyze_large_loss(
    portfolio_summary: str,
    loss_pct: float,
    positions: str,
    recent_trades: str,
) -> Optional[str]:
    """
    큰 손실 발생 시 원인 분석 + 대응 전략 (Pro).
    일 손실 -3% 이상 또는 단일 포지션 -10% 이상 시 호출.
    """
    prompt = (
        f"⚠️ 큰 손실 발생 분석 요청\n\n"
        f"손실 규모: {loss_pct:+.1f}%\n"
        f"포트폴리오 현황:\n{portfolio_summary}\n"
        f"현재 포지션:\n{positions}\n"
        f"최근 거래:\n{recent_trades}\n\n"
        "다음을 분석하세요:\n"
        "1. 손실 원인 (시장 요인 vs 전략 오류)\n"
        "2. 즉각 취해야 할 행동 (청산/유지/헤지)\n"
        "3. 전략 파라미터 조정 권고 (구체적 수치)\n"
        "4. 동일 상황 재발 방지 방안"
    )
    context = f"손실율: {loss_pct:+.1f}% — Pro 모델 호출"
    return call_gemini(
        prompt, task=GeminiTask.LARGE_LOSS,
        temperature=0.1, max_tokens=600, context=context
    )


def recommend_rebalance(
    portfolio: str,
    market_regime: str,
    performance: str,
    current_allocations: str,
) -> Optional[str]:
    """
    포트폴리오 리밸런싱 권고 (Pro).
    레짐 변화 또는 특정 버킷 성과 편차가 클 때 호출.
    """
    prompt = (
        f"포트폴리오 리밸런싱 분석\n\n"
        f"현재 레짐: {market_regime}\n"
        f"버킷별 성과:\n{performance}\n"
        f"현재 배분:\n{current_allocations}\n"
        f"전체 포트폴리오:\n{portfolio}\n\n"
        "다음을 제안하세요:\n"
        "1. 현재 배분의 문제점\n"
        "2. 권장 배분 비율 (버킷1:가치주 / 버킷2:ETF / 버킷3:급등주)\n"
        "3. 구체적 리밸런싱 순서 (어떤 종목을 먼저 조정)\n"
        "4. 레짐 변화에 따른 전략 전환 시점"
    )
    return call_gemini(
        prompt, task=GeminiTask.PORTFOLIO_REBALANCE,
        temperature=0.15, max_tokens=700
    )


def analyze_regime_change(
    old_regime: str,
    new_regime: str,
    market_data: str,
    current_positions: str,
) -> Optional[str]:
    """
    시장 레짐 전환 심층 분석 (Pro).
    Bull→Bear, Correction→Panic 등 레짐 전환 감지 시 호출.
    """
    prompt = (
        f"🔄 시장 레짐 전환 분석\n\n"
        f"이전 레짐: {old_regime.upper()} → 현재 레짐: {new_regime.upper()}\n"
        f"시장 데이터:\n{market_data}\n"
        f"현재 보유 포지션:\n{current_positions}\n\n"
        "분석해주세요:\n"
        "1. 이 레짐 전환이 일시적인지 추세 전환인지 판단\n"
        "2. 각 버킷(가치주/ETF/급등주)별 즉각 대응 전략\n"
        "3. 헤지 포지션 추가 필요 여부\n"
        "4. 다음 레짐 전환 시 선행 지표"
    )
    return call_gemini(
        prompt, task=GeminiTask.REGIME_CHANGE,
        temperature=0.1, max_tokens=600
    )


def resolve_signal_conflict(
    symbol: str,
    bullish_signals: list,
    bearish_signals: list,
    position_info: str,
) -> Optional[str]:
    """
    지표 충돌 상황 최종 판단 (Pro).
    매수/매도 신호가 혼재할 때 최종 의사결정에 활용.
    """
    bulls = "\n".join(f"  + {s}" for s in bullish_signals)
    bears = "\n".join(f"  - {s}" for s in bearish_signals)
    prompt = (
        f"종목 {symbol} 신호 충돌 판단\n\n"
        f"매수 신호:\n{bulls}\n\n"
        f"매도 신호:\n{bears}\n\n"
        f"현재 포지션: {position_info}\n\n"
        "신호 강도와 신뢰도를 평가해서:\n"
        "1. 최종 판단: 매수 유지/청산/관망 중 하나\n"
        "2. 핵심 근거 (가장 중요한 신호 2가지)\n"
        "3. 이 판단을 번복해야 할 조건"
    )
    return call_gemini(
        prompt, task=GeminiTask.CONFLICT_SIGNALS,
        temperature=0.1, max_tokens=300
    )


# ─────────────────────────────────────────────────────────────────────
# 텔레그램 /ask 명령 전용 단순 인터페이스
# ─────────────────────────────────────────────────────────────────────

def ask_gpt(prompt: str, max_tokens: int = 256) -> str:
    """
    자유 형식 질문 → Gemini Flash 응답 (telegram_bot.py /ask 명령 전용).
    실패 시 오류 메시지 문자열 반환 (예외 전파 없음).
    """
    result = call_gemini(prompt, task=GeminiTask.STOCK_ANALYSIS, max_tokens=max_tokens)
    return result or "응답 없음 (Gemini 오류)"


def suggest_strategy_update(
    strategy_name: str,
    recent_performance: str,
    current_params: str,
    market_context: str,
) -> Optional[str]:
    """
    전략 파라미터 조정 권고 (Pro).
    전략 성과가 기준 이하일 때 또는 시장 환경 변화 시 호출.
    """
    prompt = (
        f"전략 최적화 권고 요청\n\n"
        f"전략: {strategy_name}\n"
        f"최근 성과:\n{recent_performance}\n"
        f"현재 파라미터:\n{current_params}\n"
        f"시장 환경:\n{market_context}\n\n"
        "제안하세요:\n"
        "1. 현재 파라미터의 문제점\n"
        "2. 구체적 파라미터 수정값 (수치 포함)\n"
        "3. 수정 후 예상 성과 개선 범위\n"
        "4. 검증 방법 (백테스트 기간 등)"
    )
    return call_gemini(
        prompt, task=GeminiTask.STRATEGY_CHANGE,
        temperature=0.15, max_tokens=500
    )
