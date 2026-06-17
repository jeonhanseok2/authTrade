# data/finviz_scraper.py
"""
Finviz 무료 스크리너 — HTML 파싱으로 갭업/거래량 종목 수집.

Finviz 무료 티어로 얻을 수 있는 것:
  갭업 % (장 개시 후 change%), 거래량, Float, Short %, 시가총액, 섹터
  ※ 프리마켓 데이터는 Finviz Elite ($24.96/월) 필요 — 무료 티어 불가

적합한 용도:
  - 9:40 AM ET 이후 당일 갭업 종목 필터 (change% >= 10%)
  - 소형 float + 높은 short% 숏스퀴즈 후보 스캔
  - EDGAR 카탈리스트 + Finviz 갭업 교차 검증

사용:
    from data.finviz_scraper import get_gap_candidates, get_short_squeeze_candidates
    gaps = get_gap_candidates(min_change_pct=10.0)
    # [{"symbol": "SOUN", "change_pct": 25.3, "volume": 12000000, ...}, ...]

Rate limit 주의:
  Finviz는 과도한 자동 스크래핑을 차단함.
  요청 간 최소 2초 대기 + User-Agent 설정 필수.
  하루 수십 회 이내로 제한 권장.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from typing import List, Optional

import requests

_BASE_URL   = "https://finviz.com/screener.ashx"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": _USER_AGENT,
    "Accept":     "text/html,application/xhtml+xml",
    "Referer":    "https://finviz.com/",
})

_CACHE_LOCK    = threading.Lock()
_CACHE: dict   = {}
_CACHE_TTL_SEC = 180   # 3분 캐시 (Finviz 과부하 방지)
_LAST_REQ_TS   = 0.0
_MIN_INTERVAL  = 2.0   # 최소 2초 간격


def _throttled_get(url: str, params: dict) -> Optional[requests.Response]:
    """Rate-limit 준수 GET 요청."""
    global _LAST_REQ_TS
    elapsed = _time.monotonic() - _LAST_REQ_TS
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    try:
        resp = _SESSION.get(url, params=params, timeout=15)
        _LAST_REQ_TS = _time.monotonic()
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logging.debug("[finviz] 요청 실패: %s", exc)
        return None


def _parse_table(html: str) -> List[dict]:
    """
    Finviz 스크리너 HTML 테이블 파싱.
    BeautifulSoup 대신 간단한 문자열 파싱 (의존성 최소화).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup  = BeautifulSoup(html, "html.parser")
        table = soup.find("table", {"id": "screener-views-table"})
        if not table:
            # 레이아웃 변경 시 fallback — class로 탐색
            table = soup.find("table", class_="screener_table")
        if not table:
            logging.debug("[finviz] 테이블 없음 — 레이아웃 변경 또는 IP 차단")
            return []

        rows   = table.find_all("tr")
        if len(rows) < 2:
            return []

        # 첫 번째 행 = 헤더
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all("td")]

        results = []
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < len(headers):
                continue

            item = dict(zip(headers, cells))
            results.append(item)
        return results

    except ImportError:
        logging.debug("[finviz] beautifulsoup4 미설치 — pip install beautifulsoup4 lxml")
        return []
    except Exception as exc:
        logging.debug("[finviz] 파싱 실패: %s", exc)
        return []


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val.replace("%", "").replace(",", "").replace("B", "e9")
                     .replace("M", "e6").replace("K", "e3").strip())
    except (ValueError, AttributeError):
        return default


def _build_params(filters: str, order: str = "-change", rows: int = 50) -> dict:
    return {
        "v":  "111",         # Overview 뷰 (No., Ticker, Company, Sector, Price, Change, Volume...)
        "f":  filters,
        "o":  order,
        "r":  1,
        "c":  "1,2,3,4,5,6,7,8,9,10,65,66,67,68",  # 필요 컬럼
    }


