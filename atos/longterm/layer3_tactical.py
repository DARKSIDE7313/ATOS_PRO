"""
ATOS PRO v2 — Phoenix Layer 3: 战术层
=========================================
四种子策略：
  1. 因子 ETF 轮动 — 在 6 个核心因子 ETF 中轮动
  2. 行业轮动 — 在 11 个板块 ETF 中轮动
  3. 内部人追踪 — 跟随公司高管的买入信号
  4. 回撤买入 — 在大跌时自动抄底

每月运行一次（回撤检查每小时）。
目标年化：20-30%，抓住超额收益。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
from atos.core.logging import get_logger
from atos.longterm.config import LAYER3, CAPITAL

try:
    from atos.data.futu_provider import get_stock_info as _futu_info
except ImportError:
    _futu_info = None

logger = get_logger("phoenix.layer3")


def _get_info(symbol: str) -> dict:
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


class Layer3Tactical:
    """
    战术层：
    - 40% 因子 ETF 轮动
    - 30% 行业轮动
    - 20% 内部人追踪
    - 10% 回撤买入待命
    """

    def __init__(self):
        self.capital = CAPITAL["total"] * CAPITAL["layer3_pct"]
        self.positions = {}
        self.last_rotation = None

    # ── 1. 因子 ETF 轮动 ──

    def get_factor_etfs(self) -> dict:
        """可用因子 ETF 列表"""
        return LAYER3.get("factor_etfs", {
            "USMV": "低波动", "QUAL": "质量", "SLYV": "小盘价值",
            "MTUM": "动量", "VLUE": "价值", "SPHQ": "质量加权",
        })

    def calc_momentum(self, symbols: list[str], lookback_days: int = None) -> list[tuple]:
        """
        计算 N 个月的动量评分。
        
        返回：[(symbol, momentum_score, annualized_return), ...]
        按动量从高到低排序。
        """
        if lookback_days is None:
            months = LAYER3.get("factor_momentum_months", 3)
            lookback_days = months * 21  # 每月约 21 个交易日
        
        results = []
        for sym in symbols:
            try:
                etf = yf.Ticker(sym)
                hist = etf.history(period="6mo", interval="1d")
                if hist is None or hist.empty or len(hist) < lookback_days:
                    results.append((sym, -999, 0))
                    continue
                
                close = hist["Close"].squeeze()
                current = float(close.iloc[-1])
                past = float(close.iloc[-lookback_days]) if len(close) > lookback_days else float(close.iloc[0])
                
                if past <= 0:
                    result = 0.0
                else:
                    result = (current - past) / past
                
                # 标准化到年化
                days = min(lookback_days, len(close))
                annualized = result * (252 / days)
                
                results.append((sym, round(result, 4), round(annualized, 4)))
            except Exception as e:
                logger.debug(f"动量计算失败 {sym}: {e}")
                results.append((sym, -999, 0))
        
        results.sort(key=lambda x: -x[1])  # 按动量降序
        return results

    def run_factor_rotation(self) -> dict:
        """执行因子 ETF 轮动"""
        etfs = self.get_factor_etfs()
        symbols = list(etfs.keys())
        momentum = self.calc_momentum(symbols)
        
        top_n = LAYER3.get("factor_top_n", 2)
        best = momentum[:top_n]
        
        logger.info(f"因子轮动 Top {top_n}:")
        picks = []
        for sym, mom, ann in best:
            desc = etfs.get(sym, sym)
            logger.info(f"  {sym} ({desc}): {mom*100:.1f}% (年化 {ann*100:.1f}%)")
            picks.append({
                "symbol": sym,
                "description": desc,
                "momentum_3m": round(mom * 100, 2),
                "annualized": round(ann * 100, 2),
            })
        
        return {
            "type": "factor_rotation",
            "total_capital": self.capital * LAYER3.get("factor_rotation_pct", 0.40),
            "picks": picks,
        }

    # ── 2. 行业轮动 ──

    def get_sector_etfs(self) -> dict:
        return LAYER3.get("sector_etfs", {
            "XLK": "科技", "XLF": "金融", "XLV": "医疗",
            "XLE": "能源", "XLI": "工业", "XLP": "必选消费",
            "XLY": "可选消费", "XLU": "公用事业", "XLRE": "房地产",
            "XLB": "材料", "XLC": "通信",
        })

    def run_sector_rotation(self) -> dict:
        """执行行业轮动"""
        sectors = self.get_sector_etfs()
        if not isinstance(sectors, dict):
            sectors = {"XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLE": "能源",
                       "XLI": "工业", "XLP": "必选消费", "XLY": "可选消费",
                       "XLU": "公用事业", "XLRE": "房地产", "XLB": "材料", "XLC": "通信"}
        momentum = self.calc_momentum(list(sectors.keys()), 63)  # 3 个月
        
        top_n = LAYER3.get("sector_top_n", 1)
        best = momentum[:top_n]
        
        picks = []
        for sym, mom, ann in best:
            desc = sectors.get(sym, sym)
            picks.append({
                "symbol": sym, "description": desc,
                "momentum_3m": round(mom * 100, 2),
            })
        
        return {
            "type": "sector_rotation",
            "total_capital": self.capital * LAYER3.get("sector_rotation_pct", 0.30),
            "picks": picks,
        }

    # ── 3. 内部人追踪 ──

    def fetch_insider_buys(self) -> list[dict]:
        """
        获取最近的内部人大额买入信号。
        使用 yfinance 的 insider_transactions 数据。

        返回: [{symbol, insider_name, shares, value, transaction_date, score}, ...]
        """
        logger.info("检查内部人交易信号...")
        insider_picks = []

        # 扫描已知的高质量标的池
        scan_symbols = [
            "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
            "JPM", "BAC", "GS", "V", "MA", "JNJ", "UNH", "COST",
            "AMD", "AVGO", "CRM", "ADBE", "NFLX", "DIS", "CAT",
        ]

        for sym in scan_symbols[:20]:  # 限制扫描量
            try:
                stock = yf.Ticker(sym)
                # yfinance 提供 insider_transactions 属性
                if hasattr(stock, 'insider_transactions') and stock.insider_transactions is not None:
                    df = stock.insider_transactions
                    if df is None or (hasattr(df, 'empty') and df.empty):
                        continue

                    # 过滤买入交易
                    for _, row in df.iterrows():
                        try:
                            transaction = str(row.get("transactionText", row.get("Transaction", ""))).lower()
                            shares = row.get("shares", row.get("Shares", 0))
                            value = row.get("value", row.get("Value", 0))
                            insider = str(row.get("insider", row.get("Insider", "")))

                            # 只关注买入
                            if any(kw in transaction for kw in ["buy", "purchase", "acquisition", "acquire"]):
                                if shares and float(shares) > 0:
                                    insider_picks.append({
                                        "symbol": sym,
                                        "insider": insider,
                                        "shares": float(shares),
                                        "value": float(value) if value else 0,
                                        "transaction": transaction,
                                        "score": min(100, float(shares) / 1000),
                                    })
                        except Exception:
                            continue

            except Exception as e:
                logger.debug(f"内部人扫描 {sym}: {e}")
                continue

        # 按买入股数排序
        insider_picks.sort(key=lambda x: -x["shares"])
        top = insider_picks[:LAYER3.get("insider_top_n", 3)]

        if top:
            logger.info(f"内部人追踪: 发现 {len(insider_picks)} 笔买入, Top3: {[(p['symbol'], p['insider']) for p in top]}")
            # 缓存供后续使用
            self._cached_insider_picks = top
        else:
            logger.info("内部人追踪: 本期无符合条件的买入信号")

        return top

    def run_insider_tracking(self) -> dict:
        """执行内部人追踪"""
        insiders = self.fetch_insider_buys()

        result = {
            "type": "insider_tracking",
            "total_capital": self.capital * LAYER3.get("insider_pct", 0.20),
            "picks": [],
        }

        for pick in insiders:
            result["picks"].append({
                "symbol": pick["symbol"],
                "insider": pick.get("insider", ""),
                "shares": pick.get("shares", 0),
                "score": pick.get("score", 0),
            })

        return result

    # ── 4. 回撤买入 ──

    def run_dip_buying(self) -> dict:
        """检查是否需要回撤买入（由 cash_manager 统一管理）"""
        result = {
            "type": "dip_buying",
            "total_capital": 0,  # 现金部署由 CashManager 集中管理
            "triggered": False,
            "note": "已迁移到 CashManager（atos.longterm.cash_manager）",
        }
        return result

    # ── 综合运行 ──

    def run(self) -> dict:
        """
        执行 Layer 3 战术层。
        
        返回 4 个子策略的结果。
        """
        logger.info("═══════ Layer 3: 战术层开始 ═══════")
        
        factor = self.run_factor_rotation()
        sector = self.run_sector_rotation()
        insider = self.run_insider_tracking()
        dip = self.run_dip_buying()
        
        self.last_rotation = datetime.date.today()
        
        result = {
            "layer": "tactical",
            "timestamp": datetime.datetime.now().isoformat(),
            "total_capital": self.capital,
            "factor_rotation": factor,
            "sector_rotation": sector,
            "insider_tracking": insider,
            "dip_buying": dip,
        }
        
        logger.info("═══════ Layer 3: 战术层完成 ═══════")
        return result

    def get_buy_orders(self, existing_positions: dict = None) -> list[dict]:
        """生成买入订单，跳过已持有的标的"""
        if existing_positions is None:
            existing_positions = {}

        orders = []
        existing_symbols = set(existing_positions.keys())

        # 因子 ETF 轮动
        factor = self.run_factor_rotation()
        etf_capital = factor["total_capital"]
        if factor["picks"]:
            new_factor = [p for p in factor["picks"] if p["symbol"] not in existing_symbols]
            if new_factor:
                per_etf = etf_capital / len(new_factor)
                for pick in new_factor:
                    try:
                        price = _get_price(pick["symbol"])  # v11
                        if price > 0:
                            shares = max(1, int(per_etf / price))
                            orders.append({
                                "layer": "tactical",
                                "sub": "factor_rotation",
                                "symbol": pick["symbol"],
                                "action": "BUY",
                                "quantity": shares,
                                "price": round(price, 2),
                                "reason": f"因子轮动 {pick['description']} "
                                         f"动量 {pick['momentum_3m']}%",
                            })
                    except Exception as e:
                        logger.warning(f"因子订单 {pick['symbol']}: {e}")

        # 行业轮动
        sector = self.run_sector_rotation()
        sec_capital = sector["total_capital"]
        if sector["picks"]:
            new_sector = [p for p in sector["picks"] if p["symbol"] not in existing_symbols]
            if new_sector:
                per_sec = sec_capital / len(new_sector)
                for pick in new_sector:
                    try:
                        price = _get_price(pick["symbol"])  # v11
                        if price > 0:
                            shares = max(1, int(per_sec / price))
                            orders.append({
                                "layer": "tactical",
                                "sub": "sector_rotation",
                                "symbol": pick["symbol"],
                                "action": "BUY",
                                "quantity": shares,
                                "price": round(price, 2),
                                "reason": f"行业轮动 {pick['description']}",
                            })
                    except Exception as e:
                        logger.warning(f"行业订单 {pick['symbol']}: {e}")

        # 内部人追踪
        insider_result = self.run_insider_tracking()
        if insider_result.get("picks"):
            insider_capital = insider_result["total_capital"]
            new_insider = [p for p in insider_result["picks"] if p["symbol"] not in existing_symbols]
            if new_insider:
                per_insider = insider_capital / len(new_insider)
                for pick in new_insider:
                    try:
                        price = _get_price(pick["symbol"])  # v11
                        if price > 0:
                            shares = max(1, int(per_insider / price))
                            orders.append({
                                "layer": "tactical",
                                "sub": "insider_tracking",
                                "symbol": pick["symbol"],
                                "action": "BUY",
                                "quantity": shares,
                                "price": round(price, 2),
                                "reason": f"内部人买入: {pick.get('insider', '高管')}",
                            })
                    except Exception as e:
                        logger.warning(f"内部人订单 {pick['symbol']}: {e}")

        return orders

    # Legacy alias
    def get_orders(self) -> list[dict]:
        return self.get_buy_orders()

    # ── 卖出检查 ──

    def get_sell_orders(self, positions: dict) -> list[dict]:
        """
        检查 Layer 3 持仓，生成卖出订单。

        卖出触发：
          1. 因子 ETF: 不再在 Top N 动量排名中
          2. 行业 ETF: 不再在 Top N 动量排名中
          3. 内部人: 持有超过 180 天
          4. 止损: 亏损超过 15%
        """
        orders = []

        # 获取当前最优排名（用于对比）
        current_factor_picks = {p["symbol"] for p in self.run_factor_rotation().get("picks", [])}
        current_sector_picks = {p["symbol"] for p in self.run_sector_rotation().get("picks", [])}

        for symbol, pos in positions.items():
            if pos.get("layer") != "tactical":
                continue

            sub = pos.get("sub", "")
            try:
                stock = yf.Ticker(symbol)
                info = stock.info or {}
                current_price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
            except Exception:
                current_price = pos.get("avg_cost", 0)

            avg_cost = pos.get("avg_cost", 0)
            pnl_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0

            sell_reason = None
            sell_shares = pos.get("shares", 0)

            # 1. 止损检查（所有子策略通用）
            if pnl_pct < -0.15:
                sell_reason = f"止损: 亏损 {pnl_pct*100:.1f}%"

            # 2. 因子 ETF: 检查是否还在 Top 排名中
            elif sub == "factor_rotation":
                if symbol not in current_factor_picks:
                    sell_reason = f"因子动量排名下降，退出轮动"

            # 3. 行业 ETF: 检查是否还在 Top 排名中
            elif sub == "sector_rotation":
                if symbol not in current_sector_picks:
                    sell_reason = f"行业动量排名下降，退出轮动"

            # 4. 内部人追踪: 持有时间超限
            elif sub == "insider_tracking":
                buy_date_str = pos.get("buy_date", "")
                if buy_date_str:
                    try:
                        buy_date = datetime.date.fromisoformat(buy_date_str)
                        days_held = (datetime.date.today() - buy_date).days
                        if days_held > LAYER3.get("insider_max_hold", 180):
                            sell_reason = f"内部人追踪持有 {days_held} 天，超过 {LAYER3.get('insider_max_hold', 180)} 天上限"
                    except Exception:
                        pass

            if sell_reason:
                orders.append({
                    "layer": "tactical",
                    "sub": sub,
                    "symbol": symbol,
                    "action": "SELL",
                    "quantity": sell_shares,
                    "price": round(current_price, 2) if current_price > 0 else 0,
                    "reason": sell_reason,
                })
                logger.warning(f"🔴 L3 卖出: {symbol} — {sell_reason}")

        if orders:
            logger.info(f"Layer3 生成 {len(orders)} 个卖出订单")
        return orders


_layer3_instance: Layer3Tactical = None

def get_layer3() -> Layer3Tactical:
    global _layer3_instance
    if _layer3_instance is None:
        _layer3_instance = Layer3Tactical()
    return _layer3_instance

def run_layer3() -> dict:
    return get_layer3().run()
