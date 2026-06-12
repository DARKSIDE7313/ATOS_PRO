"""
Phoenix Long-Term Value Engine
===============================
Multi-factor long-term strategy: value + quality + momentum + low volatility.

Hold 10-15 positions, rebalance quarterly.
Factors (equal weight):
  1. Value:     P/E ratio (lower = better)
  2. Quality:   ROE (higher = better)  
  3. Momentum:  6-month return (higher = better)
  4. Low Vol:   60-day volatility (lower = better)

Only trades stocks above 200MA (trend filter).
"""

import pandas as pd
import numpy as np
import yfinance as yf


class PhoenixEngine:
    """Long-term factor rotation strategy."""

    def __init__(self):
        self._last_rebalance = None

    def _get_fundamentals(self, ticker: str) -> dict:
        """Fetch fundamental data from yfinance."""
        try:
            info = yf.Ticker(ticker).info
            return {
                'pe': info.get('trailingPE', None),
                'roe': info.get('returnOnEquity', None),
                'pb': info.get('priceToBook', None),
                'debt_equity': info.get('debtToEquity', None),
                'market_cap': info.get('marketCap', None),
            }
        except:
            return {}

    def score_stocks(self, tickers: list) -> list:
        """Score a list of tickers on value/quality/momentum/vol."""
        results = []

        for ticker in tickers:
            try:
                data = yf.download(ticker, period='1y', progress=False)
                if data.empty or len(data) < 200:
                    continue

                closes = data['Close'].values.flatten()

                # Trend filter: must be above 200MA
                sma200 = pd.Series(closes).rolling(200).mean().iloc[-1]
                cp = closes[-1]
                if cp <= sma200:
                    continue

                # 1. Value: use P/B as proxy (lower = cheaper)
                pb = None
                try:
                    info = yf.Ticker(ticker).info
                    pb = info.get('priceToBook')
                except:
                    pass

                # 2. Momentum: 6-month return
                mom_6m = (closes[-1] / closes[-126] - 1) if len(closes) >= 126 else 0

                # 3. Low volatility: 60-day std / price (lower = better)
                vol_60d = pd.Series(closes[-60:]).pct_change().std()
                sharpe_like = mom_6m / (vol_60d + 1e-10)  # return per unit risk

                # 4. Quality proxy: price relative to 200MA (trend strength)
                trend_strength = (cp - sma200) / sma200

                # Composite score
                score = 0.0
                # Value: if P/B available and < 5
                if pb is not None and 0 < pb < 20:
                    score += max(0, (10 - pb) / 10) * 25  # lower P/B = better, max 25pts
                else:
                    score += 10  # no data = neutral

                # Momentum: +25 if positive, scale by strength
                score += min(max(mom_6m * 100, 0), 25)

                # Sharpe-like: higher = better, max 25pts
                score += min(max(sharpe_like * 10, 0), 25)

                # Trend strength: higher = better, max 25pts
                score += min(max(trend_strength * 200, 0), 25)

                results.append({
                    'ticker': ticker, 'score': score,
                    'pb': pb, 'mom_6m': mom_6m,
                    'vol': vol_60d, 'trend': trend_strength,
                    'price': cp
                })

            except Exception as e:
                continue

        results.sort(key=lambda x: x['score'], reverse=True)
        return results

    def generate_portfolio(self, tickers: list, top_n: int = 12,
                           max_weight: float = 0.12) -> list:
        """
        Generate equal-weight portfolio from top-scoring stocks.

        Returns list of {ticker, weight, score, price, ...}
        """
        scored = self.score_stocks(tickers)
        if not scored:
            return []

        selected = scored[:top_n]

        # Equal weight, capped at max_weight
        weight = min(1.0 / len(selected), max_weight)
        total_weight = weight * len(selected)

        # Scale to 100% if total < 1.0
        if total_weight < 1.0:
            weight = 1.0 / len(selected)

        for s in selected:
            s['weight'] = round(weight * 100, 1)
            s['alloc'] = round(weight, 4)

        return selected
