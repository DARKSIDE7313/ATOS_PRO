"""
Bollinger Bands Mean Reversion Strategy
========================================
Classic mean reversion: buy when price touches lower band (oversold),
sell when it returns to the middle band or touches upper band (overbought).

Typically 60-65% WR on equities, profit factor 1.5-2.0.
"""

import pandas as pd
import numpy as np


class BollingerStrategy:
    """Bollinger Bands (20,2) mean reversion."""

    def __init__(self):
        self.prices = []

    def _bbands(self, series: pd.Series, period: int = 20, std_dev: float = 2.0):
        sma = series.rolling(period).mean()
        std = series.rolling(period).std()
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        return sma.iloc[-1], upper.iloc[-1], lower.iloc[-1]

    def _rsi(self, series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        return (100 - (100 / (1 + rs))).iloc[-1]

    def generate_signals(self, ticker: str, closes, volumes=None,
                         bb_period: int = 20, bb_std: float = 2.0,
                         rsi_low: float = 35, rsi_high: float = 65,
                         stop_pct: float = 0.05,
                         take_pct: float = 0.08) -> list:
        """
        Run Bollinger mean reversion on price series.

        BUY:  close < lower band AND RSI < oversold threshold
        SELL: close > middle band OR RSI > overbought OR stop/take hit
        """
        prices = []
        signals = []
        in_position = False
        entry_price = 0

        for i, close in enumerate(closes):
            prices.append(float(close))

            if len(prices) < bb_period + 1:
                continue

            s = pd.Series(prices)
            sma, upper, lower = self._bbands(s, bb_period, bb_std)
            rsi14 = self._rsi(s, 14)

            if in_position:
                # Exit: price returns to middle band + RSI confirms (Bug 8: RSI>60 not 50, avoid early exit)
                if close >= sma and rsi14 > 60:
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': f'return to SMA pnl_pct={(close-entry_price)/entry_price:.2%}',
                        'pnl_pct': (close - entry_price) / entry_price
                    })
                    in_position = False
                # Stop loss
                elif close <= entry_price * (1 - stop_pct):
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': f'STOP -{stop_pct:.0%}',
                        'pnl_pct': (close - entry_price) / entry_price
                    })
                    in_position = False
                # Take profit
                elif close >= entry_price * (1 + take_pct):
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': f'TP +{take_pct:.0%}',
                        'pnl_pct': (close - entry_price) / entry_price
                    })
                    in_position = False
            else:
                # Entry: price below lower band + RSI oversold
                if close <= lower and rsi14 <= rsi_low:
                    signals.append({
                        'ticker': ticker, 'action': 'BUY',
                        'price': close,
                        'reason': f'BB lower touch RSI={rsi14:.1f}',
                        'pnl_pct': 0
                    })
                    entry_price = close
                    in_position = True

        return signals
