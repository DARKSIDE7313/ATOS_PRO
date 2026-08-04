"""
ATOS 回测第二轮 — 自适应策略 + 激进参数测试
重点: 让赢家奔跑 + 牛市更激进 + 熊市更防守
"""

import sys, os, json, numpy as np, pandas as pd, yfinance as yf
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atos.tools.backtest_optimizer import (
    ATOSBacktest, Position, download_data, calc_rsi, calc_atr,
    calc_bollinger, calc_macd, compute_factor_score, quality_gate,
    entry_filters_ok, INITIAL_CAPITAL, TEST_UNIVERSE
)


class AdaptiveATOSBacktest(ATOSBacktest):
    """Adaptive backtest: adjusts parameters based on market regime."""

    def run(self, historical_data, dates):
        cfg = self.config

        for day_idx, date in enumerate(dates):
            date_str = str(date)[:10]

            # Get daily data
            daily_prices, daily_signals = self._compute_daily_signals(
                historical_data, date)

            if not daily_prices:
                continue

            # Determine SPY trend for regime adaptation
            spy_price = daily_prices.get("SPY", 500)
            spy_sig = daily_signals.get("SPY", {})
            spy_ma50 = spy_sig.get("ma50", spy_price)
            spy_ma200 = spy_sig.get("ma200", spy_price)

            # Adaptive regime detection
            if spy_price > spy_ma50 * 1.03 and spy_ma50 > spy_ma200:
                regime = "BULL_STRONG"
                # Aggressive in strong bull:
                # - Lower entry threshold
                # - Wider trail stops (let winners run)
                # - More positions
                # - Shorter cooldown
                score_thr = cfg["score_threshold"] - 0.05  # Easier entry
                trail_pct = cfg["trail_pct"] + 0.03         # Wider trail
                max_pos = cfg["max_positions"] + 4           # More positions
                cooldown = max(3, cfg["cooldown_days"] // 3) # Short cooldown
                tp_pct = cfg["take_profit_pct"] + 0.05      # Higher TP
                sl_pct = cfg["stop_loss_pct"]                # Same SL
            elif spy_price > spy_ma50:
                regime = "BULL_WEAK"
                score_thr = cfg["score_threshold"]
                trail_pct = cfg["trail_pct"] + 0.01
                max_pos = cfg["max_positions"] + 2
                cooldown = max(5, cfg["cooldown_days"] // 2)
                tp_pct = cfg["take_profit_pct"] + 0.02
                sl_pct = cfg["stop_loss_pct"]
            elif spy_price > spy_ma200:
                regime = "SIDEWAYS"
                score_thr = cfg["score_threshold"] + 0.02
                trail_pct = cfg["trail_pct"]
                max_pos = cfg["max_positions"]
                cooldown = cfg["cooldown_days"]
                tp_pct = cfg["take_profit_pct"]
                sl_pct = cfg["stop_loss_pct"]
            else:
                regime = "BEAR"
                # Defensive in bear:
                # - Higher entry threshold
                # - Tighter stops
                # - Fewer positions
                score_thr = cfg["score_threshold"] + 0.08
                trail_pct = cfg["trail_pct"] - 0.02
                max_pos = max(3, cfg["max_positions"] // 3)
                cooldown = cfg["cooldown_days"] * 2
                tp_pct = cfg["take_profit_pct"] - 0.03
                sl_pct = cfg["stop_loss_pct"] - 0.01

            trail_pct = max(0.04, min(0.20, trail_pct))
            score_thr = max(0.25, min(0.55, score_thr))
            sl_pct = max(0.04, min(0.10, sl_pct))
            tp_pct = max(0.10, min(0.40, tp_pct))

            # ---- EXITS ----
            for sym in list(self.positions.keys()):
                if sym not in daily_prices:
                    continue
                pos = self.positions[sym]
                price = daily_prices[sym]
                pnl_pct = (price - pos.avg_price) / pos.avg_price

                # Update trailing stop
                pos.highest_price = max(pos.highest_price, price)
                if pnl_pct > 0.05:
                    trail = pos.highest_price * (1 - trail_pct)
                    pos.trail_stop = max(pos.trail_stop, trail)

                sell_reason = None
                sell_qty = pos.shares

                if pnl_pct <= -sl_pct:
                    sell_reason = f"止损{pnl_pct:.1%}"
                elif pnl_pct >= tp_pct:
                    sell_reason = f"止盈+{pnl_pct:.1%}"
                elif pos.trail_stop > 0 and price < pos.trail_stop:
                    sell_reason = f"追踪{pnl_pct:.1%}"
                # Partial profit: only in SIDEWAYS/BEAR
                elif pnl_pct >= 0.15 and pos.shares >= 6 and regime in ("SIDEWAYS", "BEAR"):
                    sell_qty = max(1, pos.shares // 4)
                    sell_reason = f"锁利+{pnl_pct:.1%}"

                if sell_reason:
                    pnl = (price - pos.avg_price) * sell_qty
                    self.cash += price * sell_qty * 0.999
                    pos.shares -= sell_qty
                    self.trades.append({
                        "date": date_str, "symbol": sym, "action": "SELL",
                        "shares": sell_qty, "price": price,
                        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                        "reason": sell_reason,
                    })
                    if pos.shares <= 0:
                        del self.positions[sym]
                        self.cooldown[sym] = day_idx

            # ---- ENTRIES ----
            if regime != "BEAR" or spy_price > spy_ma200:
                if len(self.positions) < max_pos:
                    picks = []
                    for sym, sig in daily_signals.items():
                        if sym in self.positions:
                            continue
                        last_sold = self.cooldown.get(sym, -999)
                        if day_idx - last_sold < cooldown:
                            continue
                        result = compute_factor_score(sym, sig, regime)
                        picks.append(result)

                    picks.sort(key=lambda x: -x["score"])

                    for pick in picks[:25]:
                        if len(self.positions) >= max_pos:
                            break
                        sym = pick["symbol"]
                        sig = daily_signals.get(sym, {})
                        price = sig.get("price", 0)

                        # Relaxed entry filters in bull markets
                        if regime in ("BULL_STRONG", "BULL_WEAK"):
                            # Skip quality gate, just check basics
                            if pick["score"] < score_thr:
                                continue
                            rsi = sig.get("rsi", 50)
                            if rsi > 80 or rsi < 25:
                                continue
                            macd = sig.get("macd_hist", 0)
                            if macd < -0.01:
                                continue
                            # Don't require price > MA50 in weak bull
                        else:
                            ok, reason = entry_filters_ok(pick, sig, {
                                "score_threshold": score_thr})
                            if not ok:
                                continue

                        # Position sizing
                        if pick["score"] >= 0.55:
                            base_pct = 0.10 if "BULL" in regime else 0.06
                        elif pick["score"] >= 0.45:
                            base_pct = 0.08 if "BULL" in regime else 0.05
                        elif pick["score"] >= 0.35:
                            base_pct = 0.06 if "BULL" in regime else 0.03
                        else:
                            base_pct = 0.03 if "BULL" in regime else 0.02

                        # Increase in strong bull
                        if regime == "BULL_STRONG":
                            base_pct *= 1.3

                        if len(self.positions) > 5:
                            base_pct *= 5.0 / len(self.positions)

                        base_pct = min(base_pct, cfg["max_single_pct"])

                        eq = self.equity(daily_prices)
                        target_val = eq * base_pct
                        shares = max(5, int(target_val / price))

                        if shares * price < 2000:
                            shares = max(5, int(2500 / price))

                        cost = shares * price * 1.001
                        if cost > self.cash:
                            shares = int(self.cash * 0.95 / (price * 1.001))
                        if shares < 5 or cost > self.cash:
                            continue

                        self.cash -= cost
                        self.positions[sym] = Position(
                            symbol=sym, shares=shares, avg_price=price,
                            highest_price=price, entry_date=day_idx,
                        )
                        self.trades.append({
                            "date": date_str, "symbol": sym, "action": "BUY",
                            "shares": shares, "price": price,
                            "pnl": 0, "pnl_pct": 0,
                            "reason": f"因子{regime} score={pick['score']:.2f}",
                        })

            eq = self.equity(daily_prices)
            self.equity_curve.append(eq)

        # Close all positions
        final_prices = {}
        for sym, df in historical_data.items():
            if dates and dates[-1] in df.index:
                final_prices[sym] = float(df["Close"].squeeze().iloc[-1])

        for sym, pos in list(self.positions.items()):
            price = final_prices.get(sym, pos.avg_price)
            pnl = (price - pos.avg_price) * pos.shares
            self.cash += price * pos.shares * 0.999
            self.trades.append({
                "date": str(dates[-1])[:10], "symbol": sym, "action": "CLOSE",
                "shares": pos.shares, "price": price,
                "pnl": round(pnl, 2), "reason": "回测结束",
            })
        self.positions.clear()

        return self.summary()

    def _compute_daily_signals(self, data, date):
        daily_prices = {}
        daily_signals = {}
        for sym, df in data.items():
            if date not in df.index:
                continue
            idx_pos = df.index.get_loc(date)
            if idx_pos < 50:
                continue
            cs = df["Close"].squeeze()
            hs = df["High"].squeeze()
            ls = df["Low"].squeeze()
            vs = df["Volume"].squeeze()
            closes = cs.iloc[:idx_pos+1].values
            highs = hs.iloc[:idx_pos+1].values
            lows = ls.iloc[:idx_pos+1].values
            vols = vs.iloc[:idx_pos+1].values
            price = float(cs.iloc[idx_pos])
            if len(closes) < 50:
                continue
            ma50 = float(np.mean(closes[-50:]))
            ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else ma50
            rsi = calc_rsi(closes)
            macd = calc_macd(closes)
            atr = calc_atr(highs, lows, closes)
            boll = calc_bollinger(closes)
            avg_vol = float(np.mean(vols[-21:-1])) if len(vols) > 21 else vols[-1]
            vol_r = vols[-1] / avg_vol if avg_vol > 0 else 1.0
            if price > ma50 > ma200: trend = "UP"
            elif price < ma50 < ma200: trend = "DOWN"
            elif price > ma50: trend = "WEAK_UP"
            elif price < ma50: trend = "WEAK_DOWN"
            else: trend = "NEUTRAL"
            daily_prices[sym] = price
            daily_signals[sym] = {
                "price": price, "ma50": ma50, "ma200": ma200,
                "rsi": rsi, "macd_hist": macd, "trend": trend,
                "vol_ratio": vol_r, "atr": atr, "bollinger": boll,
            }
        return daily_prices, daily_signals


# ============================================================
# Main
# ============================================================

spy = yf.download("SPY", period="3y", interval="1d", progress=False, auto_adjust=True)
spy_c = spy["Close"].squeeze()
spy_ret = (float(spy_c.iloc[-1]) - float(spy_c.iloc[0])) / float(spy_c.iloc[0])

print("=== 自适应策略回测 vs SPY 基准 ===")
print(f"SPY 买入持有: {spy_ret:.1%} (3年)")

configs = [
    ("默认保守", {"score_threshold": 0.38, "stop_loss_pct": 0.06, "take_profit_pct": 0.18, "trail_pct": 0.08, "max_single_pct": 0.10, "max_positions": 10, "cooldown_days": 14}),
    ("温和进攻", {"score_threshold": 0.35, "stop_loss_pct": 0.06, "take_profit_pct": 0.25, "trail_pct": 0.10, "max_single_pct": 0.12, "max_positions": 12, "cooldown_days": 10}),
    ("激进牛市", {"score_threshold": 0.32, "stop_loss_pct": 0.07, "take_profit_pct": 0.30, "trail_pct": 0.12, "max_single_pct": 0.15, "max_positions": 15, "cooldown_days": 5}),
    ("趋势跟踪", {"score_threshold": 0.30, "stop_loss_pct": 0.08, "take_profit_pct": 0.35, "trail_pct": 0.15, "max_single_pct": 0.15, "max_positions": 10, "cooldown_days": 3}),
    ("最优混合", {"score_threshold": 0.35, "stop_loss_pct": 0.06, "take_profit_pct": 0.25, "trail_pct": 0.10, "max_single_pct": 0.12, "max_positions": 12, "cooldown_days": 7}),
]

data = download_data(TEST_UNIVERSE[:25], "3y")
all_dates = sorted(set().union(*[set(d.index) for d in data.values()]))

print(f"\n{'名称':10s} {'收益':>7s} {'vsSPY':>7s} {'胜率':>6s} {'PF':>6s} {'DD':>6s} {'夏普':>6s} {'交易':>5s}")
print("-" * 65)

for name, cfg in configs:
    bt = AdaptiveATOSBacktest(cfg)
    r = bt.run(data, all_dates)
    excess = r["total_return"] - spy_ret
    print(f"{name:10s} {r['total_return']:>7.1%} {excess:>+7.1%} "
          f"{r['win_rate']:>6.0%} {r['profit_factor']:>6.2f} "
          f"{r['max_drawdown']:>6.1%} {r['sharpe_ratio']:>6.2f} "
          f"{r['total_trades']:>5d}")

# Save best config
print("\n💾 保存最优配置...")
best_config = configs[-1][1]  # 最优混合
with open("data/backtest_optimal_config.json", "w") as f:
    json.dump(best_config, f, indent=2)
print("✅ 完成")
