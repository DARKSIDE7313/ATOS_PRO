import asyncio
from pandas import DataFrame
import yfinance as yf
from atos.infrastructure.events import MarketEvent


class BacktestEngine:
    def __init__(self, ticker: str, event_bus):
        self.ticker = ticker
        self.event_bus = event_bus

    async def run(self):
        """fetch historical data from yfinance and feed events"""
        loop = asyncio.get_running_loop()

        def _download():
            return yf.download(self.ticker, period="1y", progress=False)

        try:
            data = await loop.run_in_executor(None, _download)
        except Exception as e:
            print(f"[Backtest] yfinance download failed for {self.ticker}: {e}")
            return

        if data is None or data.empty:
            print(f"[Backtest] no data for {self.ticker}")
            return

        # handle MultiIndex (yfinance may return multi-level columns)
        closes = data["Close"]
        if isinstance(closes, DataFrame):
            closes = closes.iloc[:, 0]
        volumes = data["Volume"] if "Volume" in data else None
        if volumes is not None and isinstance(volumes, DataFrame):
            volumes = volumes.iloc[:, 0]

        for idx, (ts, close_val) in enumerate(zip(closes.index, closes.values)):
            close = float(close_val)
            volume = int(volumes.iloc[idx]) if volumes is not None else 0
            await self.event_bus.publish(MarketEvent(
                ticker=self.ticker,
                close=close,
                volume=volume
            ))
            # yield control every 50 bars to avoid blocking the loop
            if idx % 50 == 0:
                await asyncio.sleep(0)

        self.event_bus.stop()