def get_gap_candidates(
    min_change_pct:  float = 10.0,   # 최소 갭업 %
    min_volume:      int   = 500_000,# 최소 거래량
    max_price:       float = 50.0,   # 최대 주가 ($)
    min_price:       float = 1.0,    # 최소 주가 ($)
) -> List[dict]:
    """
    Finviz 스크리너로 당일 갭업 종목 조회 (장 개시 후 사용).

    Returns:
        [{"symbol", "change_pct", "volume", "price", "float_m", "short_pct"}, ...]
        빈 리스트 = 스크래핑 실패 또는 해당 종목 없음
    """
    cache_key = f"gap_{min_change_pct}_{min_volume}"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and _time.monotonic() - cached["ts"] < _CACHE_TTL_SEC:
            return cached["data"]

    # Finviz 필터 문자열:
    # geo_usa:  미국 상장
    # sh_price_1to50: $1~$50
    # ta_change_u10:  오늘 +10%+ 상승 (ta_change_u = change > X)
    change_filter = f"ta_change_u{int(min_change_pct)}"
    filters = f"geo_usa,sh_price_{int(min_price)}to{int(max_price)},{change_filter}"

    params = _build_params(filters, order="-change")
    resp   = _throttled_get(_BASE_URL, params)
    if resp is None:
        return []

    raw_rows = _parse_table(resp.text)
    results: List[dict] = []

    for row in raw_rows:
        sym        = row.get("ticker", "").upper()
        change_pct = _safe_float(row.get("change", "0"))
        volume     = int(_safe_float(row.get("volume", "0")))
        price      = _safe_float(row.get("price", "0"))

        if not sym or change_pct < min_change_pct or volume < min_volume:
            continue

        results.append({
            "symbol":     sym,
            "change_pct": round(change_pct, 2),
            "volume":     volume,
            "price":      price,
            "market_cap": row.get("market cap", ""),
            "float_m":    _safe_float(row.get("float", "0")) / 1e6,
            "short_pct":  _safe_float(row.get("short float", "0")),
            "sector":     row.get("sector", ""),
        })

    if results:
        logging.info(
            "[finviz] 갭업 후보 %d종목 (change≥%.0f%%): %s",
            len(results), min_change_pct,
            [r["symbol"] for r in results[:5]],
        )

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"data": results, "ts": _time.monotonic()}

    return results


def get_short_squeeze_candidates(
    min_short_pct:  float = 15.0,   # 최소 숏비율 %
    max_float_m:    float = 50.0,   # 최대 Float (백만주)
    min_change_pct: float = 2.0,    # 최소 오늘 상승 % (움직임 시작 확인)
) -> List[dict]:
    """
    숏스퀴즈 후보 스캔 — 소형 float + 높은 숏비율 + 당일 상승.

    Returns:
        [{"symbol", "short_pct", "float_m", "change_pct", "days_to_cover"}, ...]
    """
    cache_key = f"squeeze_{min_short_pct}_{max_float_m}"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and _time.monotonic() - cached["ts"] < _CACHE_TTL_SEC:
            return cached["data"]

    # 필터: 숏비율 15%+, float 5000만 이하, 오늘 2%+ 상승
    filters = (
        f"geo_usa,"
        f"sh_short_o{int(min_short_pct)},"
        f"sh_float_u{int(max_float_m)},"
        f"ta_change_u{int(min_change_pct)}"
    )
    params = _build_params(filters, order="-short float")
    resp   = _throttled_get(_BASE_URL, params)
    if resp is None:
        return []

    raw_rows = _parse_table(resp.text)
    results: List[dict] = []

    for row in raw_rows:
        sym       = row.get("ticker", "").upper()
        short_pct = _safe_float(row.get("short float", "0"))
        float_m   = _safe_float(row.get("float", "0")) / 1e6
        change    = _safe_float(row.get("change", "0"))

        if not sym or short_pct < min_short_pct:
            continue

        results.append({
            "symbol":        sym,
            "short_pct":     round(short_pct, 1),
            "float_m":       round(float_m, 2),
            "change_pct":    round(change, 2),
            "days_to_cover": _safe_float(row.get("short ratio", "0")),
            "price":         _safe_float(row.get("price", "0")),
        })

    if results:
        logging.info(
            "[finviz] 숏스퀴즈 후보 %d종목: %s",
            len(results), [r["symbol"] for r in results[:5]],
        )

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"data": results, "ts": _time.monotonic()}

    return results


def get_symbols(min_change_pct: float = 10.0) -> List[str]:
    """갭업 종목 심볼만 반환 (news_universe와 동일 인터페이스)."""
    return [r["symbol"] for r in get_gap_candidates(min_change_pct=min_change_pct)]
