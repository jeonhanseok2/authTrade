# data/news_universe.py
"""
Alpaca News API 기반 실시간 카탈리스트 종목 발굴.

B3 Gap&Go 전략의 핵심 문제: 정적 watchlist는 오늘의 카탈리스트 종목을 모른다.
이 모듈은 Alpaca News API로 최근 N시간 뉴스를 수집하고, 카탈리스트 키워드 필터로
오늘 급등 가능성이 있는 종목을 실시간 발굴한다.

흐름:
  1. Alpaca News API — 심볼 필터 없이 전체 뉴스 수집 (최근 4시간)
  2. 약세 키워드로 먼저 제외 (dilution, bankruptcy 등)
  3. 카탈리스트 키워드 매칭 (FDA, earnings beat, upgrade 등)
  4. 종목별 카탈리스트 빈도 집계 → 상위 종목 반환

사용:
    from data.news_universe import fetch_catalyst_symbols
    syms = fetch_catalyst_symbols(hours=4)  # 최근 4시간 카탈리스트 종목
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import List

# ── 카탈리스트 키워드 (뉴스에 있으면 B3 급등 후보) ───────────────────────
_CATALYST_KW = [
    # 바이오/의약품
    "fda", "approval", "approved", "clearance", "breakthrough",
    "phase 3", "phase iii", "clinical trial", "nda", "bla",
    # 실적
    "earnings beat", "beat estimates", "beat expectations",
    "revenue beat", "profit beat", "eps beat",
    "raises guidance", "guidance raise", "raised guidance",
    "record revenue", "record earnings",
    # 계약/파트너십
    "partnership", "contract", "agreement", "deal",
    "government contract", "military contract", "department of defense",
    # M&A
    "acquisition", "merger", "buyout", "takeover",
    # 애널리스트
    "upgrade", "price target raised", "outperform", "buy rating", "strong buy",
    # 숏스퀴즈
    "short squeeze",
]

# ── 약세 필터 (이 키워드 있으면 즉시 제외) ───────────────────────────────
_NEGATIVE_KW = [
    "secondary offering", "dilution", "disappoints", "misses estimates",
    "guidance cut", "reduces guidance", "lowers guidance",
    "sec investigation", "fraud", "delisting", "bankruptcy",
    "chapter 11", "class action", "lawsuit", "going concern",
    "restatement", "accounting fraud",
]

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}
_CACHE_TTL_SEC = 300   # 5분 캐시


def fetch_catalyst_symbols(hours: int = 4, max_symbols: int = 100) -> List[str]:
    """
    최근 N시간 카탈리스트 종목 목록 반환.

    소스 우선순위 (모두 무료):
      1. Alpaca News API     — 실시간 뉴스 (Alpaca 계정 필요, 무료 플랜 포함)
      2. SEC EDGAR 8-K       — 공식 공시 (API 키 불필요, 완전 무료)
      3. Finviz 갭 스크리너  — 장중 갭업 종목 (HTML 파싱, 9:30 ET 이후)
      전체 취합 후 중복 제거하여 반환.

    Args:
        hours:       조회 범위 (기본 4시간)
        max_symbols: 최대 반환 종목 수

    Returns:
        카탈리스트 빈도 순으로 정렬된 종목 리스트.
    """
    with _CACHE_LOCK:
        cached = _CACHE.get("data")
        if cached and _time.monotonic() - cached["ts"] < _CACHE_TTL_SEC:
            return cached["symbols"][:max_symbols]

    # 1. Alpaca News API
    alpaca_syms = _fetch_from_alpaca(hours, max_symbols)

    # 2. SEC EDGAR 8-K (완전 무료)
    edgar_syms: List[str] = []
    try:
        from data.edgar_catalyst import get_catalyst_symbols as _edgar_syms
        edgar_syms = _edgar_syms(hours=hours)
    except Exception as exc:
        logging.debug("[news_universe] EDGAR 실패: %s", exc)

    # 3. Finviz 갭 스크리너 (장중 사용, 9:30 ET 이후)
    finviz_syms: List[str] = []
    try:
        import zoneinfo
        from datetime import datetime
        now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        # 장 개시(9:35) 이후에만 Finviz 사용 (프리마켓은 데이터 없음)
        if now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 35):
            from data.finviz_scraper import get_symbols as _finviz_syms
            finviz_syms = _finviz_syms(min_change_pct=10.0)
    except Exception as exc:
        logging.debug("[news_universe] Finviz 실패: %s", exc)

    # 병합 — Alpaca 우선, EDGAR/Finviz로 보강
    seen: set = set()
    symbols: List[str] = []
    for sym in alpaca_syms + edgar_syms + finviz_syms:
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
        if len(symbols) >= max_symbols:
            break

    if symbols:
        logging.info(
            "[news_universe] 카탈리스트 종목 %d개 (alpaca=%d edgar=%d finviz=%d)",
            len(symbols), len(alpaca_syms), len(edgar_syms), len(finviz_syms),
        )

    with _CACHE_LOCK:
        _CACHE["data"] = {"symbols": symbols, "ts": _time.monotonic()}

    return symbols[:max_symbols]


def _fetch_from_alpaca(hours: int, max_symbols: int) -> List[str]:
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret  = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret:
        logging.debug("[news_universe] API 키 없음 — 스킵")
        return []

    try:
        from alpaca.data.historical import NewsClient    # type: ignore
        from alpaca.data.requests   import NewsRequest   # type: ignore

        client = NewsClient(api_key=api_key, secret_key=secret)
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)

        req = NewsRequest(
            start=start,
            end=end,
            limit=200,
            exclude_contentless=True,
        )
        resp = client.get_news(req)

        # resp.news: List[News] — 심볼 필터 없는 전체 뉴스
        articles = getattr(resp, "news", []) or []

        found: dict[str, int] = {}   # symbol → catalyst 빈도

        for article in articles:
            headline = (getattr(article, "headline", "") or "").lower()
            summary  = (getattr(article, "summary",  "") or "").lower()
            text     = f"{headline} {summary}"

            # 약세 필터 — 해당하면 이 기사의 종목 전체 제외
            if any(kw in text for kw in _NEGATIVE_KW):
                continue

            # 카탈리스트 빈도 집계
            catalyst_cnt = sum(1 for kw in _CATALYST_KW if kw in text)
            if catalyst_cnt == 0:
                continue

            # 관련 종목 심볼 추출
            syms = getattr(article, "symbols", []) or []
            for sym in syms:
                if not sym or len(sym) > 5:   # 티커는 최대 5자 (GOOGL)
                    continue
                sym = sym.upper()
                found[sym] = found.get(sym, 0) + catalyst_cnt

        # 카탈리스트 빈도 높은 순 정렬
        sorted_syms = sorted(found, key=lambda s: found[s], reverse=True)

        if sorted_syms:
            logging.info(
                "[news_universe] 카탈리스트 종목 %d개 발굴 (최근 %dh): %s",
                len(sorted_syms), hours,
                [f"{s}({found[s]})" for s in sorted_syms[:8]],
            )
        else:
            logging.debug("[news_universe] 카탈리스트 종목 없음 (최근 %dh)", hours)

        return sorted_syms[:max_symbols]

    except Exception as exc:
        logging.debug("[news_universe] Alpaca 뉴스 조회 실패: %s", exc)
        return []
