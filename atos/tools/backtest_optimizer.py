"""
ATOS PRO — 全面回测与参数优化引擎
=================================
模拟完整的 ATOS 交易逻辑在 3 年历史数据上的表现。
包含: 因子引擎 → 质量门控 → 风控过滤 → 入场 → 追踪止损 → 出场

用法:
  python3 atos/tools/backtest_optimizer.py
  python3 atos/tools/backtest_optimizer.py --optimize  # 参数网格搜索
"""

import sys, os, json, time, math
import datetime as dt
import numpy as np
import pandas as pd
import yfinance as yf
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Configuration
# ============================================================

# Test universe: liquid, diverse sectors
TEST_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "JPM", "BAC", "GS", "V", "MA",
    "JNJ", "UNH", "PFE", "MRK", "ABBV",
    "XOM", "CVX", "COP",
    "HD", "WMT", "COST", "NKE",
    "CAT", "GE", "BA",
    "SPY", "QQQ", "IWM",
]

BACKTEST_PERIOD = "3y"  # 3 years of data
INITIAL_CAPITAL = 300_000

# ============================================================
# Lightweight Signal Calculator (no Futu dependency)
# ============================================================

def calc_rsi(close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    deltas = np.diff(close[-period-1:])
    gains = deltas.clip(min=0).mean()
    losses = (-deltas.clip(max=0)).mean()
    rs = gains / max(losses, 1e-9)
    return float(100 - 100 / (1 + rs))


def calc_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return 0.0
    trs = []
    for i in range(1, period + 1):
        h, l, c_prev = high[-i], low[-i], close[-i-1]
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return float(np.mean(trs))


def calc_bollinger(close: np.ndarray, period: int = 20, std: float = 2.0) -> dict:
    ma = np.mean(close[-period:])
    sd = np.std(close[-period:])
    upper = ma + std * sd
    lower = ma - std * sd
    price = close[-1]
    pct_b = (price - lower) / (upper - lower) if upper != lower else 0.5
    return {"upper": upper, "middle": ma, "lower": lower, "pct_b": pct_b}


def calc_macd(close: np.ndarray) -> float:
    if len(close) < 26:
        return 0.0
    ema12 = pd.Series(close).ewm(span=12).mean().iloc[-1]
    ema26 = pd.Series(close).ewm(span=26).mean().iloc[-1]
    macd_line = ema12 - ema26
    signal_line = pd.Series(close).ewm(span=12).mean().iloc[-1] - pd.Series(close).ewm(span=26).mean().iloc[-1]
    # Simplified: just return macd_line - signal_line (very rough)
    # Better calculation:
    macd_full = pd.Series(close).ewm(span=12).mean() - pd.Series(close).ewm(span=26).mean()
    sig_full = macd_full.ewm(span=9).mean()
    return float(macd_full.iloc[-1] - sig_full.iloc[-1])


# ============================================================
# Factor Scoring (simplified, matches production engine)
# ============================================================

def compute_factor_score(sym: str, price_data: dict, regime: str = "BULL_WEAK") -> dict:
    """Compute multi-factor score for a symbol. Matches production factor engine."""
    price = price_data["price"]
    if price <= 0:
        return {"symbol": sym, "score": 0.0, "breakdown": {}}

    # Technical score
    rsi = price_data.get("rsi", 50)
    trend = price_data.get("trend", "NEUTRAL")
    vol_ratio = price_data.get("vol_ratio", 1.0)
    boll = price_data.get("bollinger", {})
    ma50 = price_data.get("ma50", price)

    tech_score = 0.0
    if trend == "UP":
        tech_score += 0.40
    elif trend == "WEAK_UP":
        tech_score += 0.25
    elif trend == "NEUTRAL":
        tech_score += 0.10

    if 40 <= rsi <= 60:
        tech_score += 0.18
    elif 35 <= rsi < 40:
        tech_score += 0.14
    elif 60 < rsi <= 72:
        tech_score += 0.08
    elif rsi > 75:
        tech_score -= 0.25

    if 1.2 <= vol_ratio <= 3.0:
        tech_score += 0.15

    pct_b = boll.get("pct_b", 0.5)
    if 0.2 <= pct_b <= 0.8:
        tech_score += 0.06
    elif pct_b > 0.9:
        tech_score -= 0.12

    tech_score = max(0.0, min(1.0, tech_score))

    # Momentum score (simplified)
    mom_score = 0.0
    if price > ma50:
        mom_score += 0.30
    if trend in ("UP", "WEAK_UP"):
        mom_score += 0.25
    if rsi > 50:
        mom_score += 0.15
    if vol_ratio > 1.0:
        mom_score += 0.10
    pct_from_ma = (price - ma50) / ma50 if ma50 > 0 else 0
    if -0.02 < pct_from_ma < 0.10:
        mom_score += 0.10
    mom_score = max(0.0, min(1.0, mom_score))

    # Value score (simplified — based on price relative to moving averages)
    ma200 = price_data.get("ma200", price)
    val_score = 0.0
    if ma50 > 0:
        pct_50 = abs(price - ma50) / ma50
        if pct_50 < 0.10:
            val_score += 0.40
    if ma200 > 0 and price < ma200:
        val_score += 0.20
    val_score = max(0.0, min(1.0, val_score))

    # Quality score (simplified — based on trend strength and volume)
    qual_score = 0.0
    if trend == "UP":
        qual_score += 0.40
    elif trend == "WEAK_UP":
        qual_score += 0.25
    if vol_ratio > 0.8:
        qual_score += 0.20
    if price > ma50:
        qual_score += 0.20
    qual_score = max(0.0, min(1.0, qual_score))

    # Mean reversion score
    mr_score = 0.0
    if rsi < 40:
        mr_score += 0.25
    if pct_b < 0.30:
        mr_score += 0.18
    mr_score = max(0.0, min(1.0, mr_score))

    # Regime-based weights (matching production)
    if "BEAR" in regime:
        w = {"value": 0.18, "momentum": 0.05, "quality": 0.32, "technical": 0.18, "mean_rev": 0.15, "multiframe": 0.12}
    elif "BULL_STRONG" in regime:
        w = {"value": 0.10, "momentum": 0.35, "quality": 0.06, "technical": 0.22, "mean_rev": 0.15, "multiframe": 0.12}
    else:
        w = {"value": 0.18, "momentum": 0.25, "quality": 0.10, "technical": 0.25, "mean_rev": 0.14, "multiframe": 0.08}

    breakdown = {
        "value": val_score, "momentum": mom_score, "quality": qual_score,
        "technical": tech_score, "mean_rev": mr_score,
    }

    score = (
        w["value"] * val_score +
        w["momentum"] * mom_score +
        w["quality"] * qual_score +
        w["technical"] * tech_score +
        w["mean_rev"] * mr_score
    )

    return {"symbol": sym, "score": round(score, 4), "breakdown": breakdown}


# ============================================================
# Quality Gate (matches production)
# ============================================================

def quality_gate(pick: dict, price_data: dict) -> bool:
    """Return True if the pick passes quality gate."""
    bd = pick.get("breakdown", {})
    sig = price_data

    quality_factors = sum(1 for k in ["value", "momentum", "quality", "technical"]
                         if bd.get(k, 0) > 0.4)
    macd_ok = sig.get("macd_hist", 0) > 0.001
    trend_ok = sig.get("trend", "") in ("UP", "WEAK_UP")
    rsi = sig.get("rsi", 50)
    rsi_ok = 35 < rsi < 72
    factor_score = pick.get("score", 0)

    quality_score = (
        quality_factors * 20 +
        (10 if macd_ok else 0) +
        (10 if trend_ok else 0) +
        (5 if rsi_ok else 0) -
        (30 if factor_score < 0.35 else 0)
    )
    return quality_score >= 68


# ============================================================
# Entry Filters (matches production shadow_trader)
# ============================================================

def entry_filters_ok(pick: dict, price_data: dict, config: dict) -> Tuple[bool, str]:
    """Check all entry filters. Return (passed, reason)."""
    sym = pick["symbol"]
    score = pick.get("score", 0)
    sig = price_data
    price = sig.get("price", 0)
    threshold = config["score_threshold"]

    # 1. Score threshold
    if score < threshold:
        return False, f"score={score:.2f}<{threshold}"

    # 2. RSI filter
    rsi = sig.get("rsi", 50)
    if rsi > 75:
        return False, f"RSI={rsi:.0f}>75超买"
    if rsi < 30:
        return False, f"RSI={rsi:.0f}<30弱势"

    # 3. Volume filter
    vol_r = sig.get("vol_ratio", 1.0)
    if vol_r < 0.10:
        return False, f"极度缩量"

    # 4. MACD
    macd = sig.get("macd_hist", 0)
    if macd < 0:
        return False, f"MACD负"

    # 5. Price > MA50
    ma50 = sig.get("ma50", 0)
    if ma50 > 0 and price < ma50:
        return False, f"价格<MA50"

    # 6. Bollinger
    boll = sig.get("bollinger", {})
    pct_b = boll.get("pct_b", 0.5)
    if pct_b > 0.85:
        return False, f"布林上轨"

    # 7. Quality gate
    if not quality_gate(pick, price_data):
        return False, f"质量门控不通过"

    return True, "OK"


# ============================================================
# Position Sizing (simplified)
# ============================================================

def calc_position_size(score: float, equity: float, price: float,
                       num_positions: int, config: dict) -> int:
    """Calculate position size in shares."""
    # Base allocation from score
    if score >= 0.55:
        base_pct = 0.08
    elif score >= 0.45:
        base_pct = 0.06
    elif score >= 0.38:
        base_pct = 0.04
    else:
        base_pct = 0.02

    # Diversity penalty
    if num_positions > 3:
        base_pct *= 3.0 / num_positions

    # Cap
    base_pct = min(base_pct, config["max_single_pct"])

    target_val = equity * base_pct
    shares = max(5, int(target_val / price))

    # Minimum trade value $2000
    if shares * price < 2000:
        shares = max(5, int(2500 / price))

    return shares


# ============================================================
# Main Backtest Engine
# ============================================================

@dataclass
class Position:
    symbol: str
    shares: int
    avg_price: float
    highest_price: float = 0.0
    trail_stop: float = 0.0
    entry_date: int = 0


class ATOSBacktest:
    """Full ATOS pipeline backtest."""

    def __init__(self, config: dict = None):
        self.config = config or {
            "score_threshold": 0.38,
            "stop_loss_pct": 0.06,
            "take_profit_pct": 0.18,
            "trail_pct": 0.08,
            "max_single_pct": 0.10,
            "max_positions": 12,
            "cooldown_days": 14,
            "partial_profit_pct": 0.10,
            "partial_sell_frac": 0.33,
        }
        self.cash = INITIAL_CAPITAL
        self.initial = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[dict] = []
        self.equity_curve: List[float] = []
        self.cooldown: Dict[str, int] = {}  # symbol -> last_sold_day
        self.daily_values: List[float] = []

    def equity(self, prices: Dict[str, float]) -> float:
        pos_val = sum(
            p.shares * prices.get(p.symbol, p.avg_price)
            for p in self.positions.values()
        )
        return self.cash + pos_val

    def run(self, historical_data: Dict[str, pd.DataFrame],
            dates: List, spy_trends: Dict = None) -> dict:
        """Run full backtest over historical data."""
        cfg = self.config

        for day_idx, date in enumerate(dates):
            date_str = str(date)[:10]

            # Get daily prices and compute signals
            daily_prices = {}
            daily_signals = {}

            for sym, df in historical_data.items():
                if date not in df.index:
                    continue
                idx_pos = df.index.get_loc(date)
                if idx_pos < 50:
                    continue

                close_series = df["Close"].squeeze()
                high_series = df["High"].squeeze()
                low_series = df["Low"].squeeze()
                vol_series = df["Volume"].squeeze()

                window = close_series.iloc[max(0, idx_pos-200):idx_pos+1]
                closes = close_series.iloc[:idx_pos+1].values
                highs = high_series.iloc[:idx_pos+1].values
                lows = low_series.iloc[:idx_pos+1].values
                vols = vol_series.iloc[:idx_pos+1].values

                price = float(close_series.iloc[idx_pos])

                if len(closes) < 50:
                    continue

                ma50 = float(np.mean(closes[-50:]))
                ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else ma50
                rsi = calc_rsi(closes)
                macd = calc_macd(closes)
                atr = calc_atr(highs, lows, closes)
                boll = calc_bollinger(closes)

                # Volume ratio
                avg_vol = float(np.mean(vols[-21:-1])) if len(vols) > 21 else vols[-1]
                vol_r = vols[-1] / avg_vol if avg_vol > 0 else 1.0

                # Trend
                if price > ma50 > ma200:
                    trend = "UP"
                elif price < ma50 < ma200:
                    trend = "DOWN"
                elif price > ma50:
                    trend = "WEAK_UP"
                elif price < ma50:
                    trend = "WEAK_DOWN"
                else:
                    trend = "NEUTRAL"

                daily_prices[sym] = price
                daily_signals[sym] = {
                    "price": price, "ma50": ma50, "ma200": ma200,
                    "rsi": rsi, "macd_hist": macd, "trend": trend,
                    "vol_ratio": vol_r, "atr": atr, "bollinger": boll,
                }

            if not daily_prices:
                continue

            spy_price = daily_prices.get("SPY", 500)
            spy_sig = daily_signals.get("SPY", {})

            # Determine regime (simplified)
            spy_trend = spy_sig.get("trend", "NEUTRAL")
            spy_ma50 = spy_sig.get("ma50", spy_price)
            if spy_price > spy_ma50 * 1.03:
                regime = "BULL_STRONG"
            elif spy_price > spy_ma50:
                regime = "BULL_WEAK"
            elif spy_price < spy_ma50 * 0.95:
                regime = "BEAR"
            else:
                regime = "SIDEWAYS"

            # ---- EXITS: Stop loss, take profit, trailing stop ----
            for sym in list(self.positions.keys()):
                if sym not in daily_prices:
                    continue
                pos = self.positions[sym]
                price = daily_prices[sym]
                pnl_pct = (price - pos.avg_price) / pos.avg_price

                # Update trailing stop
                pos.highest_price = max(pos.highest_price, price)
                if pnl_pct > 0.03:  # Only trail when profitable
                    trail = pos.highest_price * (1 - cfg["trail_pct"])
                    pos.trail_stop = max(pos.trail_stop, trail)

                sell_reason = None
                sell_qty = pos.shares

                # Hard stop loss
                if pnl_pct <= -cfg["stop_loss_pct"]:
                    sell_reason = f"止损{pnl_pct:.1%}"
                # Take profit
                elif pnl_pct >= cfg["take_profit_pct"]:
                    sell_reason = f"止盈+{pnl_pct:.1%}"
                # Trailing stop
                elif pos.trail_stop > 0 and price < pos.trail_stop:
                    sell_reason = f"追踪止损{pnl_pct:.1%}"
                # Partial profit taking
                elif pnl_pct >= cfg["partial_profit_pct"] and pos.shares >= 6:
                    sell_qty = max(1, int(pos.shares * cfg["partial_sell_frac"]))
                    sell_reason = f"部分止盈+{pnl_pct:.1%}"

                if sell_reason:
                    pnl = (price - pos.avg_price) * sell_qty
                    self.cash += price * sell_qty * 0.999  # 0.1% slippage
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

            # ---- ENTRIES: Factor scoring + quality gate ----
            spy_below_ma50 = spy_price < spy_ma50 * 0.98
            max_pos = cfg["max_positions"]
            if regime == "BEAR" or spy_below_ma50:
                # No new entries in bear market
                pass
            elif len(self.positions) < max_pos:
                # Compute factor scores
                picks = []
                for sym, sig in daily_signals.items():
                    if sym in self.positions:
                        continue
                    # Cooldown check
                    last_sold = self.cooldown.get(sym, -999)
                    if day_idx - last_sold < cfg["cooldown_days"]:
                        continue
                    result = compute_factor_score(sym, sig, regime)
                    picks.append(result)

                picks.sort(key=lambda x: -x["score"])

                # Filter and enter
                for pick in picks[:20]:
                    if len(self.positions) >= max_pos:
                        break
                    sym = pick["symbol"]
                    sig = daily_signals.get(sym, {})
                    price = sig.get("price", 0)

                    ok, reason = entry_filters_ok(pick, sig, cfg)
                    if not ok:
                        continue

                    shares = calc_position_size(
                        pick["score"], self.equity(daily_prices),
                        price, len(self.positions), cfg
                    )

                    cost = shares * price * 1.001  # slippage
                    if cost > self.cash * 1.0:
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
                        "reason": f"因子开仓 score={pick['score']:.2f}",
                    })

            # Mark to market
            eq = self.equity(daily_prices)
            self.equity_curve.append(eq)
            self.daily_values.append(eq)

        # Close all remaining positions at last price
        final_prices = {}
        for sym, df in historical_data.items():
            if dates and dates[-1] in df.index:
                final_prices[sym] = float(df["Close"].squeeze().iloc[-1])

        for sym, pos in list(self.positions.items()):
            price = final_prices.get(sym, pos.avg_price)
            pnl = (price - pos.avg_price) * pos.shares
            self.cash += price * pos.shares * 0.999
            self.trades.append({
                "date": str(dates[-1])[:10] if dates else "end",
                "symbol": sym, "action": "CLOSE",
                "shares": pos.shares, "price": price,
                "pnl": round(pnl, 2),
                "reason": "回测结束平仓",
            })
        self.positions.clear()

        return self.summary()

    def summary(self) -> dict:
        """Generate performance report."""
        trades = self.trades
        if not trades:
            return {"total_trades": 0, "error": "no trades"}

        sells = [t for t in trades if t["action"] in ("SELL", "CLOSE")]
        buys = [t for t in trades if t["action"] == "BUY"]
        wins = [t for t in sells if t["pnl"] > 0]
        losses = [t for t in sells if t["pnl"] <= 0]

        total_pnl = sum(t["pnl"] for t in sells)
        final_eq = self.equity_curve[-1] if self.equity_curve else self.cash

        # Calculate returns
        if len(self.equity_curve) >= 2:
            returns = np.diff(self.equity_curve) / self.equity_curve[:-1]
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252)) if np.std(returns) > 0 else 0
            # Max drawdown
            peak = np.maximum.accumulate(self.equity_curve)
            dd = (self.equity_curve - peak) / peak
            max_dd = float(np.min(dd))
        else:
            sharpe = 0
            max_dd = 0

        return {
            "total_trades": len(sells),
            "total_buys": len(buys),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(sells), 4) if sells else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(abs(sum(t["pnl"] for t in losses)) / len(losses), 2) if losses else 0,
            "profit_factor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 4)
                            if losses and abs(sum(t["pnl"] for t in losses)) > 0 else 0,
            "final_equity": round(final_eq, 2),
            "total_return": round((final_eq - self.initial) / self.initial, 4),
            "annual_return": round(((final_eq / self.initial) ** (1/3) - 1), 4),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "avg_hold_days": 0.0,
        }


