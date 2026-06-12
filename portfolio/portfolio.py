class Portfolio:
    def __init__(self, capital=100000):
        self.initial_capital = capital
        self.cash = float(capital)
        self.positions = {}  # ticker -> qty
        self.trades_log = []

    def equity(self, prices: dict) -> float:
        """current total equity = cash + market value of positions"""
        total = self.cash
        for ticker, qty in self.positions.items():
            if ticker in prices:
                total += qty * prices[ticker]
        return total

    def update_fill(self, ticker, side, qty, price):
        if side == "BUY":
            cost = qty * price
            if self.cash >= cost:
                self.cash -= cost
                self.positions[ticker] = self.positions.get(ticker, 0) + qty
                self.trades_log.append({"side": "BUY", "ticker": ticker, "qty": qty, "price": price})
        elif side == "SELL":
            current = self.positions.get(ticker, 0)
            if current >= qty:
                self.cash += qty * price
                self.positions[ticker] = current - qty
                self.trades_log.append({"side": "SELL", "ticker": ticker, "qty": qty, "price": price})
