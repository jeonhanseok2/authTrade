class PaperSimBroker:
    def __init__(self, cash=100000):
        self.cash = float(cash)
        self.positions = {}
        self.last = {}

    def set_price(self, symbol, price): self.last[symbol] = float(price)
    def get_account(self): return {"cash": self.cash, "portfolio_value": self.cash + sum(self.positions.get(s,0)*self.last.get(s,0) for s in self.positions)}
    def list_positions(self): return self.positions.copy()

    def submit_market_order(self, symbol, qty, side):
        price = self.last.get(symbol, 0.0)
        if price <= 0: return {"ok": False, "reason": "no price"}
        if side == "buy":
            cost = qty*price
            if cost > self.cash: return {"ok": False, "reason": "no cash"}
            self.cash -= cost
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
        else:
            owned = self.positions.get(symbol, 0)
            sell_qty = min(qty, owned)
            self.positions[symbol] = owned - sell_qty
            self.cash += sell_qty*price
        return {"ok": True, "symbol": symbol, "qty": qty, "side": side, "price": price}
