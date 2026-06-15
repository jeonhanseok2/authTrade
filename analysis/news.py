# analysis/news.py
"""
뉴스 분석 모듈.
- Alpaca News API로 뉴스 수집
- 키워드 빈도 분석 (어떤 테마가 시장을 지배하는지 파악)
- GPT를 활용한 종목별 뉴스 요약 및 감성(sentiment) 분석
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# 뉴스 감성 분류를 위한 키워드 사전
_BULLISH_KEYWORDS = [
    "beat", "exceed", "record", "upgrade", "approval", "partnership",
    "buyback", "dividend", "guidance raise", "contract", "award",
    "acquisition", "fda", "breakthrough", "positive", "growth",
    "expansion", "rally", "outperform", "strong", "surge",
    "실적 호조", "상향", "매수", "급등",
]

_BEARISH_KEYWORDS = [
    "miss", "below", "downgrade", "loss", "recall", "lawsuit",
    "investigation", "fraud", "warning", "cut", "layoff",
    "guidance cut", "downside", "decline", "weak", "drop",
    "missed expectations", "underperform",
    "실적 부진", "하향", "매도", "급락", "조사",
]

# 섹터/테마 키워드 맵핑
_THEME_KEYWORDS = {
    "AI/반도체":    ["ai", "artificial intelligence", "semiconductor", "chip", "gpu", "nvidia"],
    "금리/채권":    ["fed", "interest rate", "inflation", "cpi", "yield", "treasury", "hawkish", "dovish"],
    "에너지":       ["oil", "gas", "energy", "opec", "crude", "barrel"],
    "헬스케어/바이오": ["fda", "clinical trial", "drug", "biotech", "approval", "healthcare"],
    "테크":         ["cloud", "saas", "software", "tech", "cybersecurity", "data"],
    "지정학":       ["war", "sanctions", "geopolitical", "china", "taiwan", "russia"],
    "실적시즌":     ["earnings", "revenue", "eps", "guidance", "outlook", "quarterly"],
}


def _get_news_client():
    """Alpaca NewsClient 지연 초기화."""
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        return None
    try:
        from alpaca.data.historical import NewsClient  # type: ignore
        return NewsClient(api_key=api_key, secret_key=secret)
    except Exception as exc:
        logging.debug("[news] NewsClient 초기화 실패: %s", exc)
    return None


def fetch_articles(symbol: Optional[str] = None, hours: int = 24, limit: int = 20) -> List[Dict]:
    """
    Alpaca News API로 뉴스 수집.
    symbol=None 이면 시장 전체 뉴스 (SPY 기준).
    반환: [{'headline', 'summary', 'source', 'published_at', 'symbols'}]
    """
    client = _get_news_client()
    if client is None:
        return []
    try:
        from alpaca.data.requests import NewsRequest  # type: ignore
        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        params = dict(start=start, end=end, limit=limit, exclude_contentless=True)
        if symbol:
            params["symbols"] = symbol

        news_set  = client.get_news(NewsRequest(**params))
        articles  = []
        data_dict = getattr(news_set, "data", {}) or {}

        # symbol이 있으면 해당 종목 뉴스만, 없으면 전체 모아서
        all_articles = []
        for items in data_dict.values():
            all_articles.extend(items)

        for a in all_articles[:limit]:
            articles.append({
                "headline":     getattr(a, "headline",     "") or "",
                "summary":      getattr(a, "summary",      "") or "",
                "source":       getattr(a, "source",       "") or "",
                "published_at": str(getattr(a, "created_at", "") or ""),
                "symbols":      list(getattr(a, "symbols", []) or []),
            })
        return articles
    except Exception as exc:
        logging.debug("[news] 뉴스 수집 실패: %s", exc)
    return []


def analyze_sentiment(articles: List[Dict]) -> Dict:
    """
    기사 리스트에서 감성 분석 수행.
    반환: {'score': float(-1~1), 'label': 'bullish'|'bearish'|'neutral',
           'bullish_count': int, 'bearish_count': int}
    """
    bullish_count = 0
    bearish_count = 0

    for art in articles:
        text = (art.get("headline", "") + " " + art.get("summary", "")).lower()
        bulls = sum(1 for kw in _BULLISH_KEYWORDS if kw in text)
        bears = sum(1 for kw in _BEARISH_KEYWORDS if kw in text)
        bullish_count += bulls
        bearish_count += bears

    total = bullish_count + bearish_count
    if total == 0:
        return {"score": 0.0, "label": "neutral", "bullish_count": 0, "bearish_count": 0}

    score = (bullish_count - bearish_count) / total  # -1 ~ +1
    label = "bullish" if score > 0.2 else ("bearish" if score < -0.2 else "neutral")
    return {
        "score":         round(score, 3),
        "label":         label,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
    }


def extract_trending_themes(articles: List[Dict], top_n: int = 5) -> List[str]:
    """
    현재 시장에서 가장 많이 언급되는 테마 키워드 반환.
    예) ['AI/반도체', '금리/채권', '실적시즌']
    """
    theme_counts: Counter = Counter()
    for art in articles:
        text = (art.get("headline", "") + " " + art.get("summary", "")).lower()
        for theme, keywords in _THEME_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                theme_counts[theme] += 1
    return [theme for theme, _ in theme_counts.most_common(top_n)]


def extract_keyword_frequency(articles: List[Dict], top_n: int = 10) -> List[tuple]:
    """
    뉴스에서 자주 등장하는 단어 빈도 반환.
    반환: [('keyword', count), ...]
    """
    stopwords = {
        "the","a","an","in","on","at","to","for","of","and","or","is","are",
        "was","were","it","its","as","by","be","has","had","that","this","with",
        "from","their","they","have","will","can","been","said","would","also",
    }
    word_counts: Counter = Counter()
    for art in articles:
        text  = (art.get("headline", "") + " " + art.get("summary", "")).lower()
        words = [w.strip(".,;:!?\"'()") for w in text.split()]
        for w in words:
            if len(w) > 3 and w not in stopwords:
                word_counts[w] += 1
    return word_counts.most_common(top_n)


def gpt_summarize_news(symbol: str, articles: List[Dict]) -> Optional[str]:
    """
    GPT로 종목 뉴스 한국어 요약.
    촉매, 리스크, 가격 영향 예측 포함.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not articles:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=15.0)
        news_text = "\n".join(
            f"- {a['headline']}. {a['summary'][:200]}" for a in articles[:5]
        )
        prompt = (
            f"종목: {symbol}\n"
            f"뉴스:\n{news_text}\n\n"
            "위 뉴스를 한국어로 요약하세요:\n"
            "1. 핵심 촉매 (주가 상승 요인)\n"
            "2. 주요 리스크\n"
            "3. 단기 가격 영향 예측 (강한 상승/약한 상승/중립/하락)\n"
            "각 항목 1줄 이내로 간결하게."
        )
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logging.debug("[news] GPT 요약 실패: %s", exc)
    return None


def format_news_report(symbol: str, articles: List[Dict], sentiment: Dict) -> str:
    """텔레그램 전송용 뉴스 분석 텍스트."""
    label_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "📊"}.get(sentiment["label"], "📊")
    lines = [
        f"📰 <b>{symbol} 뉴스 분석</b>",
        f"감성: {label_emoji} {sentiment['label'].upper()} (점수: {sentiment['score']:+.2f})",
        f"긍정 신호: {sentiment['bullish_count']}건 / 부정 신호: {sentiment['bearish_count']}건",
        "",
        "<b>최신 헤드라인:</b>",
    ]
    for a in articles[:3]:
        lines.append(f"• {a['headline'][:80]}")
    return "\n".join(lines)
