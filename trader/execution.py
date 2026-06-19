from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

class AlpacaBroker:
    def __init__(self, api_key, secret, paper=True):
        self.client = TradingClient(api_key, secret, paper=paper)

    def get_account(self):
        a = self.client.get_account()
        result = {
            "cash":            float(a.cash),
            "portfolio_value": float(a.portfolio_value),
        }
        try:
            equity      = float(a.equity)
            last_equity = float(a.last_equity)
            result["equity"]      = equity
            result["last_equity"] = last_equity
            result["day_pl"]      = equity - last_equity
            result["day_pl_pct"]  = (equity - last_equity) / last_equity * 100 if last_equity else 0.0
        except Exception:
            pass
        return result

    def list_positions_detail(self) -> list:
        out = []
        for p in self.client.get_all_positions():
            try:
                out.append({
                    "symbol":        p.symbol,
                    "qty":           float(p.qty),
                    "avg_entry":     float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "market_value":  float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_pct": float(p.unrealized_plpc) * 100,
                })
            except Exception:
                out.append({"symbol": p.symbol, "qty": float(p.qty)})
        return out

    def get_settled_cash(self) -> float:
        """
        결제 완료 현금 조회 (Cash Account T+1 기준).
        Cash Account: non_marginable_buying_power = 미결제 자금 제외한 순수 결제 현금.
        Margin Account: cash 필드 사용 (fallback).
        """
        a = self.client.get_account()
        val = getattr(a, "non_marginable_buying_power", None)
        if val is None:
            val = getattr(a, "cash", 0)
        return float(val or 0)

    def list_positions(self):
        out = {}
        for p in self.client.get_all_positions():
            out[p.symbol] = float(p.qty)
        return out

    def submit_order(
        self,
        symbol:    str,
        qty:       int,
        side:      str,
        type:      str = "market",
        price:     float | None = None,
        tif:       str = "DAY",
        **kwargs,
    ) -> dict:
        """지정가/시장가 주문 통합 인터페이스 (Toss submit_order와 동일 시그니처)."""
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif_enum  = TimeInForce.IOC if tif.upper() == "IOC" else TimeInForce.DAY

        if type.lower() == "limit" and price:
            req = LimitOrderRequest(
                symbol=symbol, qty=qty, side=side_enum,
                time_in_force=tif_enum, limit_price=round(price, 2),
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol, qty=qty, side=side_enum,
                time_in_force=TimeInForce.DAY,
            )
        o = self.client.submit_order(req)
        return {"ok": True, "id": str(o.id), "symbol": symbol, "qty": qty, "side": side}

    def submit_market_order(self, symbol, qty, side):
        """하위호환 유지용 — submit_order로 위임."""
        return self.submit_order(symbol, qty, side, type="market")
