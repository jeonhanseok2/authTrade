# news/check.py
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

_news_client = None


def _get_news_client():
    global _news_client
    if _news_client is not None:
        return _news_client
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        return None
    try:
        from alpaca.data.historical import NewsClient  # type: ignore[attr-defined]
        _news_client = NewsClient(api_key=api_key, secret_key=secret)
    except Exception as exc:
        logging.debug("[news] NewsClient init failed: %s", exc)
    return _news_client


def fetch_recent_headlines(symbol: str, hours: int = 24) -> List[str]:
    """Alpaca News API로 최근 뉴스 헤드라인+요약 반환. 실패 시 빈 리스트."""
    client = _get_news_client()
    if client is None:
        return []
    try:
        from alpaca.data.requests import NewsRequest  # type: ignore[attr-defined]
        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        req = NewsRequest(symbols=symbol, start=start, end=end, limit=10, exclude_contentless=True)
        news_set = client.get_news(req)
        texts: List[str] = []
        articles = getattr(news_set, "data", {})
        for article in (articles.get(symbol) or []):
            headline = getattr(article, "headline", "") or ""
            summary  = getattr(article, "summary",  "") or ""
            texts.append(f"{headline}. {summary}".strip(". "))
        return texts
    except Exception as exc:
        logging.debug("[news] fetch failed for %s: %s", symbol, exc)
        return []


def get_news_text_for_gpt(symbol: str, hours: int = 24) -> str:
    """GPT 요약용 뉴스 텍스트 (최대 5건 합쳐서 반환)."""
    headlines = fetch_recent_headlines(symbol, hours=hours)
    return "\n".join(headlines[:5]) if headlines else ""


def is_positive_news(symbol: str, keywords: List[str]) -> bool:
    """최근 뉴스에서 긍정 키워드 하나라도 매칭되면 True."""
    headlines = fetch_recent_headlines(symbol)
    if not headlines:
        return False
    kws = [k.lower() for k in keywords]
    for text in headlines:
        tl = text.lower()
        if any(k in tl for k in kws):
            return True
    return False
