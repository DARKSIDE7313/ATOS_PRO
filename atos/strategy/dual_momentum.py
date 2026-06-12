"""
Dual Momentum Strategy
======================
Gary Antonacci's dual momentum: compare short-term vs long-term momentum.
When short-term momentum dominates, go long. When it flips, go flat.

Reference: Antonacci, G. "Dual Momentum Investing"
Typically 50-55% WR but high profit factor (big wins, small losses).
"""

import pandas as pd
import numpy as np


class DualMomentumStrategy:
    """Dual momentum: short-term (3mo) vs long-term (12mo) return comparison."""

    def __init__(self):
        self.prices = []

    def generate_signals(self, ticker: str, closes, volumes=None,
                         short_window: int = 63,
                         long_window: int = 252,
                         stop_pct: float = 0.08,
                         take_pct: float = 0.20) -> list:
        """
        Run dual momentum on a price series.

        BUY:  short-term return > long-term return AND short-term return > 0
        SELL: short-term return < long-term return (momentum flips) or stop/take
        """
        prices = []
        signals = []
        in_position = False
        entry_price = 0
        peak_price = 0

        for i, close in enumerate(closes):
            prices.append(float(close))

            if len(prices) < long_window + 1:
                continue

            short_ret = prices[-1] / prices[-short_window] - 1
            long_ret = prices[-1] / prices[-long_window] - 1

            if in_position:
                peak_price = max(peak_price, close)
                # Exit: momentum flips
                if short_ret < long_ret:
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close,
                        'reason': f'momentum flip SR={short_ret:.1%} LR={long_ret:.1%}',
                        'pnl_pct': (close - entry_price) / entry_price
                    })
                    in_position = False
                # Trailing stop: from peak (not entry)
                elif close <= peak_price * (1 - stop_pct):
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': close, 'reason': f'TrailStop -{stop_pct:.0%} from peak',
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
                # Entry: short momentum > long AND positive + volume confirmation
                if volumes is not None and len(prices) >= 20:
                    vol_series = pd.Series(volumes[:len(prices)])
                    avg_vol = vol_series.rolling(20).mean().iloc[-1]
                    current_vol = vol_series.iloc[-1]
                    vol_ok = current_vol > avg_vol * 0.7
                else:
                    vol_ok = True

                if short_ret > long_ret and short_ret > 0 and vol_ok:
                    signals.append({
                        'ticker': ticker, 'action': 'BUY',
                        'price': close,
                        'reason': f'mom SR={short_ret:.1%}>LR={long_ret:.1%} vol={current_vol/avg_vol:.1f}x',
                        'pnl_pct': 0
                    })
                    entry_price = close
                    peak_price = close
                    in_position = True

        return signals
