import pandas as pd


class FutuGateway:
    def __init__(self):
        self._connected = False
        self._order_counter = 1000

    def connect(self):
        self._connected = True
        print("[FutuGateway] mock connection established")

    def place_order(self, ticker, qty, side, simulate=True):
        self._order_counter += 1
        data = pd.DataFrame([{
            "order_id": f"ORD{self._order_counter}",
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "status": "FILLED" if simulate else "PENDING"
        }])
        return 0, data

    def close(self):
        self._connected = False
