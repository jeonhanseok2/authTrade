# ai/gpt_helper.py
"""
(구) OpenAI GPT 헬퍼 → Gemini Flash로 교체.
기존 호출 코드의 인터페이스를 유지하여 하위 호환성 보장.
"""
from typing import Optional

from ai.gemini_helper import summarize_news as _gemini_summarize


def summarize_news(symbol: str, news_text: str) -> Optional[str]:
    """뉴스 요약 — Gemini 1.5 Flash 사용."""
    return _gemini_summarize(symbol, news_text)
