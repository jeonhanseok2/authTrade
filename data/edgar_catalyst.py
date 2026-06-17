# data/edgar_catalyst.py
"""
SEC EDGAR 8-K 공시 기반 카탈리스트 피드 — 완전 무료, API 키 불필요.

SEC EDGAR는 공식 REST API를 무료 제공합니다:
  https://efts.sec.gov/LATEST/search-index?forms=8-K&dateRange=custom&...

8-K = 중요 사건 공시 (수시 보고서):
  Item 1.01 — 중요 계약 체결 (파트너십/계약)
  Item 1.02 — 중요 계약 종료
  Item 2.01 — 자산 취득/처분 (M&A)
  Item 5.02 — 경영진 교체
  Item 7.01 — Regulation FD 공시 (실적 발표 포함)
  Item 8.01 — 기타 사건 (FDA 승인, 특허, 계약 등)

사용:
    from data.edgar_catalyst import fetch_catalyst_8k
    events = fetch_catalyst_8k(hours=4)
    # [{"symbol": "SOUN", "company": "SoundHound AI", "type": "contract", ...}, ...]

참고:
  - EDGAR API 공식 Rate Limit: 10 req/sec, User-Agent 헤더 필수
  - CIK → Ticker 매핑: EDGAR company_tickers.json (매일 업데이트)
  - 공시 파일 → 실제 내용까지는 추가 파싱 필요 (현재는 제목 기반 분류)
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

_EDGAR_BASE   = "https://efts.sec.gov"
_TICKER_URL   = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT   = "authTrade/1.0 hs.jeon@gschargev.co.kr"   # EDGAR 정책: 이메일 포함 필수

_CACHE_LOCK   = threading.Lock()
_CACHE: dict  = {}
_TICKER_CACHE: dict = {}     # CIK → ticker 매핑 캐시
_TICKER_TS:   float = 0.0

# 8-K item 번호 → 카탈리스트 유형 매핑
_ITEM_TYPE = {
    "1.01": "contract",     # 중요 계약
    "2.01": "acquisition",  # M&A/자산 취득
    "7.01": "disclosure",   # Regulation FD (실적 전망 포함)
    "8.01": "event",        # 기타 사건 (FDA 등)
    "1.02": "termination",  # 계약 종료 (부정적)
    "8.02": "unaudited",    # 감사되지 않은 재무제표
}

# 제목 키워드 → 카탈리스트 유형 재분류
_TITLE_CATALYST = [
    (["fda", "approval", "clearance", "breakthrough"],          "fda"),
    (["merger", "acquisition", "buyout", "takeover"],           "ma"),
    (["contract", "agreement", "partnership", "award"],         "contract"),
    (["earnings", "revenue", "profit", "results", "guidance"],  "earnings"),
    (["upgrade", "downgrade", "price target"],                  "analyst"),
]

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})


def _load_ticker_map() -> dict[str, str]:
    """CIK → ticker 매핑 로드 (24시간 캐시)."""
    global _TICKER_TS
    with _CACHE_LOCK:
        if _TICKER_CACHE and _time.monotonic() - _TICKER_TS < 86400:
            return _TICKER_CACHE
        try:
            r = _SESSION.get(_TICKER_URL, timeout=10)
            r.raise_for_status()
            raw = r.json()
            # 형태: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
            mapping = {
                str(v["cik_str"]).zfill(10): v["ticker"].upper()
                for v in raw.values()
                if v.get("cik_str") and v.get("ticker")
            }
            _TICKER_CACHE.update(mapping)
            _TICKER_TS = _time.monotonic()
            logging.debug("[edgar] ticker 매핑 %d개 로드", len(mapping))
        except Exception as exc:
            logging.debug("[edgar] ticker 매핑 실패: %s", exc)
    return _TICKER_CACHE


def _classify_title(title: str) -> str:
    """8-K 제목에서 카탈리스트 유형 결정."""
    tl = title.lower()
    for keywords, cat_type in _TITLE_CATALYST:
        if any(kw in tl for kw in keywords):
            return cat_type
    return "event"


def _is_negative(title: str) -> bool:
    """부정적 공시 여부 (제외 대상)."""
    tl = title.lower()
    negative = [
        "termination", "resignation", "departure", "bankruptcy",
        "chapter 11", "going concern", "delisting", "restatement",
        "sec investigation", "fraud", "class action",
    ]
    return any(kw in tl for kw in negative)


def fetch_catalyst_8k(hours: int = 4, max_results: int = 50) -> List[dict]:
    """
    SEC EDGAR에서 최근 N시간 8-K 공시를 조회하고 카탈리스트 이벤트를 반환.

    Args:
        hours:       조회 범위 (기본 4시간)
        max_results: 최대 반환 개수

    Returns:
        List of dict:
            symbol:   티커 (없으면 "")
            company:  회사명
            type:     카탈리스트 유형 (fda/ma/contract/earnings/analyst/event)
            title:    8-K 제목
            filed_at: 공시 시각 (UTC ISO 문자열)
            url:      EDGAR 공시 URL
    """
    cache_key = f"8k_{hours}"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and _time.monotonic() - cached["ts"] < 300:  # 5분 캐시
            return cached["data"][:max_results]

    results = _fetch_edgar(hours, max_results)

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"data": results, "ts": _time.monotonic()}

    return results[:max_results]


def _fetch_edgar(hours: int, max_results: int) -> List[dict]:
    try:
        ticker_map = _load_ticker_map()

        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        url = (
            f"{_EDGAR_BASE}/LATEST/search-index?"
            f"forms=8-K"
            f"&dateRange=custom"
            f"&startdt={start.strftime('%Y-%m-%d')}"
            f"&enddt={end.strftime('%Y-%m-%d')}"
            f"&hits.hits.total.value=true"
            f"&hits.hits._source.period_of_report=true"
        )

        r = _SESSION.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        hits = data.get("hits", {}).get("hits", [])
        results: List[dict] = []

        for hit in hits:
            src    = hit.get("_source", {})
            title  = src.get("display_names", [{}])[0].get("name", "") if src.get("display_names") else ""
            form_title = src.get("file_date", "")

            # 회사명 + CIK
            entity   = src.get("entity_name", "") or ""
            cik_raw  = str(src.get("file_num", "") or "").split("-")[0] if src.get("file_num") else ""
            cik      = src.get("cik", "") or ""
            ticker   = ticker_map.get(str(cik).zfill(10), "")

            # 8-K 공시 제목 (items 필드)
            items      = src.get("period_of_report", "") or ""
            description = src.get("entity_name", entity)
            filed_at   = src.get("file_date", "")

            # 부정적 공시 제외
            if _is_negative(description):
                continue

            cat_type = _classify_title(description)

            acc_no = src.get("accession_no", "").replace("-", "")
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no}/" if cik and acc_no else ""

            results.append({
                "symbol":   ticker,
                "company":  description,
                "type":     cat_type,
                "title":    description,
                "filed_at": filed_at,
                "url":      doc_url,
            })

            if len(results) >= max_results:
                break

        if results:
            logging.info(
                "[edgar] 8-K 공시 %d건 조회 (최근 %dh)", len(results), hours,
            )
        return results

    except Exception as exc:
        logging.debug("[edgar] 8-K 조회 실패: %s", exc)
        return []


def get_catalyst_symbols(hours: int = 4) -> List[str]:
    """
    8-K 공시에서 티커 심볼만 추출하여 반환.
    news_universe.py와 동일한 인터페이스로 news_universe가 사용.
    """
    events   = fetch_catalyst_8k(hours=hours)
    seen: set = set()
    symbols: List[str] = []
    for ev in events:
        sym = ev.get("symbol", "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    if symbols:
        logging.info("[edgar] 카탈리스트 종목 %d개: %s", len(symbols), symbols[:8])
    return symbols
