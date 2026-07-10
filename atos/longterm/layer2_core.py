"""
ATOS PRO v2 — Phoenix Layer 2: 核心层
=========================================
完整的质量因子+多因子选股组合。
两种子策略各占 50%：
  1. 质量因子组合 — quality_score > 75 的 Top 20
  2. 多因子综合排名 — Value+Quality+Momentum+LowVol 综合 Top 25

每季度调仓（91天），持有 1 年滚动。
目标年化：12-18%，这是三层策略的超额收益主体。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import os, json
from atos.core.logging import get_logger
from atos.longterm.config import LAYER2, CAPITAL

try:
    from atos.data.futu_provider import get_stock_info as _futu_info
except ImportError:
    _futu_info = None

logger = get_logger("phoenix.layer2")


def _get_info(symbol: str) -> dict:
    """获取股票基本面 — Futu优先，yfinance后备"""
    if _futu_info:
        data = _futu_info(symbol)
        if data.get("_valid"):
            return data
    try:
        return (yf.Ticker(symbol).info or {})
    except Exception:
        return {}

def _get_price(symbol: str) -> float:
    """v11: 从 OHLCV 获取可靠价格"""
    try:
        ticker = yf.Ticker(symbol)
        if hasattr(ticker, 'fast_info'):
            price = getattr(ticker.fast_info, 'lastPrice', 0) or getattr(ticker.fast_info, 'regularMarketPrice', 0)
            if price and price > 0:
                return float(price)
    except Exception:
        pass
    try:
        df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if not df.empty and len(df) > 0:
            return float(df["Close"].squeeze().iloc[-1])
    except Exception:
        pass
    return 0.0


class Layer2Core:
    """
    核心层：
    - 50% 质量因子组合（20 只）
    - 50% 多因子排名组合（25 只）
    - 每季度再平衡
    - 含完整卖出检查
    """

    def __init__(self, state_dir: str = None):
        self.capital = CAPITAL["total"] * CAPITAL["layer2_pct"]
        self.positions = {}  # {symbol: {shares, avg_cost, buy_date, score, sub_strategy}}
        self.last_rebalance = None
        self._cached_ranking = None
        self._cached_portfolio = None
        self.state_dir = state_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "state"
        )
        self._load_state()

    def _state_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        return os.path.join(self.state_dir, "phoenix_layer2_state.json")

    def _load_state(self):
        """从磁盘恢复持仓和再平衡时间"""
        try:
            with open(self._state_file()) as f:
                data = json.load(f)
                self.positions = data.get("positions", {})
                last_reb = data.get("last_rebalance")
                if last_reb:
                    self.last_rebalance = datetime.date.fromisoformat(last_reb)
                logger.info(f"Layer2 状态恢复: {len(self.positions)} 个持仓")
        except Exception:
            self.positions = {}
            self.last_rebalance = None

    def _save_state(self):
        """持久化持仓到磁盘"""
        try:
            data = {
                "positions": self.positions,
                "last_rebalance": (
                    self.last_rebalance.isoformat()
                    if isinstance(self.last_rebalance, datetime.date) else None
                ),
                "capital": self.capital,
            }
            with open(self._state_file(), "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Layer2 状态保存失败: {e}")

    # ── 质量因子评分 ──

    def _calc_quality_score(self, info: dict) -> float:
        """
        AQR 风格质量评分，4 个维度各 25 分，满分 100。

        维度：
          1. 盈利能力 (25分): ROE, 毛利率, 经营利润率
          2. 增长稳定性 (25分): 营收增长, 盈利增长, Beta
          3. 财务安全 (25分): 负债率, 流动比率, 自由现金流
          4. 分红/回购 (25分): 股息率, 回购, 派息率
        """
        score = 0.0

        # 1. 盈利能力
        roe = info.get("returnOnEquity", 0) or 0
        gross_margin = info.get("grossMargins", 0) or 0
        op_margin = info.get("operatingMargins", 0) or 0
        if roe > 0.20: score += 10
        elif roe > 0.15: score += 7
        elif roe > 0.10: score += 4
        if gross_margin > 0.50: score += 8
        elif gross_margin > 0.35: score += 5
        if op_margin > 0.20: score += 7
        elif op_margin > 0.10: score += 4

        # 2. 增长稳定性
        revenue_growth = info.get("revenueGrowth", 0) or 0
        earnings_growth = info.get("earningsGrowth", 0) or 0
        beta = info.get("beta", 1) or 1
        if revenue_growth > 0.15: score += 8
        elif revenue_growth > 0.05: score += 5
        if earnings_growth > 0.15: score += 8
        elif earnings_growth > 0.05: score += 5
        if beta < 0.8: score += 9
        elif beta < 1.0: score += 5

        # 3. 财务安全
        debt_equity = info.get("debtToEquity", 0) or 0
        current_ratio = info.get("currentRatio", 0) or 0
        fcf = info.get("freeCashflow", 0) or 0
        if debt_equity < 50: score += 12
        elif debt_equity < 100: score += 8
        elif debt_equity < 200: score += 4
        if current_ratio > 2.0: score += 8
        elif current_ratio > 1.2: score += 5
        if fcf > 0: score += 5

        # 4. 分红/回购（股东回报）
        div_yield = info.get("dividendYield", 0) or 0
        if div_yield > 1: div_yield = div_yield / 100  # normalize
        payout = info.get("payoutRatio", 0) or 0
        if payout > 1: payout = payout / 100
        if div_yield > 0.02: score += 8
        elif div_yield > 0.01: score += 4
        if 0 < payout < 0.6: score += 7
        elif 0 < payout < 0.8: score += 4
        if div_yield > 0 and payout > 0:
            score += 5  # 分红+回购=积极的资本配置

        # 市值加分（大盘更稳定）
        market_cap = info.get("marketCap", 0) or 0
        if market_cap > 100e9: score += 5
        elif market_cap > 10e9: score += 3

        return min(score, 100)

    # ── 多因子综合排名 ──

    def _calc_multifactor_score(self, info: dict) -> float:
        """
        Fama-French + Greenblatt 混合评分。

        权重：
          Value      30% — P/B, P/E 越低越好
          Quality    30% — ROE, 毛利率, 负债率
          Momentum   20% — 近期价格动量
          LowVol     20% — Beta 越低越好
        """
        score = 0.0

        # Value (30%)
        pb = info.get("priceToBook", 0) or 0
        pe = info.get("trailingPE", 0) or info.get("forwardPE", 0) or 0
        if 0 < pb < 5:
            score += max(0, (5 - pb) / 5) * 30
        elif pb <= 0:
            score += 5
        else:
            score += 5
        if 0 < pe < 15:
            score += 5  # bonus for deep value

        # Quality (30%)
        roe = info.get("returnOnEquity", 0) or 0
        gross_margin = info.get("grossMargins", 0) or 0
        debt_equity = info.get("debtToEquity", 0) or 0
        if roe > 0.20: score += 12
        elif roe > 0.10: score += 7
        elif roe > 0.05: score += 3
        if gross_margin > 0.40: score += 10
        elif gross_margin > 0.25: score += 5
        if debt_equity < 50: score += 8
        elif debt_equity < 100: score += 4

        # Momentum (20%) — approximated from beta and recent growth
        revenue_growth = info.get("revenueGrowth", 0) or 0
        beta = info.get("beta", 1) or 1
        if revenue_growth > 0.20: score += 10
        elif revenue_growth > 0.10: score += 5
        if beta > 1.0: score += max(0, min(10, (beta - 1) * 5))

        # Low Vol (20%)
        if beta < 0.8: score += 20
        elif beta < 1.0: score += 15
        elif beta < 1.2: score += 10
        elif beta < 1.5: score += 5

        return min(score, 100)

    # ── 股票池筛选 ──

    def _get_universe(self) -> list[str]:
        """获取股票池"""
        try:
            from atos.core.universe import ALL_SYMBOLS
            return ALL_SYMBOLS[:]
        except ImportError:
            # 后备：标普500主要成分
            return [
                "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
                "JPM", "BAC", "GS", "V", "MA", "JNJ", "UNH", "PFE",
                "COST", "WMT", "HD", "CAT", "BA", "XOM", "CVX",
                "AMD", "AVGO", "QCOM", "CRM", "ADBE", "NFLX", "DIS",
            ]

    # ── 核心运行 ──

    def run(self) -> dict:
        """
        执行 Layer 2 完整排名。
        返回质量组合和多因子组合的选股结果。
        """
        logger.info("═══════ Layer 2: 核心层排名开始 ═══════")

        symbols = self._get_universe()
        quality_picks = []
        multifactor_picks = []

        for sym in symbols:
            try:
                info = _get_info(sym)
                market_cap = info.get("marketCap", 0) or 0

                # 过滤微盘股
                if market_cap < LAYER2.get("quality_min_market_cap", 2e9):
                    continue

                quality = self._calc_quality_score(info)
                multifactor = self._calc_multifactor_score(info)

                if quality >= LAYER2.get("quality_min_score", 75):
                    quality_picks.append({"symbol": sym, "score": round(quality, 1)})

                multifactor_picks.append({"symbol": sym, "score": round(multifactor, 1)})

            except Exception as e:
                logger.debug(f"Layer2 评分失败 {sym}: {e}")
                continue

        # 排序取 Top
        quality_picks.sort(key=lambda x: -x["score"])
        multifactor_picks.sort(key=lambda x: -x["score"])

        top_quality = quality_picks[:LAYER2.get("quality_top_n", 20)]
        top_multifactor = multifactor_picks[:LAYER2.get("multifactor_top_n", 25)]

        # 合并去重（质量优先）
        quality_symbols = {p["symbol"] for p in top_quality}
        united = list(top_quality)
        for p in top_multifactor:
            if p["symbol"] not in quality_symbols:
                united.append(p)

        self._cached_ranking = united
        self.last_rebalance = datetime.date.today()
        self._save_state()

        logger.info(f"Layer2 完成: 质量 {len(top_quality)}只 + 多因子 {len(top_multifactor)}只 = 合并 {len(united)}只")

        return {
            "layer": "core",
            "timestamp": datetime.datetime.now().isoformat(),
            "total_capital": self.capital,
            "quality_count": len(top_quality),
            "multifactor_count": len(top_multifactor),
            "united_count": len(united),
            "top_5": [p["symbol"] for p in united[:5]],
        }

    # ── 买入订单 ──

    def get_buy_orders(self, existing_positions: dict = None) -> list[dict]:
        """
        生成买入订单。跳过已在现有持仓中的标的。

        Args:
            existing_positions: {symbol: {shares, avg_cost, ...}} 来自 Phoenix 总持仓
        """
        if existing_positions is None:
            existing_positions = {}

        if not self._cached_ranking:
            self.run()

        orders = []
        ranking = self._cached_ranking or []
        l2_capital = self.capital
        max_positions = LAYER2.get("quality_top_n", 20) + LAYER2.get("multifactor_top_n", 25)
        selected = ranking[:max_positions]

        # 过滤已有持仓
        new_picks = [p for p in selected if p["symbol"] not in existing_positions]
        if not new_picks:
            logger.info("Layer2: 所有目标标的已在持仓中，跳过买入")
            return orders

        capital_per = l2_capital / max(len(new_picks), 1)

        for pick in new_picks:
            try:
                price = _get_price(pick["symbol"])  # v11: 可靠价格
                if price <= 0:
                    continue
                shares = max(1, int(capital_per / price))
                orders.append({
                    "layer": "core",
                    "symbol": pick["symbol"],
                    "action": "BUY",
                    "quantity": shares,
                    "price": round(price, 2),
                    "score": pick["score"],
                    "reason": f"多因子排名 {pick['score']:.0f}分",
                })
            except Exception as e:
                logger.warning(f"L2 买入订单 {pick['symbol']}: {e}")

        logger.info(f"Layer2 生成 {len(orders)} 个买入订单")
        return orders

    # ── 卖出检查 ──

    def _quality_check(self, symbol: str) -> str:
        """
        检查持仓标的是否触发卖出条件。

        返回: "SELL" | "REDUCE" | "HOLD"
        """
        try:
            stock = yf.Ticker(symbol)
            info = stock.info or {}

            debt_equity = info.get("debtToEquity", 0) or 0
            roe = info.get("returnOnEquity", 0) or 0
            current_ratio = info.get("currentRatio", 0) or 0
            revenue_growth = info.get("revenueGrowth", 0) or 0
            earnings_growth = info.get("earningsGrowth", 0) or 0

            # 🔴 红灯：必须卖出
            if debt_equity > LAYER2.get("sell_debt_equity_max", 200):
                return "SELL"
            if roe < LAYER2.get("sell_roe_min", 0):
                return "SELL"
            if current_ratio < 0.5:
                return "SELL"

            # 🟡 黄灯：减仓
            if revenue_growth < LAYER2.get("sell_revenue_decline", -0.20):
                return "REDUCE"
            if earnings_growth < -0.30:
                return "REDUCE"

            return "HOLD"
        except Exception as e:
            logger.warning(f"质量检查失败 {symbol}: {e}")
            return "HOLD"  # 数据获取失败不卖（避免误杀）

    def get_sell_orders(self, positions: dict) -> list[dict]:
        """
        检查 Layer 2 持仓，生成卖出订单。

        Args:
            positions: 来自 Phoenix 总持仓的 dict
        """
        orders = []

        for symbol, pos in positions.items():
            # 只处理 Layer 2 的持仓
            if pos.get("layer") != "core":
                continue

            decision = self._quality_check(symbol)

            if decision == "SELL":
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.info or {}
                    price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
                except Exception:
                    price = pos.get("avg_cost", 0)

                orders.append({
                    "layer": "core",
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": pos.get("shares", 0),
                    "price": round(price, 2) if price > 0 else 0,
                    "reason": f"质量恶化: 负债/Roe/流动性触发卖出",
                })
                logger.warning(f"🔴 L2 卖出: {symbol} — 质量恶化")

            elif decision == "REDUCE":
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.info or {}
                    price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
                except Exception:
                    price = pos.get("avg_cost", 0)

                reduce_shares = max(1, pos.get("shares", 0) // 2)
                orders.append({
                    "layer": "core",
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": reduce_shares,
                    "price": round(price, 2) if price > 0 else 0,
                    "reason": f"营收/盈利下降，减仓50%",
                })
                logger.warning(f"🟡 L2 减仓: {symbol} — 基本面走弱")

        if orders:
            logger.info(f"Layer2 生成 {len(orders)} 个卖出/减仓订单")
        return orders

    # ── 组合估值 ──

    def get_positions_value(self, positions: dict = None) -> float:
        """计算 Layer 2 持仓总市值"""
        if positions is None:
            positions = self.positions
        total = 0.0
        for symbol, pos in positions.items():
            if pos.get("layer") != "core":
                continue
            try:
                stock = yf.Ticker(symbol)
                info = stock.info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
            except Exception:
                price = pos.get("avg_cost", 0)
            total += pos.get("shares", 0) * price
        return total


# ─── 单例 ───

_layer2_instance: Layer2Core = None


def get_layer2() -> Layer2Core:
    global _layer2_instance
    if _layer2_instance is None:
        _layer2_instance = Layer2Core()
    return _layer2_instance


def run_layer2() -> dict:
    return get_layer2().run()
