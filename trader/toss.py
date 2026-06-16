# trader/toss.py
"""
토스증권 Open API 브로커 어댑터.

Base URL : https://openapi.tossinvest.com
Auth     : POST /oauth2/token  (Client Credentials)
Order    : POST /api/v1/orders
Positions: GET  /api/v1/holdings
Account  : GET  /api/v1/accounts
Prices   : GET  /api/v1/prices        (현재가, 최대 200종목)
Candles  : GET  /api/v1/candles       (1m / 1d)
Orderbook: GET  /api/v1/orderbook     (호가)

제약:
  - WebSocket 없음 → 폴링으로 대체
  - 샌드박스 없음 → MODE=paper 시 PaperSimBroker 사용
  - 수수료 0.1% / 거래
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests


BASE_URL = "https://openapi.tossinvest.com"


class TossInvestBroker:
    def __init__(self, client_id: str, client_secret: str, account_seq: int):
        self._client_id     = client_id
        self._client_secret = client_secret
        self._account_seq   = str(account_seq)
        self._token:    Optional[str] = None
        self._token_exp: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── 인증 ──────────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._session.post(
            f"{BASE_URL}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "client_credentials",
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        self._token     = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 86400))
        self._session.headers.update({"Authorization": f"Bearer {self._token}"})
        logging.info("[Toss] 토큰 갱신 완료 (expires_in=%s)", body.get("expires_in"))
        return self._token

    def _headers(self, with_account: bool = False) -> Dict[str, str]:
        self._ensure_token()
        h = {}
        if with_account:
            h["X-Tossinvest-Account"] = self._account_seq
        return h

    # ── 계좌 ──────────────────────────────────────────────────────────

    def get_account(self) -> Dict:
        """계좌 잔고 + 평가금액 반환."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/accounts",
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        accounts = r.json().get("result", [])
        # accountSeq 일치 계좌 조회
        for acc in accounts:
            if str(acc.get("accountSeq")) == self._account_seq:
                return {
                    "accountNo":   acc.get("accountNo"),
                    "accountType": acc.get("accountType"),
                }
        return {"accountNo": "", "accountType": ""}

    def get_balance(self) -> Dict:
        """보유 포지션 전체 조회로 평가금액/잔고 계산."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/holdings",
            headers=self._headers(with_account=True),
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        mv     = result.get("marketValue", {}).get("amount", {})
        pl     = result.get("profitLoss", {})
        return {
            "cash":            float(mv.get("usd") or 0),
            "portfolio_value": float(mv.get("usd") or 0),
            "pnl_usd":         float((pl.get("amount") or {}).get("usd") or 0),
            "pnl_rate":        float(pl.get("rate") or 0),
        }

    # ── 포지션 ────────────────────────────────────────────────────────

    def list_positions(self) -> Dict[str, float]:
        """보유 종목 {symbol: qty} 반환."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/holdings",
            headers=self._headers(with_account=True),
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("result", {}).get("items", [])
        return {it["symbol"]: float(it["quantity"]) for it in items}

    def get_position(self, symbol: str) -> Optional[Dict]:
        """단일 종목 포지션 정보."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/holdings",
            headers=self._headers(with_account=True),
            params={"symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("result", {}).get("items", [])
        for it in items:
            if it["symbol"] == symbol:
                return {
                    "symbol":               it["symbol"],
                    "qty":                  float(it["quantity"]),
                    "avg_entry_price":      float(it.get("averagePurchasePrice") or 0),
                    "current_price":        float(it.get("lastPrice") or 0),
                }
        return None

    # ── 시세 ──────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """현재가 (USD)."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/prices",
            headers=self._headers(),
            params={"symbols": symbol},
            timeout=8,
        )
        r.raise_for_status()
        items = r.json().get("result", [])
        for it in items:
            if it["symbol"] == symbol:
                return float(it.get("lastPrice") or 0)
        return 0.0

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        """복수 종목 현재가 (최대 200개)."""
        if not symbols:
            return {}
        r = self._session.get(
            f"{BASE_URL}/api/v1/prices",
            headers=self._headers(),
            params={"symbols": ",".join(symbols)},
            timeout=10,
        )
        r.raise_for_status()
        return {
            it["symbol"]: float(it.get("lastPrice") or 0)
            for it in r.json().get("result", [])
        }

    def get_orderbook(self, symbol: str) -> Dict:
        """호가 (bid/ask 최우선)."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/orderbook",
            headers=self._headers(),
            params={"symbol": symbol},
            timeout=8,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        bids   = result.get("bids", [])
        asks   = result.get("asks", [])
        return {
            "bid":     float(bids[0]["price"]) if bids else 0.0,
            "ask":     float(asks[0]["price"]) if asks else 0.0,
            "bid_qty": float(bids[0].get("quantity", 0)) if bids else 0.0,
            "ask_qty": float(asks[0].get("quantity", 0)) if asks else 0.0,
        }

    def get_candles(
        self,
        symbol:   str,
        interval: str = "1m",   # "1m" | "1d"
        count:    int = 100,
    ) -> List[Dict]:
        """분봉/일봉 캔들 데이터."""
        r = self._session.get(
            f"{BASE_URL}/api/v1/candles",
            headers=self._headers(),
            params={"symbol": symbol, "interval": interval, "count": count},
            timeout=10,
        )
        r.raise_for_status()
        candles = r.json().get("result", {}).get("candles", [])
        return [
            {
                "timestamp": c["timestamp"],
                "open":      float(c["openPrice"]),
                "high":      float(c["highPrice"]),
                "low":       float(c["lowPrice"]),
                "close":     float(c["closePrice"]),
                "volume":    float(c["volume"]),
            }
            for c in candles
        ]

    # ── 주문 ──────────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol:    str,
        qty:       int,
        side:      str,           # "buy" | "sell"
        type:      str = "market",  # "market" | "limit"
        price:     Optional[float] = None,
        tif:       str = "DAY",        # "DAY" or "IOC"
        client_id: Optional[str]  = None,
    ) -> Dict:
        """주문 제출."""
        payload: Dict = {
            "symbol":    symbol,
            "side":      "BUY" if side.lower() == "buy" else "SELL",
            "orderType": "LIMIT" if type.lower() == "limit" else "MARKET",
            "quantity":  str(qty),
        }
        if type.lower() == "limit" and price:
            payload["price"]       = str(price)
            payload["timeInForce"] = tif.upper()  # "DAY" or "IOC"
        if client_id:
            payload["clientOrderId"] = client_id

        r = self._session.post(
            f"{BASE_URL}/api/v1/orders",
            headers=self._headers(with_account=True),
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        logging.info("[Toss] 주문 완료: %s %s %d주 → orderId=%s",
                     side.upper(), symbol, qty, result.get("orderId"))
        return {
            "ok":      True,
            "id":      result.get("orderId"),
            "symbol":  symbol,
            "qty":     qty,
            "side":    side,
        }

    def submit_market_order(self, symbol: str, qty: int, side: str) -> Dict:
        """시장가 주문 (telegram_bot 호환용)."""
        return self.submit_order(symbol, qty, side, type="market")