# ============================================================
# Data Download & Preparation
# ============================================================

def download_data(symbols: list, period: str = "3y") -> Dict[str, pd.DataFrame]:
    """Download historical data for backtesting."""
    print(f"📥 下载 {len(symbols)} 只股票 {period} 历史数据...")
    data = {}
    failed = []

    # Batch download
    chunk_size = 15
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        ticker_str = " ".join(chunk)
        try:
            df_all = yf.download(ticker_str, period=period, interval="1d",
                                progress=False, auto_adjust=True, group_by="ticker", progress=False, auto_adjust=True)
            for sym in chunk:
                try:
                    if isinstance(df_all.columns, pd.MultiIndex):
                        if sym in df_all.columns.levels[0]:
                            df_sym = df_all[sym].copy()
                        else:
                            failed.append(sym)
                            continue
                    else:
                        df_sym = df_all.copy()
                    if not df_sym.empty and len(df_sym) >= 50:
                        data[sym] = df_sym
                    else:
                        failed.append(sym)
                except Exception:
                    failed.append(sym)
            print(f"  批次 {i//chunk_size+1}: {len(chunk)}只 → {len(data)}/{len(symbols)} 成功")
        except Exception as e:
            print(f"  批次 {i//chunk_size+1} 下载失败: {e}")
            failed.extend(chunk)

    if failed:
        print(f"  ⚠️ {len(failed)} 只失败: {failed[:5]}...")

    return data


