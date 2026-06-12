"""
AlphaEngine — Multi-Strategy Quantitative Trading System
========================================================
Combines 3 proven strategies from open-source quant research:
  1. Bollinger Mean Reversion (QuantEdge: 64% WR, PF 2.1)
  2. Short-Term Reversal (randomwalkhan: 69% WR)
  3. Volume-Confirmed Momentum (je-suis-tm/quant-trading)

Entry requires at least 2 of 3 signals to agree (ensemble approach).
Exit uses trailing ATR stop (locks in profits) + hard stop.

Reference:
  - github.com/punyamodi/QuantEdge
  - github.com/randomwalkhan/Short-Term-Reversal-Strategy
  - github.com/je-suis-tm/quant-trading
"""

import pandas as pd
import numpy as np


class AlphaEngine:
    """Ensemble trading engine — combines multiple signal sources."""

    def __init__(self):
        self.prices = []
        self.volumes = []
        self.highs = []
        self.lows = []

    # ---------- Indicators ----------

    def _sma(self, s, p):
        return s.rolling(p).mean()

    def _ema(self, s, p):
        return s.ewm(span=p, adjust=False).mean()

    def _rsi(self, s, p=14):
        d = s.diff()
        g = d.clip(lower=0).rolling(p).mean()
        l = (-d.clip(upper=0)).rolling(p).mean()
        return (100 - 100 / (1 + g / (l + 1e-10))).iloc[-1]

    def _bbands(self, s, p=20, std=2.0):
        sma = s.rolling(p).mean()
        stdv = s.rolling(p).std()
        return sma.iloc[-1], sma.iloc[-1] + std * stdv.iloc[-1], sma.iloc[-1] - std * stdv.iloc[-1]

    def _atr(self, s, h, l, p=14):
        tr = pd.concat([h - l, (h - s.shift(1)).abs(), (l - s.shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(p).mean().iloc[-1]

    def _williams_r(self, s, h, l, p=14):
        hh = h.rolling(p).max()
        ll = l.rolling(p).min()
        return ((hh.iloc[-1] - s.iloc[-1]) / (hh.iloc[-1] - ll.iloc[-1] + 1e-10) * -100)

    def _volume_ratio(self, vs, p=20):
        return vs.iloc[-1] / (vs.rolling(p).mean().iloc[-1] + 1e-10)

    # ---------- Signal Generators ----------

    def _signal_bollinger(self, s, h, l, vs):
        """Signal 1: Bollinger Bands Mean Reversion (QuantEdge)"""
        sma, upper, lower = self._bbands(s, 20, 2.0)
        rsi14 = self._rsi(s, 14)
        cp = s.iloc[-1]
        vol_ratio = self._volume_ratio(vs, 20)

        # Buy: price near/below lower band + RSI oversold + volume confirmation
        if cp <= lower * 1.02 and rsi14 < 40 and vol_ratio > 1.0:
            return 1, f"BB_rev RSI={rsi14:.0f} vol={vol_ratio:.1f}x"

        # Sell: price near/above upper band + RSI overbought
        if cp >= upper * 0.98 and rsi14 > 60:
            return -1, f"BB_top RSI={rsi14:.0f}"

        return 0, ""

    def _signal_reversal(self, s, h, l, vs):
        """Signal 2: Short-Term Reversal (randomwalkhan)"""
        rsi2 = self._rsi(s, 2)
        rsi14 = self._rsi(s, 14)
        wr = self._williams_r(s, h, l, 14)
        cp = s.iloc[-1]
        sma5 = self._sma(s, 5).iloc[-1]

        # Buy: extreme oversold on multiple oscillators
        buy_score = 0
        if rsi2 < 20: buy_score += 1
        if rsi14 < 35: buy_score += 1
        if wr < -80: buy_score += 1
        if cp < sma5: buy_score += 1  # price below short MA = discount

        if buy_score >= 2:
            return 1, f"REV RSI2={rsi2:.0f} RSI14={rsi14:.0f} WR={wr:.0f}"

        # Sell: extreme overbought
        sell_score = 0
        if rsi2 > 80: sell_score += 1
        if rsi14 > 70: sell_score += 1
        if wr > -20: sell_score += 1
        if cp > sma5 * 1.03: sell_score += 1

        if sell_score >= 2:
            return -1, f"REV_exit RSI2={rsi2:.0f}"

        return 0, ""

    def _signal_momentum(self, s, h, l, vs):
        """Signal 3: Volume-Confirmed Momentum (je-suis-tm)"""
        ema10 = self._ema(s, 10).iloc[-1]
        ema30 = self._ema(s, 30).iloc[-1]
        ema50 = self._ema(s, 50).iloc[-1]
        vol_ratio = self._volume_ratio(vs, 20)
        cp = s.iloc[-1]

        # Buy: uptrend (short MA above long MA) + volume confirmation
        if ema10 > ema50 and cp > ema10 and vol_ratio > 1.1:
            return 1, f"MOM ema_aligned vol={vol_ratio:.1f}x"

        # Sell: trend breaks down
        if ema10 < ema30 and cp < ema30:
            return -1, f"MOM_break ema10<30"

        return 0, ""

    # ---------- Main Strategy ----------

    def generate_signals(self, ticker: str, closes, highs=None, lows=None, volumes=None,
                         stop_atr: float = 2.5, take_atr: float = 3.0) -> list:
        """
        Run ensemble strategy. Requires 2+ confirmations to enter.
        Uses trailing ATR stop for exits.
        """
        # Bug 1: Clear state on each call to prevent leakage across tickers
        self.prices = []
        self.highs = []
        self.lows = []
        self.volumes = []

        if highs is None:
            highs = closes
        if lows is None:
            lows = closes
        if volumes is None:
            volumes = np.ones_like(closes)

        signals = []
        in_position = False
        entry_price = 0
        peak_price = 0
        n_bars = len(closes)

        for i in range(n_bars):
            self.prices.append(float(closes[i]))
            self.highs.append(float(highs[i]))
            self.lows.append(float(lows[i]))
            self.volumes.append(float(volumes[i]))

            if len(self.prices) < 60:
                continue

            s = pd.Series(self.prices)
            h = pd.Series(self.highs)
            l = pd.Series(self.lows)
            vs = pd.Series(self.volumes)

            cp = s.iloc[-1]
            atr = self._atr(s, h, l, 14)

            if in_position:
                peak_price = max(peak_price, cp)
                trailing_stop = peak_price - stop_atr * atr

                # Exit signals
                b_sig, b_reason = self._signal_bollinger(s, h, l, vs)
                r_sig, r_reason = self._signal_reversal(s, h, l, vs)
                m_sig, m_reason = self._signal_momentum(s, h, l, vs)

                sell_votes = sum(1 for x in [b_sig, r_sig, m_sig] if x == -1)
                exit_reason = []

                if cp <= trailing_stop:
                    exit_reason.append(f"TrailStop {stop_atr}ATR")
                if sell_votes >= 2:
                    reasons = [r for s, r in [(b_sig, b_reason), (r_sig, r_reason), (m_sig, m_reason)] if s == -1]
                    exit_reason.append("|".join(reasons))
                # Take profit: use ATR/entry_price for consistent % across tickers
                atr_pct = atr / entry_price
                if cp >= entry_price * (1 + take_atr * atr_pct):
                    exit_reason.append(f"TP +{take_atr}ATR")
                # Max hold: 20 bars
                if len(self.prices) - self._entry_bar > 20:
                    exit_reason.append("MaxHold 20d")

                if exit_reason:
                    pnl_pct = (cp - entry_price) / entry_price
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': cp, 'pnl_pct': pnl_pct,
                        'reason': '|'.join(exit_reason),
                        'bar': i
                    })
                    in_position = False
            else:
                # Entry: need 2+ buy signals
                b_sig, b_reason = self._signal_bollinger(s, h, l, vs)
                r_sig, r_reason = self._signal_reversal(s, h, l, vs)
                m_sig, m_reason = self._signal_momentum(s, h, l, vs)

                buy_votes = sum(1 for x in [b_sig, r_sig, m_sig] if x == 1)
                if buy_votes >= 2:
                    reasons = [r for s, r in [(b_sig, b_reason), (r_sig, r_reason), (m_sig, m_reason)] if s == 1]
                    signals.append({
                        'ticker': ticker, 'action': 'BUY',
                        'price': cp, 'pnl_pct': 0,
                        'reason': '|'.join(reasons),
                        'bar': i
                    })
                    entry_price = cp
                    peak_price = cp
                    self._entry_bar = len(self.prices)
                    in_position = True

        return signals
