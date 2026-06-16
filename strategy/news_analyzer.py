"""
strategy/news_analyzer.py — 뉴스 심리 분석기 + 신뢰도 보정

MarketRegimeAnalyzer에 연동되어 ConfidenceScore를 뉴스 점수로 보정합니다.

최종 신뢰도 = (차트 점수 × 0.7) + (뉴스 점수 × 0.3)

기능:
  1. yfinance 또는 Alpaca News API로 최근 1시간 뉴스 수집
  2. Gemini Flash로 긍정/부정 점수(-1.0 ~ 1.0) 산출
  3. 긴급 키워드 감지 → 즉시 블랙리스트 + 텔레그램 경고
  4. 모든 뉴스 점수를 market_log DB에 기록
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import storage.db_manager as dbm

# ── 긴급 차단 키워드 ──────────────────────────────────────────────────
# 감지 시 즉시 블랙리스트 등록 + 텔레그램 경고

_BLOCK_KEYWORDS_KR = [
    "유상증자", "소송", "실적 악화", "상장폐지", "감사의견", "부정 감사",
    "회계 부정", "횡령", "배임", "자본잠식", "영업 정지", "검찰",
]
_BLOCK_KEYWORDS_EN = [
    "secondary offering", "dilution", "lawsuit", "class action",
    "sec investigation", "fraud", "delisting", "going concern",
    "earnings miss", "guidance cut", "chapter 11", "bankruptcy",
    "restatement", "accounting fraud",
]
_BLOCK_KEYWORDS = _BLOCK_KEYWORDS_KR + _BLOCK_KEYWORDS_EN

# ── 뉴스 캐시 TTL ─────────────────────────────────────────────────────
_NEWS_CACHE_SEC = 300   # 5분
_SCORE_CACHE_SEC = 600  # 10분


@dataclass
class NewsScore:
    """뉴스 심리 분석 결과."""
    symbol:        str
    raw_score:     float         # Gemini 원점수 (-1.0 ~ 1.0)
    normalized:    int           # 0~30점으로 변환 (신뢰도 보정용)
    article_count: int = 0
    headlines:     List[str] = field(default_factory=list)
    blocked:       bool = False
    block_reason:  str = ""

    @property
    def weight(self) -> float:
        """최종 신뢰도 계산용 가중치 (0.0 ~ 1.0)."""
        return (self.normalized / 30.0) if not self.blocked else 0.0

    def summary(self) -> str:
        emoji = "🟢" if self.raw_score > 0.2 else "🔴" if self.raw_score < -0.2 else "🟡"
        return (f"{emoji} [{self.symbol}] 뉴스 심리: {self.raw_score:+.2f} "
                f"({self.normalized}pt/{self.article_count}건)")


class NewsAnalyzer:
    """
    뉴스 심리 분석기.

    ConfidenceScanner와 연동하여 차트 점수에 뉴스 보정을 적용합니다.

    사용 예:
        analyzer = NewsAnalyzer(notify=orch._notify)
        chart_score = conf_scanner.score(symbol, df)  # 0~100
        final_score = analyzer.blend(symbol, chart_score.total)
        if final_score < 70:
            return  # 진입 차단
    """

    def __init__(
        self,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._notify = notify or (lambda _: None)
        self._cache:  Dict[str, Tuple[float, NewsScore]] = {}   # symbol → (ts, score)
        self._blocked_today: Dict[str, str] = {}                # symbol → reason

    # ── 뉴스 수집 ─────────────────────────────────────────────────────

    def _fetch_headlines(self, symbol: str, hours: int = 1) -> List[str]:
        """
        최근 N시간 뉴스 제목 수집.
        Alpaca News API 우선, 실패 시 yfinance fallback.
        """
        headlines: List[str] = []

        # 1순위: Alpaca News API
        try:
            import os
            api_key = os.getenv("ALPACA_API_KEY")
            secret  = os.getenv("ALPACA_SECRET_KEY")
            if api_key and secret:
                from alpaca.data.historical import NewsClient   # type: ignore
                from alpaca.data.requests   import NewsRequest  # type: ignore
                client = NewsClient(api_key=api_key, secret_key=secret)
                start  = datetime.now(timezone.utc) - timedelta(hours=hours)
                req    = NewsRequest(symbols=[symbol], start=start, limit=20)
                resp   = client.get_news(req)
                for article in (resp.news or []):
                    title = getattr(article, "headline", "") or getattr(article, "title", "")
                    if title:
                        headlines.append(title)
                logging.debug("[NewsAnalyzer] Alpaca 뉴스 %d건: %s", len(headlines), symbol)
                return headlines
        except Exception as exc:
            logging.debug("[NewsAnalyzer] Alpaca 뉴스 실패: %s — yfinance fallback", exc)

        # 2순위: yfinance
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            news   = ticker.news or []
            now_ts = time.time()
            cutoff = now_ts - hours * 3600
            for item in news:
                if item.get("providerPublishTime", 0) >= cutoff:
                    title = item.get("title", "")
                    if title:
                        headlines.append(title)
            logging.debug("[NewsAnalyzer] yfinance 뉴스 %d건: %s", len(headlines), symbol)
        except Exception as exc:
            logging.debug("[NewsAnalyzer] yfinance 뉴스 실패: %s", exc)

        return headlines

    # ── 긴급 키워드 차단 ─────────────────────────────────────────────

    def _check_block_keywords(self, symbol: str, headlines: List[str]) -> Tuple[bool, str]:
        """
        긴급 차단 키워드 감지.

        Returns:
            (차단여부, 감지된 키워드)
        """
        combined = " ".join(headlines).lower()
        for kw in _BLOCK_KEYWORDS:
            if kw.lower() in combined:
                return True, kw
        return False, ""

    # ── Gemini 심리 분석 ──────────────────────────────────────────────

    def _gemini_sentiment(self, symbol: str, headlines: List[str]) -> float:
        """
        Gemini Flash로 뉴스 제목의 긍정/부정 점수 산출.

        Returns:
            float: -1.0 (매우 부정) ~ 1.0 (매우 긍정)
        """
        if not headlines:
            return 0.0

        prompt = (
            f"다음은 주식 {symbol}의 최근 뉴스 제목들입니다.\n"
            f"투자 관점에서 전반적인 감성을 -1.0(매우 부정)에서 1.0(매우 긍정) 사이의 숫자 하나로만 답하세요.\n"
            f"설명 없이 숫자만 출력하세요.\n\n"
            + "\n".join(f"- {h}" for h in headlines[:10])
        )

        try:
            from ai.gemini_helper import call_gemini, GeminiTask
            raw = call_gemini(prompt, task=GeminiTask.STOCK_ANALYSIS, max_tokens=10)
            if raw:
                score = float(raw.strip().split()[0])
                return max(-1.0, min(1.0, score))
        except Exception as exc:
            logging.debug("[NewsAnalyzer] Gemini 호출 실패: %s — 키워드 폴백", exc)

        # Gemini 실패 시 키워드 기반 폴백
        return self._keyword_sentiment(headlines)

    def _keyword_sentiment(self, headlines: List[str]) -> float:
        """키워드 기반 감성 점수 (Gemini 불가 시 폴백)."""
        from analysis.news import _BULLISH_KEYWORDS, _BEARISH_KEYWORDS
        text   = " ".join(headlines).lower()
        bull   = sum(1 for kw in _BULLISH_KEYWORDS if kw in text)
        bear   = sum(1 for kw in _BEARISH_KEYWORDS if kw in text)
        total  = bull + bear
        if total == 0:
            return 0.0
        return (bull - bear) / total

    # ── 점수 정규화 ───────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw: float) -> int:
        """
        raw (-1.0 ~ 1.0) → 0~30점 변환.

        -1.0 → 0점, 0.0 → 15점, +1.0 → 30점
        """
        return int((raw + 1.0) / 2.0 * 30)

    # ── 메인 분석 함수 ────────────────────────────────────────────────

    def analyze(self, symbol: str, hours: int = 1) -> NewsScore:
        """
        종목 뉴스 수집 → 긴급 차단 → Gemini 심리 점수 산출 → DB 기록.

        캐시 TTL 내 동일 종목 재호출 시 캐시 반환.

        Returns:
            NewsScore
        """
        # 캐시 확인
        cached = self._cache.get(symbol)
        if cached:
            ts, score = cached
            if time.monotonic() - ts < _SCORE_CACHE_SEC:
                return score

        # 이미 차단된 종목
        if symbol in self._blocked_today:
            return NewsScore(
                symbol=symbol, raw_score=-1.0, normalized=0,
                blocked=True, block_reason=self._blocked_today[symbol],
            )

        headlines = self._fetch_headlines(symbol, hours=hours)

        # 긴급 키워드 차단 체크
        blocked, block_kw = self._check_block_keywords(symbol, headlines)
        if blocked:
            self._blocked_today[symbol] = block_kw
            result = NewsScore(
                symbol=symbol, raw_score=-1.0, normalized=0,
                article_count=len(headlines),
                headlines=headlines[:5],
                blocked=True, block_reason=block_kw,
            )
            self._cache[symbol] = (time.monotonic(), result)

            warn_msg = (
                f"🚨 [뉴스 차단] {symbol}\n"
                f"감지 키워드: {block_kw}\n"
                f"→ 블랙리스트 즉시 등록"
            )
            self._notify(warn_msg)
            logging.warning("[NewsAnalyzer] 긴급 차단: %s (%s)", symbol, block_kw)

            # DB 기록
            self._log_to_db(symbol, -1.0, len(headlines), block_kw)
            return result

        # 뉴스 없으면 중립
        if not headlines:
            result = NewsScore(symbol=symbol, raw_score=0.0, normalized=15, article_count=0)
            self._cache[symbol] = (time.monotonic(), result)
            return result

        # Gemini 심리 분석
        raw_score  = self._gemini_sentiment(symbol, headlines)
        normalized = self._normalize(raw_score)

        result = NewsScore(
            symbol=symbol,
            raw_score=raw_score,
            normalized=normalized,
            article_count=len(headlines),
            headlines=headlines[:5],
        )
        self._cache[symbol] = (time.monotonic(), result)
        logging.info("[NewsAnalyzer] %s", result.summary())

        # DB 기록
        self._log_to_db(symbol, raw_score, len(headlines), "")
        return result

    def _log_to_db(
        self, symbol: str, score: float, count: int, note: str
    ) -> None:
        """뉴스 점수를 market_log에 기록 (수익률-뉴스 상관분석용)."""
        try:
            from datetime import date as _date
            dbm.save_market_log(
                date=str(_date.today()),
                nasdaq_ma20=None,
                regime=f"NEWS:{symbol}:{score:+.2f}:{count}:{note}",
                scanner_score=None,
            )
        except Exception as exc:
            logging.debug("[NewsAnalyzer] DB 기록 실패: %s", exc)

    # ── 신뢰도 통합 (차트 × 0.7 + 뉴스 × 0.3) ───────────────────────

    def blend(self, symbol: str, chart_score: int, hours: int = 1) -> int:
        """
        최종 신뢰도 = 차트 점수 × 0.7 + 뉴스 점수(0~100 환산) × 0.3

        Args:
            symbol:      종목 코드
            chart_score: ConfidenceScanner의 차트 점수 (0~100)
            hours:       뉴스 수집 범위 (기본 1시간)

        Returns:
            최종 신뢰도 점수 (0~100)
        """
        news = self.analyze(symbol, hours=hours)

        if news.blocked:
            return 0  # 긴급 차단 → 즉시 0점

        # 뉴스 점수를 0~100 스케일로 환산
        news_100 = news.normalized / 30.0 * 100

        final = int(chart_score * 0.7 + news_100 * 0.3)
        logging.info(
            "[NewsAnalyzer] %s 최종 신뢰도: %d점 (차트 %d × 0.7 + 뉴스 %.0f × 0.3)",
            symbol, final, chart_score, news_100,
        )
        return final

    def is_blocked(self, symbol: str) -> bool:
        return symbol in self._blocked_today
