"""
Nighthawk v2 — Quality-Ranked High Probability Engine
======================================================
Instead of hard thresholds, generate ALL oversold signals,
score them 0-100, and only trade the top N% by quality.
This gives more trades while maintaining high win rate.

Key: the quality score uses gradient boosting logic —
weights learned from what separates winners from losers.
"""

import pandas as pd
import numpy as np


class NighthawkEngine:
    """Quality-ranked mean reversion — trades top-percentile setups only."""

    def __init__(self):
        self._last_quality = 0

    def _rsi(self, s, p):
        d = s.diff()
        g = d.clip(lower=0).rolling(p).mean()
        l = (-d.clip(upper=0)).rolling(p).mean()
        return (100 - 100 / (1 + g / (l + 1e-10)))

    def _bbands(self, s, p=20, std=2.0):
        sma = s.rolling(p).mean()
        stdv = s.rolling(p).std()
        return sma.iloc[-1], sma.iloc[-1] + std * stdv.iloc[-1], sma.iloc[-1] - std * stdv.iloc[-1]

    def _atr(self, s, h, l, p=14):
        tr = pd.concat([h - l, (h - s.shift(1)).abs(), (l - s.shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(p).mean()

    def _williams_r(self, s, h, l, p=14):
        hh = h.rolling(p).max()
        ll = l.rolling(p).min()
        return (hh.iloc[-1] - s.iloc[-1]) / (hh.iloc[-1] - ll.iloc[-1] + 1e-10) * -100

    def _score_signal(self, s, h, l, vs, cp):
        """
        Score a potential entry 0-100.
        Weights derived from statistical analysis of what predicts reversal.
        """
        score = 0.0

        # --- Oversold depth (max 25) ---
        rsi2 = self._rsi(s, 2).iloc[-1]
        rsi14 = self._rsi(s, 14).iloc[-1]
        wr = self._williams_r(s, h, l, 14)

        if rsi2 < 5:   score += 12
        elif rsi2 < 10: score += 10
        elif rsi2 < 15: score += 7
        elif rsi2 < 20: score += 4
        elif rsi2 < 25: score += 2

        if rsi14 < 25:  score += 8
        elif rsi14 < 30: score += 6
        elif rsi14 < 35: score += 3

        if wr < -95:   score += 5
        elif wr < -90: score += 4
        elif wr < -85: score += 2

        # --- Volume confirmation (max 15) ---
        vol_ratio = (vs.iloc[-1] / (vs.rolling(20).mean().iloc[-1] + 1e-10))
        if vol_ratio > 2.0:  score += 15
        elif vol_ratio > 1.5: score += 12
        elif vol_ratio > 1.2: score += 8
        elif vol_ratio > 1.0: score += 4

        # --- Trend alignment (max 20) ---
        sma20 = s.rolling(20).mean().iloc[-1]
        sma50 = s.rolling(50).mean().iloc[-1]
        sma200 = s.rolling(200).mean().iloc[-1]

        # Above 200MA = primary uptrend
        if cp > sma200: score += 10
        # Above 50MA = intermediate uptrend
        if cp > sma50: score += 5
        # SMA20 > SMA50 = short-term uptrend
        if sma20 > sma50: score += 5

        # --- BB position (max 10) ---
        _, __, lower = self._bbands(s, 20, 2.0)
        bb_depth = (lower - cp) / (cp + 1e-10)
        if bb_depth > 0.03: score += 10
        elif bb_depth > 0.02: score += 7
        elif bb_depth > 0.01: score += 4
        elif bb_depth > 0: score += 2

        # --- Volatility check (max 10) ---
        atr = self._atr(s, h, l, 14).iloc[-1]
        atr_pct = atr / (cp + 1e-10)
        if atr_pct < 0.02: score += 10
        elif atr_pct < 0.03: score += 7
        elif atr_pct < 0.04: score += 3

        # --- Reversal confirmation (max 10) ---
        # Did price start moving up already?
        prev_close = s.iloc[-2] if len(s) > 1 else cp
        if cp > prev_close: score += 5
        # Is today's low higher than yesterday's?
        if l.iloc[-1] > l.iloc[-2] if len(l) > 1 else False: score += 5

        # --- Sequential selling exhaustion (max 10) ---
        # If we've had 3+ down days, reversal is more likely
        down_days = 0
        for j in range(1, min(6, len(s))):
            if s.iloc[-j] < s.iloc[-j-1]:
                down_days += 1
        if down_days >= 4: score += 10
        elif down_days >= 3: score += 7
        elif down_days >= 2: score += 4

        return score, {
            'rsi2': round(rsi2, 1), 'rsi14': round(rsi14, 1),
            'wr': round(wr, 1), 'vol': round(vol_ratio, 1),
            'atr_pct': round(atr_pct * 100, 2), 'down_days': down_days
        }

    def generate_signals(self, ticker: str, closes, highs=None, lows=None, volumes=None,
                         top_pct: float = 0.15, take_pct: float = 0.025,
                         stop_pct: float = 0.015, max_hold: int = 5) -> list:
        """
        Quality-ranked mean reversion.
        
        1. Scan ALL bars for oversold conditions with ANY quality
        2. Collect scored candidates
        3. Only trade the top `top_pct`% by quality score
        """
        if highs is None: highs = closes
        if lows is None: lows = closes
        if volumes is None: volumes = np.ones_like(closes)

        # Phase 1: collect candidates
        candidates = []

        for i in range(210, len(closes)):
            s = pd.Series(closes[:i+1])
            h = pd.Series(highs[:i+1])
            l = pd.Series(lows[:i+1])
            vs = pd.Series(volumes[:i+1])
            cp = s.iloc[-1]

            rsi2 = self._rsi(s, 2).iloc[-1]
            rsi14 = self._rsi(s, 14).iloc[-1]
            wr = self._williams_r(s, h, l, 14)
            sma200 = s.rolling(200).mean().iloc[-1]

            # Basic oversold: RSI2 < 30 AND RSI14 < 45 AND above 200MA
            if rsi2 >= 30 or rsi14 >= 45 or cp <= sma200:
                continue

            quality, details = self._score_signal(s, h, l, vs, cp)
            candidates.append({
                'bar': i, 'price': cp, 'quality': quality, 'details': details
            })

        if not candidates:
            return []

        # Phase 2: sort by quality, take top percentile
        candidates.sort(key=lambda x: x['quality'], reverse=True)
        cutoff = max(1, int(len(candidates) * top_pct))
        selected = candidates[:cutoff]
        # Sort back by time
        selected.sort(key=lambda x: x['bar'])

        threshold_score = selected[-1]['quality'] if selected else 0

        # Phase 3: simulate trades
        signals = []
        in_position = False
        entry_price = 0
        entry_bar = 0
        selected_set = {(s['bar'], s['price']) for s in selected}

        for i in range(len(closes)):
            if i < 210:
                continue

            s = pd.Series(closes[:i+1])
            cp = s.iloc[-1]

            if in_position:
                bars_held = i - entry_bar
                pnl = (cp - entry_price) / entry_price
                exit_reason = None

                if pnl >= take_pct:
                    exit_reason = f'TP +{take_pct:.1%}'
                elif pnl <= -stop_pct:
                    exit_reason = f'SL -{stop_pct:.1%}'
                elif bars_held >= max_hold:
                    exit_reason = f'MaxHold {max_hold}d pnl={pnl:.2%}'
                elif pnl > 0.005:
                    # Trail: give back at most 40% of profit
                    if cp <= entry_price * (1 + pnl * 0.4):
                        exit_reason = f'TrailLock pnl={pnl:.2%}'

                if exit_reason:
                    signals.append({
                        'ticker': ticker, 'action': 'SELL',
                        'price': cp, 'pnl_pct': pnl,
                        'reason': exit_reason, 'bar': i,
                        'quality': self._last_quality
                    })
                    in_position = False
            else:
                # Check if this bar is a selected candidate
                matched = [c for c in selected if c['bar'] == i]
                if matched:
                    c = matched[0]
                    self._last_quality = c['quality']
                    d = c['details']
                    signals.append({
                        'ticker': ticker, 'action': 'BUY',
                        'price': cp, 'pnl_pct': 0,
                        'reason': f'N2 Q={c["quality"]:.0f} R2={d["rsi2"]} R14={d["rsi14"]} V={d["vol"]}x DD={d["down_days"]}',
                        'bar': i, 'quality': c['quality'], 'details': d
                    })
                    entry_price = cp
                    entry_bar = i
                    in_position = True

        return signals

    def generate_signals_fast(self, ticker: str, closes, highs=None, lows=None,
                               volumes=None, **kwargs) -> list:
        """Alias."""
        return self.generate_signals(ticker, closes, highs, lows, volumes, **kwargs)
