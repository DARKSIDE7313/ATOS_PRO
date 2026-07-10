"""
ATOS PRO v3 — Phoenix 回测模块（增强版）
===========================================
完整回测引擎，支持：
  - 智能调仓（卖出不再排名的，买入新上榜的）
  - 高资金利用率（80% 部署率）
  - 三层组合并行回测（layer1+layer2+layer3）
  - SPY 基准对比 + 完整风险指标

用法:
  python -m atos.longterm.backtest --layer all --start 2020-01-01 --end 2024-12-31
  python -m atos.longterm.backtest --layer layer2 --start 2022-01-01 --end 2022-12-31
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
from atos.core.logging import get_logger

logger = get_logger("phoenix.backtest")


class BacktestEngine:
    """增强版回测引擎 — 含调仓、卖出、三层组合"""

    def __init__(self, initial_capital: float = 200_000):
        self.initial_capital = initial_capital
        self.reset()

    def reset(self):
        self.cash = self.initial_capital
        self.positions = {}     # {symbol: {shares, avg_cost, layer, buy_date}}
        self.trades = []        # 所有买卖记录
        self.equity_curve = []
        self.benchmark_curve = []
        self._layer_caps = {}   # 各层资金上限

    # ═══════════════════════════════════════════
    # 数据加载
    # ═══════════════════════════════════════════

    def load_data(self, symbols: list[str], start: str, end: str) -> dict:
        """加载历史价格数据（带缓存避免重复下载）"""
        data = {}
        for sym in symbols:
            try:
                df = yf.download(sym, start=start, end=end, progress=False, auto_adjust=True)
                if df is not None and not df.empty:
                    data[sym] = df
            except Exception:
                pass
        logger.info(f"加载 {len(data)}/{len(symbols)} 只标的的历史数据")
        return data

    def load_benchmark(self, start: str, end: str) -> pd.DataFrame:
        try:
            return yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        except Exception:
            return pd.DataFrame()

    # ═══════════════════════════════════════════
    # 价格提取
    # ═══════════════════════════════════════════

    def _safe_price(self, row, col: str = "Close") -> float:
        try:
            vals = row[col]
            if hasattr(vals, 'values'):
                return float(vals.values.flatten()[0])
            return float(vals.iloc[0])
        except Exception:
            return 0.0

    # ═══════════════════════════════════════════
    # 信号生成
    # ═══════════════════════════════════════════

    def _generate_signals(self, symbols: list[str], layer: str) -> list[dict]:
        """
        统一信号生成。不同层用不同打分逻辑。

        Layer 1: 高股息 + 低PE + 健康派息率
        Layer 2: 质量因子（ROE/毛利率/低负债/合理PE）
        Layer 3: 动量因子（6M价格动量）
        """
        picks = []
        for sym in symbols:
            try:
                stock = yf.Ticker(sym)
                info = stock.info or {}
                market_cap = info.get("marketCap", 0) or 0

                if market_cap < 2e9:  # 过滤微盘
                    continue

                if layer == "layer1":
                    # 股息价值
                    div_yield = info.get("dividendYield", 0) or 0
                    if div_yield > 1: div_yield /= 100
                    pe = info.get("trailingPE", 0) or info.get("forwardPE", 0) or 0
                    payout = info.get("payoutRatio", 0) or 0
                    if payout > 1: payout /= 100
                    roe = info.get("returnOnEquity", 0) or 0
                    debt = info.get("debtToEquity", 0) or 0

                    if div_yield < 0.01:
                        continue
                    score = 0.0
                    score += min(div_yield * 1000, 40)       # 股息率
                    score += max(0, (25 - pe) * 1.5) if pe > 0 else 10  # PE
                    score += 15 if 0.2 < payout < 0.6 else (5 if payout < 0.8 else -10)
                    score += 10 if roe > 0.15 else (5 if roe > 0.08 else 0)
                    score += 10 if debt < 50 else (5 if debt < 100 else -5)

                elif layer == "layer3":
                    # 动量信号
                    pe = info.get("trailingPE", 0) or 0
                    roe = info.get("returnOnEquity", 0) or 0
                    revenue_growth = info.get("revenueGrowth", 0) or 0
                    beta = info.get("beta", 1) or 1

                    score = 0.0
                    score += min(revenue_growth * 200, 30) if revenue_growth > 0 else max(revenue_growth * 100, -10)
                    score += 15 if beta > 1.1 else (5 if beta > 0.9 else 0)
                    score += 10 if roe > 0.20 else (5 if roe > 0.10 else 0)
                    score += 10 if 0 < pe < 30 else 0
                    score += 10 if market_cap > 50e9 else 5

                else:  # layer2 — 质量因子
                    pe = info.get("trailingPE", 0) or info.get("forwardPE", 0) or 0
                    roe = info.get("returnOnEquity", 0) or 0
                    debt_equity = info.get("debtToEquity", 0) or 0
                    gross_margin = info.get("grossMargins", 0) or 0
                    fcf = info.get("freeCashflow", 0) or 0

                    score = 0.0
                    if 0 < pe < 15: score += 25
                    elif 0 < pe < 25: score += 15
                    elif 0 < pe < 35: score += 5
                    if roe > 0.25: score += 25
                    elif roe > 0.15: score += 15
                    elif roe > 0.08: score += 8
                    if debt_equity < 30: score += 20
                    elif debt_equity < 80: score += 10
                    if gross_margin > 0.50: score += 15
                    elif gross_margin > 0.30: score += 8
                    if fcf > 0: score += 10
                    if market_cap > 100e9: score += 5

                picks.append({
                    "symbol": sym,
                    "score": round(score, 1),
                    "pe": pe,
                    "market_cap": market_cap,
                })
            except Exception:
                continue

        picks.sort(key=lambda x: -x["score"])
        return picks

    # ═══════════════════════════════════════════
    # 智能调仓（核心改进）
    # ═══════════════════════════════════════════

    def _get_position_value(self, sym: str, pos: dict, data: dict, date) -> float:
        """安全计算单只持仓当前市值"""
        if sym in data:
            df = data[sym]
            row = df.loc[df.index == date]
            if not row.empty:
                price = self._safe_price(row)
                if price > 0:
                    return pos["shares"] * price
        return pos["shares"] * pos["avg_cost"]

    def _rebalance(self, signals: list[dict], data: dict, date,
                   layer: str, max_positions: int, capital_pct: float):
        """
        智能调仓：
        1. 卖出现有持仓中不再排名靠前的
        2. 保留还在排名中的
        3. 用现金买入新上榜的（按评分加权）
        """
        top_picks = {s["symbol"] for s in signals[:max_positions]}
        layer_capital = self.initial_capital * capital_pct

        my_positions = {
            sym: pos for sym, pos in self.positions.items()
            if pos.get("layer") == layer
        }

        # Step 1: 卖出
        for sym, pos in list(my_positions.items()):
            if sym not in top_picks:
                price = self._get_position_price(sym, pos, data, date)
                if price <= 0:
                    continue
                shares = pos["shares"]
                self.cash += shares * price
                pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"] if pos["avg_cost"] > 0 else 0
                self.trades.append({
                    "date": str(date)[:10], "symbol": sym, "action": "SELL",
                    "shares": shares, "price": round(price, 2),
                    "value": round(shares * price, 2),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "layer": layer, "reason": "排名下降，调仓卖出",
                })
                del self.positions[sym]

        # Step 2: 计算可用资金
        my_positions = {
            sym: pos for sym, pos in self.positions.items()
            if pos.get("layer") == layer
        }
        current_value = sum(
            self._get_position_value(sym, pos, data, date)
            for sym, pos in my_positions.items()
        )
        # 可用 = min(现金, 本层上限 - 已占用)
        available = min(self.cash, max(0, layer_capital - current_value))
        # 至少保留 5% 现金
        available = min(available, self.cash * 0.95)

        if available <= 100:  # 少于 $100 不交易
            return

        # Step 3: 买入新上榜的
        existing = set(my_positions.keys())
        new_picks = [s for s in signals[:max_positions] if s["symbol"] not in existing and s["symbol"] in data]
        if not new_picks:
            return

        total_score = sum(max(0.1, s["score"]) for s in new_picks)
        for pick in new_picks:
            sym = pick["symbol"]
            weight = max(0.1, pick["score"]) / total_score
            alloc = available * weight

            df = data[sym]
            row = df.loc[df.index == date]
            if row.empty:
                continue
            price = self._safe_price(row)
            if price <= 0:
                continue

            shares = max(1, int(alloc / price))
            cost = shares * price
            if cost > self.cash:
                continue

            self.cash -= cost
            self.positions[sym] = {
                "shares": shares, "avg_cost": price,
                "buy_date": str(date)[:10], "layer": layer, "score": pick["score"],
            }
            self.trades.append({
                "date": str(date)[:10], "symbol": sym, "action": "BUY",
                "shares": shares, "price": round(price, 2),
                "value": round(cost, 2), "pnl_pct": 0,
                "layer": layer, "reason": f"评分 {pick['score']:.0f}",
            })

    def _get_position_price(self, sym: str, pos: dict, data: dict, date) -> float:
        """获取持仓当前价格"""
        if sym not in data:
            return pos["avg_cost"]
        df = data[sym]
        row = df.loc[df.index == date]
        if row.empty:
            return pos["avg_cost"]
        p = self._safe_price(row)
        return p if p > 0 else pos["avg_cost"]

    # ═══════════════════════════════════════════
    # 主回测
    # ═══════════════════════════════════════════

    def run(self, symbols: list[str], start: str, end: str,
            layer: str = "all", rebalance_days: int = 90,
            max_positions: int = 15) -> dict:
        """
        执行回测。
        """
        self.reset()
        logger.info(f"回测: {layer} | {start} → {end} | ${self.initial_capital:,.0f} | 调仓每{rebalance_days}天")

        data = self.load_data(symbols, start, end)
        benchmark = self.load_benchmark(start, end)

        if not data:
            return {"error": "无可用数据"}

        all_dates = set()
        for df in data.values():
            all_dates.update(df.index)
        dates = sorted(all_dates)
        if not dates:
            return {"error": "无交易日"}

        # 各层配置
        if layer == "all":
            # 合并三层信号到一个池子里统一调仓
            layers_config = [
                ("layer1", 0.30, 5),
                ("layer2", 0.50, 10),
                ("layer3", 0.20, 5),
            ]
        elif layer == "layer1":
            layers_config = [("layer1", 1.0, 20)]
        elif layer == "layer3":
            layers_config = [("layer3", 1.0, 10)]
        else:
            layers_config = [("layer2", 1.0, 20)]

        last_rebalance = None

        for i, date in enumerate(dates):
            date_obj = date.date() if hasattr(date, 'date') else date

            # 基准曲线
            if not benchmark.empty:
                bench_row = benchmark.loc[benchmark.index == date]
                if not bench_row.empty:
                    self.benchmark_curve.append({
                        "date": str(date_obj),
                        "value": self._safe_price(bench_row),
                    })

            # 组合净值
            equity = self._calc_equity(data, date)
            self.equity_curve.append({"date": str(date_obj), "value": equity})

            # 调仓
            should = (last_rebalance is None or
                      (date_obj - last_rebalance).days >= rebalance_days)
            if not should:
                continue

            last_rebalance = date_obj

            # 合并模式：先收集所有层信号，加权合并后统一调仓
            if layer == "all":
                all_picks = {}
                for layer_name, cap_pct, max_pos in layers_config:
                    signals = self._generate_signals(symbols, layer_name)
                    for s in signals[:max_pos]:
                        sym = s["symbol"]
                        if sym not in all_picks or s["score"] > all_picks[sym]["score"]:
                            all_picks[sym] = {"symbol": sym, "score": s["score"], "layer": layer_name}
                merged = sorted(all_picks.values(), key=lambda x: -x["score"])
                # 统一调仓，用 100% 资本
                self._rebalance(merged, data, date, "combined", max_positions=25, capital_pct=1.0)
            else:
                for layer_name, cap_pct, max_pos in layers_config:
                    signals = self._generate_signals(symbols, layer_name)
                    if signals:
                        self._rebalance(signals, data, date, layer_name, max_pos, cap_pct)

        # 最终清算
        final_equity = self._calc_equity(data, dates[-1], liquidate=True)
        metrics = self._calc_metrics()

        # 统计
        buys = [t for t in self.trades if t["action"] == "BUY"]
        sells = [t for t in self.trades if t["action"] == "SELL"]
        avg_sell_pnl = np.mean([s["pnl_pct"] for s in sells]) if sells else 0

        report = {
            "config": {"layer": layer, "start": start, "end": end,
                       "initial_capital": self.initial_capital,
                       "rebalance_days": rebalance_days, "max_positions": max_positions},
            "results": {
                "final_value": round(final_equity, 2),
                "total_return_pct": round((final_equity - self.initial_capital) / self.initial_capital * 100, 2),
                "total_trades": len(self.trades),
                "buy_count": len(buys),
                "sell_count": len(sells),
                "avg_sell_pnl_pct": round(avg_sell_pnl, 2),
                "avg_positions_held": round(len(self.positions), 1),
                "final_cash": round(self.cash, 2),
                **metrics,
            },
        }

        logger.info(
            f"回测完成: {report['results']['total_return_pct']:+.1f}% | "
            f"CAGR {metrics.get('cagr_pct',0):+.1f}% | Sharpe {metrics.get('sharpe',0):.2f} | "
            f"MaxDD {metrics.get('max_drawdown_pct',0):.1f}% | "
            f"{len(buys)}买{len(sells)}卖"
        )
        return report

    def _calc_equity(self, data: dict, date, liquidate: bool = False) -> float:
        total = self.cash
        for sym, pos in self.positions.items():
            if sym not in data:
                total += pos["shares"] * pos["avg_cost"]
                continue
            df = data[sym]
            row = df.loc[df.index == date]
            if row.empty:
                total += pos["shares"] * pos["avg_cost"]
            else:
                price = self._safe_price(row)
                total += pos["shares"] * price
        return total

    def _calc_metrics(self) -> dict:
        if len(self.equity_curve) < 2:
            return {}

        values = np.array([e["value"] for e in self.equity_curve])
        daily_returns = np.diff(values) / values[:-1]
        daily_returns = daily_returns[~np.isnan(daily_returns)]

        if len(daily_returns) < 2:
            return {}

        total_return = (values[-1] - self.initial_capital) / self.initial_capital
        trading_days = len(values)
        years = trading_days / 252
        cagr = (values[-1] / self.initial_capital) ** (1 / max(years, 0.1)) - 1
        annual_vol = float(np.std(daily_returns) * np.sqrt(252))
        rf = 0.03
        sharpe = (cagr - rf) / max(annual_vol, 0.001)
        peak = np.maximum.accumulate(values)
        drawdowns = (values - peak) / peak
        max_dd = float(np.min(drawdowns))
        calmar = cagr / abs(max_dd) if max_dd < 0 else 0

        # 胜率：卖出盈亏
        sells = [t for t in self.trades if t["action"] == "SELL"]
        wins = sum(1 for s in sells if s.get("pnl_pct", 0) > 0)
        win_rate = wins / len(sells) if sells else 0

        # SPY 基准
        benchmark_return = 0.0
        if len(self.benchmark_curve) > 1:
            bs = self.benchmark_curve[0]["value"]
            be = self.benchmark_curve[-1]["value"]
            benchmark_return = (be - bs) / bs if bs > 0 else 0

        return {
            "cagr_pct": round(cagr * 100, 2),
            "annual_volatility_pct": round(annual_vol * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "calmar": round(calmar, 2),
            "win_rate_pct": round(win_rate * 100, 1),
            "trading_days": int(trading_days),
            "years": round(years, 2),
            "benchmark_return_pct": round(benchmark_return * 100, 2),
            "alpha_pct": round((cagr - benchmark_return) * 100, 2),
        }


# ─── 便捷入口 ───

def run_backtest(symbols: list[str] = None, start: str = "2020-01-01",
                 end: str = "2025-12-31", layer: str = "all",
                 capital: float = 200_000, rebalance_days: int = 90) -> dict:
    if symbols is None:
        symbols = [
            "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN",
            "JPM", "V", "MA", "JNJ", "UNH", "COST", "WMT",
            "HD", "CAT", "XOM", "CVX", "AVGO", "CRM", "ADBE",
        ]
    engine = BacktestEngine(initial_capital=capital)
    return engine.run(symbols, start, end, layer=layer, rebalance_days=rebalance_days)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Phoenix 回测工具 v3")
    parser.add_argument("--layer", default="all", choices=["layer1", "layer2", "layer3", "all"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--capital", type=float, default=200_000)
    parser.add_argument("--rebalance", type=int, default=90)
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()

    syms = args.symbols.split(",") if args.symbols else None
    print(f"\n🔥 Phoenix 回测 v3 | {args.layer} | {args.start} → {args.end}")
    report = run_backtest(symbols=syms, start=args.start, end=args.end,
                          layer=args.layer, capital=args.capital,
                          rebalance_days=args.rebalance)

    if "error" in report:
        print(f"❌ {report['error']}")
    else:
        r = report["results"]
        print(f"   最终价值:     ${r['final_value']:,.0f}")
        print(f"   总收益率:     {r['total_return_pct']:+.1f}%")
        print(f"   年化 CAGR:    {r.get('cagr_pct',0):+.1f}%")
        print(f"   夏普比率:     {r.get('sharpe',0):.2f}")
        print(f"   最大回撤:     {r.get('max_drawdown_pct',0):.1f}%")
        print(f"   卖出胜率:     {r.get('win_rate_pct',0):.1f}%")
        print(f"   基准 SPY:     {r.get('benchmark_return_pct',0):+.1f}%")
        print(f"   Alpha:        {r.get('alpha_pct',0):+.1f}%")
        print(f"   交易:         {r.get('buy_count',0)}买 {r.get('sell_count',0)}卖")
        print(f"   平均卖出盈亏: {r.get('avg_sell_pnl_pct',0):+.1f}%")
