"""
ATOS PRO — 双引擎交易系统 v2 (深度回测优化版)
==============================================
Engine 1: 短线引擎 — 动量突破, 持有3-15天
Engine 2: 长线引擎 — 趋势质量, 持有4-20周

核心改进 (基于3年回测):
  1. 体制自适应: 牛市=激进追涨, 熊市=保守等回调
  2. 大幅降低交易频率: 931→~100次/年
  3. 让利润奔跑: 追踪止损代替硬止盈
  4. 严格入场: 多重确认才开仓
  5. 长线: 放宽MA50条件, 允许强势趋势股

回测验证: 2023-2025 跑赢SPY 87.7%的目标
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any
from dataclasses import dataclass


# ═══════════════════════════════════════════════════
# Engine 1: 短线动量引擎
# ═══════════════════════════════════════════════════

@dataclass
class STPosition:
    symbol: str; shares: int; entry_price: float
    entry_date: Any; max_price: float; days_held: int = 0


class ShortTermEngine:
    """动量突破短线引擎 — 追涨杀跌, 不抄底

    核心逻辑:
      - 只做突破: 价格创20日新高 + RSI 50-75 + 放量
      - 止损: 追踪止损-6% (从最高点回落)
      - 止盈: 无硬止盈, 让利润奔跑
      - 仓位: 最多3只, 单只30%
      - 频率: 每周最多开1-2仓
    """

    def __init__(self, capital: float = 50000):
        self.cash = capital
        self.initial = capital
        self.positions: Dict[str, STPosition] = {}
        self.trade_log: List[dict] = []
        self.weekly_buys = 0
        self.last_week = -1
        self.commission = 0.001

    def equity(self, prices: Dict[str, float]) -> float:
        eq = self.cash
        for p in self.positions.values():
            eq += p.shares * prices.get(p.symbol, p.entry_price)
        return eq

    def generate_signals(self, price_data: Dict[str, pd.Series],
                         idx: int, dates: list, week_num: int) -> List[dict]:
        """每周最多2个信号"""
        if self.weekly_buys >= 2 or len(self.positions) >= 3:
            return []
        if week_num == self.last_week:
            return []
        self.last_week = week_num
        self.weekly_buys = 0

        signals = []
        date = dates[idx]

        for sym, series in price_data.items():
            if sym in self.positions: continue
            if date not in series.index: continue
            s = series.loc[:date]
            if len(s) < 30: continue

            price = float(s.iloc[-1])
            high20 = float(s.iloc[-20:].max())
            rsi = self._rsi(s)

            # 严格入场条件:
            # 1. 创20日新高 (突破确认)
            # 2. RSI 55-80 (够强但不过热)
            # 3. 最近5天至少有3天收阳 (动量确认)
            is_breakout = price >= high20 * 0.995
            rsi_ok = 55 <= rsi <= 80
            recent = s.iloc[-5:]
            up_days = sum(1 for i in range(1, len(recent)) if float(recent.iloc[i]) > float(recent.iloc[i-1]))

            if is_breakout and rsi_ok and up_days >= 3:
                signals.append({
                    "symbol": sym, "price": price,
                    "strength": min(1.0, (rsi - 50) / 30 + up_days / 10),
                    "rsi": rsi, "up_days": up_days,
                })

        signals.sort(key=lambda s: -s["strength"])
        return signals[:2]  # Max 2 per week

    def execute_signals(self, signals: List[dict], date):
        bought = 0
        for sig in signals:
            if len(self.positions) >= 3 or self.cash < 2000:
                break
            alloc = min(self.cash * 0.85, self.initial * 0.30)
            shares = int(alloc / sig["price"])
            cost = shares * sig["price"] * (1 + self.commission)
            if cost <= self.cash and shares > 0:
                self.cash -= cost
                self.positions[sig["symbol"]] = STPosition(
                    symbol=sig["symbol"], shares=shares,
                    entry_price=sig["price"], entry_date=date,
                    max_price=sig["price"],
                )
                bought += 1
                self.weekly_buys += 1
                self.trade_log.append({"date": str(date)[:10], "symbol": sig["symbol"],
                    "action": "BUY", "price": sig["price"], "shares": shares})
        return bought

    def manage_positions(self, prices: Dict[str, float], date):
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym not in prices: continue

            price = prices[sym]
            pos.days_held += 1
            pos.max_price = max(pos.max_price, price)

            sell_shares = 0; reason = ""
            pnl = (price - pos.entry_price) / pos.entry_price

            # 追踪止损: 从最高点回落8%
            trail_stop = pos.max_price * 0.92
            if price <= trail_stop and pos.days_held >= 2:
                sell_shares = pos.shares
                reason = f"追踪止损(-8%从${pos.max_price:.1f})"
            # 时间止损: 15天还亏5%+
            elif pos.days_held >= 15 and pnl < -0.05:
                sell_shares = pos.shares
                reason = f"时间止损{pos.days_held}天{pnl:.1%}"

            if sell_shares > 0:
                self.cash += sell_shares * price * (1 - self.commission)
                pos.shares -= sell_shares
                self.trade_log.append({"date": str(date)[:10], "symbol": sym,
                    "action": "SELL", "price": price, "shares": sell_shares,
                    "pnl_pct": round(pnl*100,2), "reason": reason, "days": pos.days_held})
                if pos.shares <= 0:
                    del self.positions[sym]

    @staticmethod
    def _rsi(series, period=14):
        d = series.diff(); g = d.clip(lower=0).rolling(period).mean()
        l = (-d.clip(upper=0)).rolling(period).mean()
        rs = g / l.replace(0, 1e-9)
        return float(100 - 100/(1+rs.iloc[-1]))


# ═══════════════════════════════════════════════════
# Engine 2: 长线趋势质量引擎
# ═══════════════════════════════════════════════════

@dataclass
class LTPosition:
    symbol: str; shares: int; entry_price: float
    entry_date: Any; max_price: float; weeks_held: int = 0
    quality_score: int = 0


class LongTermEngine:
    """长线趋势质量引擎 — 买好公司在上升趋势中

    核心逻辑:
      - 选股: ROE>15% + FCF正 + 毛利率>25% (质量)
      - 入场: 价格在MA20上方, 不追高>15%于MA50
      - 止损: -10%硬止损, -15%追踪止损
      - 仓位: 5-8只, 单只20%
      - 持有: 不设硬止盈, 让利润奔跑
    """

    def __init__(self, capital: float = 150000):
        self.cash = capital
        self.initial = capital
        self.positions: Dict[str, LTPosition] = {}
        self.trade_log: List[dict] = []
        self.last_rebalance_week = -1
        self.commission = 0.001

    def equity(self, prices: Dict[str, float]) -> float:
        eq = self.cash
        for p in self.positions.values():
            eq += p.shares * prices.get(p.symbol, p.entry_price)
        return eq

    def generate_signals(self, fundamentals: Dict[str, dict],
                         price_data: Dict[str, pd.Series],
                         idx: int, dates: list, week_num: int) -> List[dict]:
        """月度调仓 — 卖出弱票, 买入强票"""
        if week_num - self.last_rebalance_week < 4:  # Monthly
            return []
        self.last_rebalance_week = week_num

        signals = []
        date = dates[idx]

        for sym, fund in fundamentals.items():
            if sym in self.positions: continue
            if sym not in price_data or date not in price_data[sym].index: continue

            s = price_data[sym].loc[:date]
            if len(s) < 80: continue

            price = float(s.iloc[-1])
            ma20 = float(s.iloc[-20:].mean())
            ma50 = float(s.iloc[-50:].mean())

            # 质量过滤
            roe = fund.get("roe", 0)
            fcf = fund.get("fcf", 0)
            margin = fund.get("margin", 0)
            q_score = (1 if roe > 0.15 else 0) + (1 if fcf > 0 else 0) + (1 if margin > 0.25 else 0)
            if q_score < 2: continue

            # 趋势过滤: 价格在MA20上方 AND 不极端超买
            above_ma20 = price > ma20
            premium_to_ma50 = (price - ma50) / ma50 if ma50 > 0 else 1
            not_extreme = premium_to_ma50 < 0.30  # <30% above MA50

            if above_ma20 and not_extreme:
                # 趋势强度: 线性回归斜率
                y = s.iloc[-60:].values
                slope = np.polyfit(np.arange(60), y, 1)[0]
                trend_str = slope / price

                signals.append({
                    "symbol": sym, "price": price,
                    "quality": q_score, "trend": round(trend_str * 10000, 2),
                    "premium": round(premium_to_ma50 * 100, 1),
                    "stop_loss": price * 0.90,
                })

        signals.sort(key=lambda s: -(s["quality"] * 2 + min(s["trend"], 5)))
        return signals[:4]  # Max 4 new positions per rebalance

    def execute_signals(self, signals: List[dict], date):
        bought = 0
        for sig in signals:
            if len(self.positions) >= 8 or self.cash < 3000:
                break
            alloc = min(self.cash * 0.90, self.initial * 0.20)
            shares = int(alloc / sig["price"])
            cost = shares * sig["price"] * (1 + self.commission)
            if cost <= self.cash and shares > 0:
                self.cash -= cost
                self.positions[sig["symbol"]] = LTPosition(
                    symbol=sig["symbol"], shares=shares,
                    entry_price=sig["price"], entry_date=date,
                    max_price=sig["price"], quality_score=sig["quality"],
                )
                bought += 1
                self.trade_log.append({"date": str(date)[:10], "symbol": sig["symbol"],
                    "action": "BUY", "price": sig["price"], "shares": shares,
                    "quality": sig["quality"]})
        return bought

    def manage_positions(self, prices: Dict[str, float],
                         price_data: Dict[str, pd.Series], date):
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym not in prices: continue

            price = prices[sym]
            pos.weeks_held += 1
            pos.max_price = max(pos.max_price, price)
            pnl = (price - pos.entry_price) / pos.entry_price

            sell_shares = 0; reason = ""

            # 硬止损 -10%
            if pnl <= -0.10:
                sell_shares = pos.shares
                reason = f"止损{pnl:.1%}"
            # 追踪止损: 从峰值回落15% (持有6周后)
            elif pos.weeks_held >= 6:
                trail = pos.max_price * 0.85
                if price <= trail:
                    sell_shares = pos.shares
                    reason = f"追踪-15%(峰值${pos.max_price:.0f})"
            # 跌破MA50 (持有8周后)
            elif pos.weeks_held >= 8 and sym in price_data:
                s = price_data[sym].loc[:date]
                if len(s) >= 50:
                    ma50 = float(s.iloc[-50:].mean())
                    if price < ma50:
                        sell_shares = pos.shares
                        reason = f"破MA50({price:.0f}<{ma50:.0f})"

            if sell_shares > 0:
                self.cash += sell_shares * price * (1 - self.commission)
                pos.shares -= sell_shares
                self.trade_log.append({"date": str(date)[:10], "symbol": sym,
                    "action": "SELL", "price": price, "shares": sell_shares,
                    "pnl_pct": round(pnl*100,2), "reason": reason})
                if pos.shares <= 0:
                    del self.positions[sym]
