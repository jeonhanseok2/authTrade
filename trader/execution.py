from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

class AlpacaBroker:
    def __init__(self, api_key, secret, paper=True):
        self.client = TradingClient(api_key, secret, paper=paper)

    def get_account(self):
        a = self.client.get_account()
        return {"cash": float(a.cash), "portfolio_value": float(a.portfolio_value)}

    def list_positions(self):
        out = {}
        for p in self.client.get_all_positions():
            out[p.symbol] = float(p.qty)
        return out

    def submit_market_order(self, symbol, qty, side):
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=(OrderSide.BUY if side=='buy' else OrderSide.SELL), time_in_force=TimeInForce.DAY)
        o = self.client.submit_order(req)
        return {"ok": True, "id": o.id, "symbol": symbol, "qty": qty, "side": side}
