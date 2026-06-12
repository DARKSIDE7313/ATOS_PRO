import pandas as pd
from atos.infrastructure.events import SignalEvent
from atos.market.regime.regime_engine import RegimeEngine

class InstitutionalStrategy:
    def __init__(self, event_bus, regime_engine: RegimeEngine):
        self.event_bus = event_bus
        self.regime = regime_engine
        self.prices = []
        self.volumes = []
        self.in_position = False
        self.entry_price = 0
        self.stop_loss = 0
        self.take_profit = 0

    def _ema(self, series, period):
        return series.ewm(span=period, adjust=False).mean()

    def _rsi(self, series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        return (100 - (100 / (1 + rs))).iloc[-1]

    def _atr(self, series, period=14):
        return series.diff().abs().rolling(period).mean().iloc[-1]

    async def on_market(self, market_event):
        self.prices.append(market_event.close)
        self.volumes.append(market_event.volume)

        # 同步 regime engine（用收盘价近似 SPY，真实版应传入 SPY 数据）
        self.regime.update(market_event.close)

        if len(self.prices) < 210:
            return

        closes = pd.Series(self.prices)
        volumes = pd.Series(self.volumes)

        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        ema200 = self._ema(closes, 200)
        rsi = self._rsi(closes)
        atr = self._atr(closes)
        vol_ratio = volumes.iloc[-1] / (volumes.rolling(20).mean().iloc[-1] + 1e-10)
        atr_pct = atr / (closes.iloc[-1] + 1e-10)

        curr_price = closes.iloc[-1]
        curr_ema20 = ema20.iloc[-1]
        curr_ema50 = ema50.iloc[-1]
        curr_ema200 = ema200.iloc[-1]
        prev_ema20 = ema20.iloc[-2]
        prev_ema50 = ema50.iloc[-2]

        regime = self.regime.get_regime()
        risk_mult = regime["risk_multiplier"]

        # 熊市强制平仓
        if risk_mult == 0.0 and self.in_position:
            await self.event_bus.publish(SignalEvent(
                ticker=market_event.ticker, side="SELL",
                confidence=1.0, price=curr_price))
            self.in_position = False
            return

        # 止损
        if self.in_position and curr_price <= self.stop_loss:
            await self.event_bus.publish(SignalEvent(
                ticker=market_event.ticker, side="SELL",
                confidence=1.0, price=curr_price))
            self.in_position = False
            return

        # 止盈
        if self.in_position and curr_price >= self.take_profit:
            await self.event_bus.publish(SignalEvent(
                ticker=market_event.ticker, side="SELL",
                confidence=0.9, price=curr_price))
            self.in_position = False
            return

        # 死叉出场
        if self.in_position and prev_ema20 >= prev_ema50 and curr_ema20 < curr_ema50:
            await self.event_bus.publish(SignalEvent(
                ticker=market_event.ticker, side="SELL",
                confidence=0.8, price=curr_price))
            self.in_position = False
            return

        # 五重入场条件（优化版）
        if not self.in_position and risk_mult > 0:
            trend_up = curr_ema20 > curr_ema50
            above_ema200 = curr_price > curr_ema200
            rsi_ok = 40 <= rsi <= 70
            volume_ok = vol_ratio >= 1.2
            volatility_ok = atr_pct < 0.05

            if trend_up and above_ema200 and rsi_ok and volume_ok and volatility_ok:
                self.entry_price = curr_price
                self.stop_loss = curr_price - 2.5 * atr
                self.take_profit = curr_price + 4.0 * atr
                self.in_position = True
                await self.event_bus.publish(SignalEvent(
                    ticker=market_event.ticker, side="BUY",
                    confidence=0.9 * risk_mult, price=curr_price))
