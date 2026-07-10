"""
ATOS PRO v2 — Phoenix Layer 1: 基础层
=========================================
两种子策略：
  1. 股息贵族组合 — 高质量、稳定分红的公司
  2. 增强型指数定投 — 根据 PE 调整定投金额

每 15 天运行一次。DCA 日志和再平衡时间持久化到磁盘。
目标年化：8-12%，维持组合底线稳定性。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import os, json
from atos.core.logging import get_logger
from atos.longterm.config import LAYER1, CAPITAL

# Futu 数据源（优先），yfinance 后备
try:
    from atos.data.futu_provider import get_stock_info
except ImportError:
    get_stock_info = None

logger = get_logger("phoenix.layer1")


def _get_info(symbol: str) -> dict:
    """获取股票基本面 — Futu优先，yfinance后备"""
    if get_stock_info:
        data = get_stock_info(symbol)
        if data.get("_valid"):
            return data
    try:
        return (yf.Ticker(symbol).info or {})
    except Exception:
        return {}

def _get_price(symbol: str) -> float:
    """v11: 从 OHLCV 获取价格 (比 stock.info 可靠得多)"""
    try:
        # 先用 info (快)
        info = yf.Ticker(symbol).fast_info if hasattr(yf.Ticker(symbol), 'fast_info') else {}
        price = getattr(info, 'lastPrice', 0) or getattr(info, 'regularMarketPrice', 0)
        if price and price > 0:
            return float(price)
    except Exception:
        pass
    try:
        # 降级: 用 history (慢但可靠)
        df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if not df.empty and len(df) > 0:
            close = df["Close"].squeeze()
            if hasattr(close, 'iloc'):
                return float(close.iloc[-1])
    except Exception:
        pass
    return 0.0


class Layer1Foundation:
    """
    基础层：
    - 50% 股息贵族
    - 50% 指数增强定投
    """

    def __init__(self, state_dir: str = None):
        self.capital = CAPITAL["total"] * CAPITAL["layer1_pct"]
        self.positions = {}
        self.last_rebalance = None
        self.dca_log = []
        self.state_dir = state_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "state"
        )
        self._load_state()

    def _state_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        return os.path.join(self.state_dir, "phoenix_layer1_state.json")

    def _load_state(self):
        """从磁盘恢复 DCA 日志和再平衡时间"""
        try:
            with open(self._state_file()) as f:
                data = json.load(f)
                self.dca_log = data.get("dca_log", [])
                last_reb = data.get("last_rebalance")
                if last_reb:
                    self.last_rebalance = datetime.date.fromisoformat(last_reb)
                logger.info(f"Layer1 状态恢复: {len(self.dca_log)} 条DCA记录")
        except Exception:
            self.dca_log = []
            self.last_rebalance = None

    def _save_state(self):
        """持久化 DCA 日志到磁盘"""
        try:
            data = {
                "dca_log": [
                    {
                        **{k: v.isoformat() if isinstance(v, datetime.date) else v
                           for k, v in r.items()},
                        "date": r["date"].isoformat() if isinstance(r.get("date"), datetime.date) else str(r.get("date", ""))
                    }
                    for r in self.dca_log[-100:]
                ],
                "last_rebalance": (
                    self.last_rebalance.isoformat()
                    if isinstance(self.last_rebalance, datetime.date) else None
                ),
            }
            with open(self._state_file(), "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Layer1 状态保存失败: {e}")

    # ── 股息贵族子模块 ──

    def load_dividend_aristocrats(self) -> list[str]:
        """加载股息贵族名单"""
        fallback_aristocrats = [
            "KO", "PEP", "JNJ", "PG", "WMT", "MCD", "CL", "KMB",
            "ADP", "BRO", "CINF", "EMR", "GD", "ITW", "LOW",
            "ABBV", "ABT", "BDX", "CAH", "CVX", "ED", "EXPD",
            "HRL", "IBM", "LIN", "MKC", "NUE", "SYY", "TGT", "XOM",
        ]
        try:
            html = pd.read_html("https://en.wikipedia.org/wiki/S%26P_500_Dividend_Aristocrats")
            if html and len(html) > 0:
                df = html[0]
                if "Symbol" in df.columns:
                    return df["Symbol"].tolist()
        except Exception:
            pass
        return fallback_aristocrats

    def score_aristocrat(self, symbol: str) -> dict:
        """评分一只股息贵族"""
        try:
            info = _get_info(symbol)
            div_yield = info.get("dividendYield", 0) or 0
            if div_yield > 1:
                div_yield = div_yield / 100
            payout = info.get("payoutRatio", 0) or 0
            if payout > 1:
                payout = payout / 100
            roe = info.get("returnOnEquity", 0) or 0
            debt_equity = info.get("debtToEquity", 0) or 0
            gross_margin = info.get("grossMargins", 0) or 0
            pe = info.get("trailingPE", 0) or 0
            price = info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or 0
            if price <= 0:
                return {"symbol": symbol, "score": 0, "error": "无价格数据"}
            score = 0
            if div_yield > 0.04: score += 25
            elif div_yield > 0.03: score += 20
            elif div_yield > 0.02: score += 10
            elif div_yield > 0.01: score += 5
            if 0.30 <= payout <= 0.60: score += 20
            elif 0.20 <= payout <= 0.80: score += 10
            if roe > 0.25: score += 20
            elif roe > 0.15: score += 15
            elif roe > 0.10: score += 10
            if debt_equity < 30: score += 15
            elif debt_equity < 80: score += 10
            elif debt_equity < 150: score += 5
            if gross_margin > 0.50: score += 15
            elif gross_margin > 0.30: score += 10
            if 10 < pe < 25: score += 5
            return {
                "symbol": symbol, "score": score,
                "div_yield": round(div_yield, 4),
                "payout": round(payout, 4),
                "roe": round(roe, 4),
                "debt_equity": round(debt_equity, 1),
                "gross_margin": round(gross_margin, 4),
                "pe": round(pe, 1),
            }
        except Exception as e:
            return {"symbol": symbol, "score": 0, "error": str(e)}

    def select_aristocrats(self, count: int = None) -> list[dict]:
        """选出最优股息贵族，结果缓存"""
        if count is None:
            count = LAYER1.get("aristocrat_position_count", 15)
        symbols = self.load_dividend_aristocrats()
        results = []
        for sym in symbols:
            scored = self.score_aristocrat(sym)
            if scored.get("score", 0) > 0:
                results.append(scored)
        results.sort(key=lambda x: -x["score"])
        top = results[:count]
        self.last_rebalance = datetime.date.today()
        self._save_state()
        # 缓存供 get_orders() 复用
        self._cached_aristocrats = top
        logger.info(f"选出 Top {len(top)} 股息贵族, 状态已保存")
        return top

    # ── 指数增强定投子模块 ──

    def get_sp500_pe(self) -> float:
        try:
            voo = yf.Ticker("VOO")
            info = voo.info or {}
            return float(info.get("trailingPE", 0) or info.get("forwardPE", 0) or 20)
        except Exception:
            return 20.0

    def calculate_dca_multiple(self, pe: float) -> float:
        min_pe = LAYER1.get("dca_min_pe_for_double", 15)
        max_pe_half = LAYER1.get("dca_max_pe_for_half", 25)
        max_pe_quarter = LAYER1.get("dca_max_pe_for_quarter", 30)
        if pe < min_pe: return 2.0
        elif pe < max_pe_half: return 1.0
        elif pe < max_pe_quarter: return 0.5
        else: return 0.25

    def should_dca_today(self) -> bool:
        today = datetime.date.today()
        if not self.dca_log:
            return True
        try:
            last = max(
                datetime.date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"]
                for r in self.dca_log[-20:] if r.get("date")
            )
            return (today - last).days >= LAYER1.get("dca_period_days", 15)
        except Exception:
            return True

    def execute_dca(self) -> dict:
        if not self.should_dca_today():
            return {"action": "skip", "reason": "未到定投日"}
        pe = self.get_sp500_pe()
        multiple = self.calculate_dca_multiple(pe)
        base_amount = LAYER1.get("dca_base_amount", 30000)
        invest_amount = base_amount * multiple
        etf = LAYER1.get("dca_etf", "VOO")
        try:
            stock = yf.Ticker(etf)
            info = stock.info or {}
            price = info.get("currentPrice", 0) or info.get("regularMarketPrice", 0)
            if price <= 0:
                price = float(stock.history(period="1d")["Close"].iloc[-1])
        except Exception:
            return {"action": "error", "reason": f"无法获取 {etf} 价格"}
        shares = invest_amount / price
        result = {
            "action": "buy", "symbol": etf,
            "amount": invest_amount, "shares": round(shares, 2),
            "price": round(price, 2), "pe": round(pe, 1),
            "multiple": multiple, "date": datetime.date.today().isoformat(),
        }
        self.dca_log.append(result)
        self._save_state()
        logger.info(f"📊 定投: {etf} × ${invest_amount:.0f} "
                    f"({shares:.1f}股 @ ${price:.2f}) PE={pe:.1f} 倍数={multiple:.1f}x | 已保存")
        return result

    def run(self) -> dict:
        """执行 Layer 1"""
        result = {"layer": "foundation", "timestamp": datetime.datetime.now().isoformat()}
        need_rebalance = (self.last_rebalance is None or
                         (datetime.date.today() - self.last_rebalance).days >= LAYER1.get("rebalance_frequency_days", 183))
        if need_rebalance:
            aristocrats = self.select_aristocrats()
            result["aristocrats"] = aristocrats
            result["aristocrat_count"] = len(aristocrats)
            result["rebalanced"] = True
        else:
            result["rebalanced"] = False
        result["dca"] = self.execute_dca()
        return result

    def get_buy_orders(self, existing_positions: dict = None) -> list[dict]:
        """生成买入订单，跳过已持有的标的"""
        if existing_positions is None:
            existing_positions = {}

        orders = []
        aristocrats = getattr(self, '_cached_aristocrats', None) or self.select_aristocrats()
        # 过滤已有持仓
        new_picks = [a for a in aristocrats if a["symbol"] not in existing_positions]
        if not new_picks:
            logger.info("Layer1: 所有股息贵族已在持仓中，跳过买入")
            return orders

        capital_per = (self.capital * LAYER1.get("aristocrats_pct", 0.50)) / max(len(new_picks), 1)
        for a in new_picks:
            try:
                price = _get_price(a["symbol"])  # v11: 用可靠的 OHLCV 价格
                if price > 0:
                    orders.append({
                        "layer": "foundation", "symbol": a["symbol"],
                        "action": "BUY", "quantity": max(1, int(capital_per / price)),
                        "price": round(price, 2),
                        "reason": f"股息贵族 #{a['score']}分 股息率{a['div_yield']*100:.1f}%",
                    })
            except Exception as e:
                logger.warning(f"{a['symbol']} 订单失败: {e}")
        return orders

    # Legacy alias — do NOT remove (called by phoenix_runner.py pre-v3)
    def get_orders(self) -> list[dict]:
        return self.get_buy_orders()

    # ── 卖出检查 ──

    def _check_aristocrat_health(self, symbol: str) -> str:
        """
        检查股息贵族持仓健康状态。

        返回: "SELL" | "REDUCE" | "HOLD"
        """
        try:
            info = _get_info(symbol)

            div_yield = info.get("dividendYield", 0) or 0
            if div_yield > 1: div_yield = div_yield / 100
            payout = info.get("payoutRatio", 0) or 0
            if payout > 1: payout = payout / 100
            roe = info.get("returnOnEquity", 0) or 0
            pe = info.get("trailingPE", 0) or info.get("forwardPE", 0) or 0
            debt_equity = info.get("debtToEquity", 0) or 0
            revenue_growth = info.get("revenueGrowth", 0) or 0

            # 🔴 红灯：必须卖出
            if div_yield <= 0 and payout <= 0:
                return "SELL"  # 股息被削减
            if payout > 0.80:
                return "SELL"  # 派息率不可持续
            if roe < 0:
                return "SELL"  # 亏损
            if debt_equity > 200:
                return "SELL"  # 负债爆炸

            # 🟡 黄灯：减仓
            if pe > 30:
                return "REDUCE"  # 估值过高
            if revenue_growth < -0.15:
                return "REDUCE"  # 营收严重萎缩

            return "HOLD"
        except Exception as e:
            logger.warning(f"股息贵族健康检查失败 {symbol}: {e}")
            return "HOLD"

    def get_sell_orders(self, positions: dict) -> list[dict]:
        """
        检查 Layer 1 持仓，生成卖出订单。

        Args:
            positions: 来自 Phoenix 总持仓的 dict
        """
        orders = []
        aristocrat_list = self.load_dividend_aristocrats()

        for symbol, pos in positions.items():
            if pos.get("layer") != "foundation":
                continue

            # 检查是否被移出股息贵族名单
            if symbol not in aristocrat_list:
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.info or {}
                    price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
                except Exception:
                    price = pos.get("avg_cost", 0)

                orders.append({
                    "layer": "foundation",
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": pos.get("shares", 0),
                    "price": round(price, 2) if price > 0 else 0,
                    "reason": "被移出股息贵族名单",
                })
                logger.warning(f"🔴 L1 卖出: {symbol} — 不再属于股息贵族")
                continue

            # 健康检查
            decision = self._check_aristocrat_health(symbol)

            if decision == "SELL":
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.info or {}
                    price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
                except Exception:
                    price = pos.get("avg_cost", 0)

                orders.append({
                    "layer": "foundation",
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": pos.get("shares", 0),
                    "price": round(price, 2) if price > 0 else 0,
                    "reason": f"基本面恶化触发卖出",
                })
                logger.warning(f"🔴 L1 卖出: {symbol} — 基本面恶化")

            elif decision == "REDUCE":
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.info or {}
                    price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
                except Exception:
                    price = pos.get("avg_cost", 0)

                reduce_shares = max(1, pos.get("shares", 0) // 2)
                orders.append({
                    "layer": "foundation",
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": reduce_shares,
                    "price": round(price, 2) if price > 0 else 0,
                    "reason": f"PE过高或营收萎缩，减仓50%",
                })
                logger.warning(f"🟡 L1 减仓: {symbol} — 估值/营收走弱")

        if orders:
            logger.info(f"Layer1 生成 {len(orders)} 个卖出/减仓订单")
        return orders


_layer1_instance: Layer1Foundation = None

def get_layer1() -> Layer1Foundation:
    global _layer1_instance
    if _layer1_instance is None:
        _layer1_instance = Layer1Foundation()
    return _layer1_instance

def run_layer1() -> dict:
    return get_layer1().run()
