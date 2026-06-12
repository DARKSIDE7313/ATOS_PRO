"""
RSI-2 Extreme Mean Reversion Strategy
=====================================
Larry Connors' classic "RSI-2" strategy — one of the highest win-rate
strategies in quantitative trading literature (70-80% WR on indices).

Logic:
  BUY:  RSI(2) drops below oversold threshold (extreme panic selling)
  SELL: RSI(2) rises above exit threshold OR price > 5-period SMA

Reference: Connors, L. & Alvarez, C. "Short Term Trading Strategies That Work"
"""

import pandas as pd
import numpy as np


class RSI2Strategy:
    """RSI(2) Extreme Reversion — ultra short-term mean reversion."""

    def __init__(self):
        self.prices = []

    def _rsi(self, series: pd.Series, period: int = 2) -> float:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        return (100 - (100 / (1 + rs))).iloc[-1]

    def generate_signals(self, ticker: str, closes,
                         buy_threshold: float = 15.0,
                         exit_rsi: float = 70.0,
                         stop_pct: float = 0.05,
                         take_pct: float = 0.05) -> list:
        """
        Run RSI-2 strategy on a price series.

        Returns list of signals: [{'action': 'BUY'/'SELL', 'price': float, 'reason': str}, ...]
        """
        prices = []
        signals = []
        in_position = False
        entry_price = 0

        for i, close in enumerate(closes):
            prices.append(float(close))

            if len(prices) < 3:
                continue

            s = pd.Series(prices)
            rsi2 = self._rsi(s, 2)
            sma5 = s.rolling(5).mean().iloc[-1] if len(prices) >= 5 else close

            if in_position:
                # Exit: RSI recovers OR price crosses above SMA5
                if rsi2 > exit_rsi:
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': f'RSI2={rsi2:.1f}>exit',
                        'pnl_pct': (close - entry_price) / entry_price
                    })
                    in_position = False
                elif close > sma5 and rsi2 > 50:
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': 'price > SMA5',
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
                # Entry: RSI(2) extreme oversold
                if rsi2 < buy_threshold:
                    signals.append({
                        'ticker': ticker, 'action': 'BUY',
                        'price': close, 'reason': f'RSI2={rsi2:.1f}',
                        'pnl_pct': 0
                    })
                    entry_price = close
                    in_position = True

        return signals
