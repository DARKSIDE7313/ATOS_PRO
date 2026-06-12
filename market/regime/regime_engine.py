import pandas as pd

class RegimeEngine:
    def __init__(self):
        self.spy_prices = []
        self.vix_prices = []

    def update(self, spy_close: float, vix_close: float = 15.0):
        self.spy_prices.append(spy_close)
        self.vix_prices.append(vix_close)

    def get_regime(self) -> dict:
        if len(self.spy_prices) < 200:
            return {"regime": "UNKNOWN", "risk_multiplier": 0.5}

        spy = pd.Series(self.spy_prices)
        vix = pd.Series(self.vix_prices)
        spy_ma200 = spy.rolling(200).mean().iloc[-1]
        current_spy = spy.iloc[-1]
        current_vix = vix.iloc[-1]

        if current_spy > spy_ma200 and current_vix < 20:
            return {"regime": "BULL_STRONG", "risk_multiplier": 1.0}
        elif current_spy > spy_ma200 and current_vix <= 30:
            return {"regime": "BULL_WEAK", "risk_multiplier": 0.6}
        elif current_vix > 30:
            return {"regime": "HIGH_VOL", "risk_multiplier": 0.3}
        else:
            return {"regime": "BEAR", "risk_multiplier": 0.0}
