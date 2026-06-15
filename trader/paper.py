class PaperSimBroker:
    def __init__(
        self,
        cash: float = 100_000.0,
        commission_per_share: float = 0.0,   # Alpaca 무료 티어
        slippage_bps: float = 5.0,           # 5 basis points 시장 충격
    ):
        self.cash       = float(cash)
        self.positions: dict = {}
        self.last:      dict = {}
        self._commission = float(commission_per_share)
        self._slip_bps   = float(slippage_bps)

    def set_price(self, symbol: str, price: float) -> None:
        self.last[symbol] = float(price)

    def get_account(self) -> dict:
        pv = sum(
            self.positions.get(s, 0) * self.last.get(s, 0.0)
            for s in self.positions
        )
        return {"cash": self.cash, "portfolio_value": self.cash + pv}

    def list_positions(self) -> dict:
        return {k: v for k, v in self.positions.items() if v > 0}

    def submit_market_order(self, symbol: str, qty: int, side: str) -> dict:
        raw_price = self.last.get(symbol, 0.0)
        if raw_price <= 0:
            return {"ok": False, "reason": "no price"}

        slip = self._slip_bps / 10_000.0
        fill_price = raw_price * (1.0 + slip) if side == "buy" else raw_price * (1.0 - slip)
        commission = self._commission * qty

        if side == "buy":
            cost = fill_price * qty + commission
            if cost > self.cash:
                return {"ok": False, "reason": "insufficient cash"}
            self.cash -= cost
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
        else:
            owned    = self.positions.get(symbol, 0)
            sell_qty = min(qty, owned)
            self.positions[symbol] = owned - sell_qty
            self.cash += fill_price * sell_qty - commission

        return {
            "ok":    True,
            "symbol": symbol,
            "qty":   qty,
            "side":  side,
            "price": fill_price,
        }
