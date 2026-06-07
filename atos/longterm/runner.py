# DEPRECATED — not imported by any active module.
"""
ATOS PRO v2 — 长期投资独立运行器
=================================
$10,000 虚拟资金，每月选股，长期持有。
独立于短期交易系统运行。
"""

import os, sys, json, datetime, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger
from atos.core.universe import ALL_SYMBOLS
from atos.longterm.engine import comprehensive_long_term_rank, build_long_term_portfolio
from atos.longterm.value_investor import calculate_intrinsic_value
import yfinance as yf

logger = get_logger("longterm.runner")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class LongTermPortfolio:
    def __init__(self, initial_cash: float = 10000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.holdings = {}  # {symbol: {shares, avg_cost, buy_date}}
        self.history = []    # 交易记录
        self.last_rebalance = None

    @property
    def total_value(self) -> float:
        pos_val = 0
        for sym, h in self.holdings.items():
            try:
                stock = yf.Ticker(sym)
                price = stock.fast_info.get("lastPrice", 0) if hasattr(stock, 'fast_info') else 0
                if price <= 0:
                    price = h["avg_cost"]
                pos_val += h["shares"] * price
            except Exception:
                pos_val += h["shares"] * h["avg_cost"]
        return self.cash + pos_val

    def rebalance(self, max_positions: int = 10):
        """每月调仓"""
        logger.info(f"长期组合月调仓开始 | 资金: ${self.total_value:,.0f}")

        # 筛选标的
        symbols = ALL_SYMBOLS[:30]  # 从流动池中选
        rankings = comprehensive_long_term_rank(symbols)
        pf = build_long_term_portfolio(rankings, max_positions=max_positions,
                                        min_composite=55)

        if not pf["positions"]:
            logger.info("无合格长期标的，保持现金")
            return

        per_position = self.total_value / len(pf["positions"])
        bought = 0

        for pos in pf["positions"]:
            sym = pos["symbol"]
            if sym in self.holdings:
                continue  # 已持有

            try:
                stock = yf.Ticker(sym)
                price = stock.fast_info.get("lastPrice", 0) if hasattr(stock, 'fast_info') else 0
                if price <= 0:
                    continue

                shares = int(per_position / price)
                if shares == 0 or shares * price > self.cash:
                    continue

                self.cash -= shares * price
                self.holdings[sym] = {
                    "shares": shares,
                    "avg_cost": round(price, 2),
                    "buy_date": datetime.date.today().isoformat(),
                    "composite_score": pos["composite_score"],
                }
                self.history.append({
                    "date": datetime.datetime.now().isoformat(),
                    "action": "BUY",
                    "symbol": sym,
                    "shares": shares,
                    "price": round(price, 2),
                    "cost": round(shares * price, 2),
                    "composite_score": pos["composite_score"],
                })
                bought += 1
            except Exception as e:
                logger.error(f"长期买入失败 {sym}: {e}")

        self.last_rebalance = datetime.date.today().isoformat()
        logger.info(f"长期调仓完成: 买入{bought}只 | 持仓{len(self.holdings)}只 | 现金${self.cash:,.0f}")

    def get_report(self) -> dict:
        holdings_detail = []
        for sym, h in self.holdings.items():
            try:
                stock = yf.Ticker(sym)
                price = stock.fast_info.get("lastPrice", 0) if hasattr(stock, 'fast_info') else h["avg_cost"]
                if price <= 0:
                    price = h["avg_cost"]
            except Exception:
                price = h["avg_cost"]

            pnl = (price - h["avg_cost"]) * h["shares"]
            pnl_pct = (price - h["avg_cost"]) / h["avg_cost"] if h["avg_cost"] > 0 else 0
            days_held = (datetime.date.today() - datetime.date.fromisoformat(h["buy_date"])).days

            holdings_detail.append({
                "symbol": sym,
                "shares": h["shares"],
                "avg_cost": h["avg_cost"],
                "current_price": round(price, 2),
                "market_value": round(h["shares"] * price, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
                "days_held": days_held,
            })

        return {
            "initial_capital": self.initial_cash,
            "cash": round(self.cash, 2),
            "total_value": round(self.total_value, 2),
            "total_return": round((self.total_value - self.initial_cash) / self.initial_cash, 4),
            "holdings_count": len(self.holdings),
            "holdings": holdings_detail,
            "last_rebalance": self.last_rebalance,
            "trade_count": len(self.history),
        }


# 全局单例
_long_term_pf = None
_state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "longterm_state.json")


def get_or_create_portfolio() -> LongTermPortfolio:
    global _long_term_pf
    if _long_term_pf is None:
        if os.path.exists(_state_file):
            try:
                with open(_state_file) as f:
                    saved = json.load(f)
                _long_term_pf = LongTermPortfolio(initial_cash=saved.get("initial_cash", 10000))
                _long_term_pf.cash = saved.get("cash", 10000)
                _long_term_pf.holdings = saved.get("holdings", {})
                _long_term_pf.history = saved.get("history", [])
                _long_term_pf.last_rebalance = saved.get("last_rebalance")
                logger.info(f"加载长期组合: ${_long_term_pf.total_value:,.0f} | {len(_long_term_pf.holdings)}只")
            except Exception:
                _long_term_pf = LongTermPortfolio(initial_cash=10000)
        else:
            _long_term_pf = LongTermPortfolio(initial_cash=10000)
    return _long_term_pf


def save_state():
    pf = get_or_create_portfolio()
    state = {
        "initial_cash": pf.initial_cash,
        "cash": pf.cash,
        "holdings": pf.holdings,
        "history": pf.history,
        "last_rebalance": pf.last_rebalance,
    }
    os.makedirs(os.path.dirname(_state_file), exist_ok=True)
    with open(_state_file, "w") as f:
        json.dump(state, f, indent=2)


def run_longterm_cycle():
    """每月运行一次"""
    pf = get_or_create_portfolio()
    today = datetime.date.today()

    # 只在每月第一个周一调仓
    if pf.last_rebalance and today.day > 7:
        return pf.get_report()

    if pf.last_rebalance and (today - datetime.date.fromisoformat(pf.last_rebalance)).days < 25:
        return pf.get_report()

    pf.rebalance(max_positions=10)
    save_state()
    return pf.get_report()