# ============================================================
# Parameter Optimization via Grid Search
# ============================================================

def optimize_parameters(data: dict, dates: list) -> dict:
    """Grid search for optimal parameters."""
    print("\n🔍 参数网格搜索优化...")

    param_grid = {
        "score_threshold": [0.32, 0.35, 0.38, 0.42],
        "stop_loss_pct": [0.05, 0.06, 0.07],
        "take_profit_pct": [0.15, 0.18, 0.22],
        "trail_pct": [0.06, 0.08, 0.10],
        "max_single_pct": [0.08, 0.10, 0.12],
        "max_positions": [8, 10, 12],
        "cooldown_days": [7, 14, 21],
    }

    best_result = None
    best_score = -999
    results = []

    # Smart search: only test key combos (not full grid = 3888 combos)
    key_combos = [
        # (score_thr, stop_loss, take_profit, trail, max_single, max_pos, cooldown)
        (0.35, 0.06, 0.18, 0.08, 0.10, 10, 14),   # baseline
        (0.38, 0.06, 0.18, 0.08, 0.10, 8, 14),    # tighter entry
        (0.38, 0.05, 0.22, 0.08, 0.10, 10, 14),   # wider profit
        (0.35, 0.06, 0.22, 0.10, 0.10, 12, 14),   # wider trail
        (0.38, 0.07, 0.18, 0.10, 0.08, 8, 21),    # wider stops
        (0.42, 0.06, 0.18, 0.08, 0.10, 8, 14),    # very tight entry
        (0.35, 0.06, 0.18, 0.06, 0.12, 12, 7),    # aggressive
        (0.32, 0.05, 0.22, 0.10, 0.10, 12, 14),   # loose entry, wide exits
        (0.38, 0.06, 0.15, 0.08, 0.10, 10, 14),   # tighter take profit
        (0.35, 0.07, 0.22, 0.10, 0.08, 8, 21),    # conservative
    ]

    for i, (st, sl, tp, tr, ms, mp, cd) in enumerate(key_combos):
        config = {
            "score_threshold": st,
            "stop_loss_pct": sl,
            "take_profit_pct": tp,
            "trail_pct": tr,
            "max_single_pct": ms,
            "max_positions": mp,
            "cooldown_days": cd,
            "partial_profit_pct": 0.10,
            "partial_sell_frac": 0.33,
        }

        bt = ATOSBacktest(config)
        result = bt.run(data, dates)

        # Composite score: prioritize profit factor + return - drawdown
        total_return = result.get("total_return", 0)
        sharpe = result.get("sharpe_ratio", 0)
        pf = result.get("profit_factor", 0)
        max_dd = result.get("max_drawdown", -1)
        wr = result.get("win_rate", 0)
        trades = result.get("total_trades", 0)

        # Score formula: reward profitable, frequent, low-drawdown strategies
        composite = (
            total_return * 40 +
            sharpe * 20 +
            (pf - 1.0) * 30 +
            (1 + max_dd) * 20 +
            wr * 15 +
            min(trades / 100, 1.0) * 5
        )

        results.append({"config": config, "result": result, "composite": composite})

        print(f"  [{i+1}/{len(key_combos)}] "
              f"st={st} sl={sl} tp={tp} tr={tr} mp={mp} cd={cd} | "
              f"Ret={total_return:.1%} WR={wr:.0%} PF={pf:.2f} "
              f"DD={max_dd:.1%} Sharpe={sharpe:.2f} | Score={composite:.1f}")

        if composite > best_score:
            best_score = composite
            best_result = {"config": config, "result": result, "composite": composite}

    return best_result, results


