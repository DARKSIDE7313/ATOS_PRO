import pandas as pd


class RegimeEngine:
    """市场状态检测引擎。

    当前为简化版：在缺乏真实 SPY/VIX 数据时，默认维持中性偏多。
    后续接入真实 SPY 数据后可启用完整四象限判断。
    """

    def __init__(self):
        self.spy_prices = []
        self.vix_prices = []

    def update(self, spy_close: float, vix_close: float = 15.0):
        self.spy_prices.append(spy_close)
        self.vix_prices.append(vix_close)

    def get_regime(self) -> dict:
        """返回市场状态和对应风险乘数。

        当前默认 BULL_WEAK (0.6)，避免因缺少真实 SPY 数据
        而误判熊市导致强制清仓。
        接入 SPY 数据后自动启用完整判断。
        """
        if len(self.spy_prices) < 200:
            return {"regime": "WARMUP", "risk_multiplier": 0.5}

        spy = pd.Series(self.spy_prices)
        vix = pd.Series(self.vix_prices)
        spy_ma200 = spy.rolling(200).mean().iloc[-1]
        current_spy = spy.iloc[-1]
        current_vix = vix.iloc[-1]

        # 完整四象限（需要真实 SPY + VIX 数据）
        if current_spy > spy_ma200 and current_vix < 20:
            return {"regime": "BULL_STRONG", "risk_multiplier": 1.0}
        elif current_spy > spy_ma200 and current_vix <= 30:
            return {"regime": "BULL_WEAK", "risk_multiplier": 0.6}
        elif current_vix > 30:
            return {"regime": "HIGH_VOL", "risk_multiplier": 0.3}
        elif current_spy <= spy_ma200 and current_vix > 25:
            # 真正的熊市：SPY 跌破 200MA + VIX 高企
            return {"regime": "BEAR", "risk_multiplier": 0.0}
        else:
            # 默认中性偏多：当前无 SPY 数据时避免误杀
            return {"regime": "BULL_WEAK", "risk_multiplier": 0.6}
