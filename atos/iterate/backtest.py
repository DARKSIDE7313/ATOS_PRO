"""
ATOS PRO v2 — 回测引擎
=======================
全功能回测：在历史数据上跑完整的交易策略。
验证策略在"过去"能不能赚钱，再去实盘。

支持：
  - 多标的同步回测
  - 滑点模型（市价单0.1%）
  - 费率模型（$0.005/股，最低$1）
  - 完整业绩报告

输出与 core.metrics 兼容的指标。
"""

import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger
from atos.core.metrics import all_metrics, format_report
from atos.market.regime.regime_engine import RegimeEngine

logger = get_logger("iterate.backtest")


class BacktestEngine:
    """
    事件驱动回测引擎。
    """

    def __init__(self, initial_capital: float = 100000.0,
                 commission_per_share: float = 0.005,
                 min_commission: float = 1.0,
                 slippage_pct: float = 0.001):  # 0.1% slippage
        self.initial_capital = initial_capital
        self.commission_per_share = commission_per_share
        self.min_commission = min_commission
        self.slippage_pct = slippage_pct

        self.cash = initial_capital
        self.positions = {}  # {symbol: {qty, avg_price}}
        self.equity_curve = [initial_capital]
        self.trades = []
        self.daily_returns = []

    def _slipped_price(self, price: float, side: str) -> float:
        """计算滑点后的实际成交价"""
        slip = price * self.slippage_pct
        return price + slip if side == "BUY" else price - slip

    def _commission(self, shares: int) -> float:
        """计算佣金"""
        return max(self.min_commission, shares * self.commission_per_share)

    def execute(self, symbol: str, action: str, shares: int,
                price: float, date: str = "", reason: str = ""):
        """执行一笔交易"""
        if shares <= 0:
            return

        fill_price = self._slipped_price(price, action)
        cost = fill_price * shares + self._commission(shares)

        if action == "BUY":
            if cost > self.cash:
                # 买不起，调整数量
                affordable = int((self.cash - self.min_commission) / fill_price)
                if affordable <= 0:
                    return
                shares = affordable
                cost = fill_price * shares + self._commission(shares)

            self.cash -= cost
            if symbol in self.positions:
                old = self.positions[symbol]
                total_shares = old["qty"] + shares
                old_cost = old["qty"] * old["avg_price"]
                self.positions[symbol] = {
                    "qty": total_shares,
                    "avg_price": (old_cost + fill_price * shares) / total_shares,
                }
            else:
                self.positions[symbol] = {"qty": shares, "avg_price": fill_price}

        elif action == "SELL":
            if symbol not in self.positions or self.positions[symbol]["qty"] < shares:
                # 不够卖
                if symbol in self.positions:
                    shares = self.positions[symbol]["qty"]
                else:
                    return

            self.cash += fill_price * shares - self._commission(shares)
            pos = self.positions[symbol]
            pnl = (fill_price - pos["avg_price"]) * shares

            pos["qty"] -= shares
            if pos["qty"] <= 0:
                del self.positions[symbol]

            self.trades.append({
                "date": date,
                "symbol": symbol,
                "action": action,
                "shares": shares,
                "price": round(fill_price, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
            })

    def mark_to_market(self, prices: dict[str, float]):
        """按市价估值"""
        position_value = sum(
            pos["qty"] * prices.get(sym, 0)
            for sym, pos in self.positions.items()
        )
        total = self.cash + position_value
        self.equity_curve.append(total)

        # 日收益率
        if len(self.equity_curve) >= 2:
            prev = self.equity_curve[-2]
            self.daily_returns.append(
                (total - prev) / prev if prev > 0 else 0
            )

    def summary(self) -> dict:
        """生成回测报告"""
        if not self.trades:
            return {"message": "无交易"}

        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self.trades)

        metrics = all_metrics(self.daily_returns, self.equity_curve) \
            if len(self.daily_returns) >= 2 else {}

        return {
            **metrics,
            "total_trades": len(self.trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(abs(sum(t["pnl"] for t in losses)) / len(losses), 2) if losses else 0,
            "final_equity": round(self.equity_curve[-1], 2) if self.equity_curve else self.initial_capital,
            "final_cash": round(self.cash, 2),
            "total_return": round((self.equity_curve[-1] - self.initial_capital) / self.initial_capital, 4),
        }


def run_simple_backtest(symbols: list[str] = None,
                         strategy_fn=None,
                         period: str = "2y",
                         initial_capital: float = 100000.0) -> dict:
    """
    简单回测入口。
    如果不传 strategy_fn，就只用金叉死叉（基准策略）。
    """
    if symbols is None:
        symbols = ["SPY", "AAPL", "MSFT"]

    engine = BacktestEngine(initial_capital=initial_capital)

    # 下载历史数据
    data = {}
    for sym in symbols:
        df = yf.download(sym, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if not df.empty:
            data[sym] = df

    if not data:
        return {"error": "无数据"}

    # 对齐日期
    dates = sorted(set().union(*[set(d.index) for d in data.values()]))
    logger.info(f"回测: {len(symbols)} 只, {len(dates)} 天, ${initial_capital:,.0f}")

    regime_engine = RegimeEngine()

    for i, date in enumerate(dates):
        # 收集当日价格
        prices = {}
        for sym, df in data.items():
            if date in df.index:
                prices[sym] = float(df.loc[date, "Close"].squeeze())

        if not prices:
            continue

        # 更新市场状态
        spy_price = prices.get("SPY", 450.0)
        regime_engine.update(spy_price)
        regime = regime_engine.get_regime()

        # 计算 MA（需要历史数据）
        if i >= 50:
            for sym in list(prices.keys()):
                if sym not in data or date not in data[sym].index:
                    continue
                df_sym = data[sym]
                idx = df_sym.index.get_loc(date)
                if idx < 50:
                    continue

                close_series = df_sym["Close"].squeeze()
                ma50 = float(close_series.iloc[idx - 49:idx + 1].mean())
                ma200 = float(close_series.iloc[idx - 199:idx + 1].mean()) \
                    if idx >= 199 else ma50

                # 简单金叉死叉
                if idx >= 1:
                    prev_ma50 = float(close_series.iloc[idx - 49:idx].mean())
                    prev_ma200 = float(close_series.iloc[idx - 199:idx].mean()) \
                        if idx >= 199 else prev_ma50

                    golden_cross = ma50 > ma200 and prev_ma50 <= prev_ma200
                    death_cross = ma50 < ma200 and prev_ma50 >= prev_ma200

                    # 风险过滤
                    risk_mult = regime.get("risk_multiplier", 0.5)
                    if risk_mult <= 0:
                        continue

                    if golden_cross and sym not in engine.positions:
                        qty = int((engine.cash * 0.1 * risk_mult) / prices[sym])
                        engine.execute(sym, "BUY", qty, prices[sym],
                                       date=str(date), reason="金叉信号")
                    elif death_cross and sym in engine.positions:
                        qty = engine.positions[sym]["qty"]
                        engine.execute(sym, "SELL", qty, prices[sym],
                                       date=str(date), reason="死叉信号")

        engine.mark_to_market(prices)

    return engine.summary()