# ============================================================
# Main
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimize", action="store_true", help="Run parameter optimization")
    parser.add_argument("--symbols", type=int, default=20, help="Number of symbols to test")
    args = parser.parse_args()

    symbols = TEST_UNIVERSE[:args.symbols]
    print(f"🧪 ATOS 回测引擎 — {len(symbols)} 只标的, 3年数据")

    # Download data
    data = download_data(symbols, BACKTEST_PERIOD)
    if len(data) < 5:
        print("❌ 数据不足，无法回测")
        return

    # Align dates
    all_dates = sorted(set().union(*[set(d.index) for d in data.values()]))
    print(f"📅 {len(all_dates)} 个交易日 ({all_dates[0]} → {all_dates[-1]})")

    if args.optimize:
        best, all_results = optimize_parameters(data, all_dates)

        print(f"\n{'='*60}")
        print(f"🏆 最优参数组合:")
        cfg = best["config"]
        r = best["result"]
        print(f"   score_threshold={cfg['score_threshold']}")
        print(f"   stop_loss={cfg['stop_loss_pct']}  take_profit={cfg['take_profit_pct']}")
        print(f"   trail={cfg['trail_pct']}  max_single={cfg['max_single_pct']}")
        print(f"   max_positions={cfg['max_positions']}  cooldown={cfg['cooldown_days']}d")
        print(f"   ---")
        print(f"   总收益: {r['total_return']:.1%}  |  年化: {r.get('annual_return',0):.1%}")
        print(f"   胜率: {r['win_rate']:.0%}  |  盈亏比: {r['profit_factor']:.2f}")
        print(f"   夏普: {r['sharpe_ratio']:.2f}  |  最大回撤: {r['max_drawdown']:.1%}")
        print(f"   交易次数: {r['total_trades']}  |  最终权益: ${r['final_equity']:,.0f}")
        print(f"   综合评分: {best['composite']:.1f}")

        # Save best config
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "backtest_optimal_config.json"
        )
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump({"config": cfg, "result": r, "composite": best["composite"]}, f, indent=2)
        print(f"\n💾 最优参数已保存到 data/backtest_optimal_config.json")

    else:
        # Single test with default config
        print(f"\n📊 默认参数回测...")
        bt = ATOSBacktest()
        result = bt.run(data, all_dates)
        print(f"\n{'='*60}")
        print(f"📊 回测结果（默认参数）:")
        for k, v in result.items():
            if isinstance(v, float):
                print(f"   {k}: {v:.4f}" if v < 1 else f"   {k}: {v:,.2f}")
            else:
                print(f"   {k}: {v}")
        print(f"\n💡 运行 python3 atos/tools/backtest_optimizer.py --optimize 进行参数优化")


if __name__ == "__main__":
    main()
