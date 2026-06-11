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

logger = get_logger("phoenix.layer3")


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
        用简化方法：追踪近期被内部人买入的股票。
        
        真实实现需要 SEC EDGAR API 或 whalewisdom.com 数据。
        此处简化为从公开源获取。
        """
        # 简化版：关注大额内幕交易的常见股票
        # 真实实现应该：
        # 1. 爬取 SEC Form 4 数据
        # 2. 筛选内幕买入
        # 3. 按买入金额和人数排名
        
        logger.info("检查内部人交易信号（简化版）...")
        
        # 检查是否有新闻
        try:
            import requests
            # 尝试获取 Yahoo Finance 内幕交易数据
            # 这个端点不稳定，作为尝试验证
            insider_scores = {}
            
            # 用做空比例作为代理信号（高做空 = 可能有轧空）
            # 这是间接替代，后续可以升级
            return insider_scores
        except Exception:
            pass
        
        logger.info("内部人追踪：暂无可用数据（需要 SEC API 接入）")
        return []

    def run_insider_tracking(self) -> dict:
        """执行内部人追踪"""
        insiders = self.fetch_insider_buys()
        
        result = {
            "type": "insider_tracking",
            "total_capital": self.capital * LAYER3.get("insider_pct", 0.20),
            "picks": [],
            "note": "需要 SEC EDGAR API 接入才能获取实时数据",
        }
        
        if insiders:
            for sym, score in list(insiders.items())[:LAYER3.get("insider_top_n", 3)]:
                result["picks"].append({"symbol": sym, "score": score})
        
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

    def get_orders(self) -> list[dict]:
        """生成需要执行的订单"""
        orders = []
        
        # 因子 ETF 轮动
        factor = self.run_factor_rotation()
        etf_capital = factor["total_capital"]
        if factor["picks"]:
            per_etf = etf_capital / len(factor["picks"])
            for pick in factor["picks"]:
                try:
                    stock = yf.Ticker(pick["symbol"])
                    price = float(stock.info.get("currentPrice", 0) or
                                 stock.history(period="1d")["Close"].iloc[-1])
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
            per_sec = sec_capital / len(sector["picks"])
            for pick in sector["picks"]:
                try:
                    stock = yf.Ticker(pick["symbol"])
                    price = float(stock.info.get("currentPrice", 0) or
                                 stock.history(period="1d")["Close"].iloc[-1])
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
        
        return orders


_layer3_instance: Layer3Tactical = None

def get_layer3() -> Layer3Tactical:
    global _layer3_instance
    if _layer3_instance is None:
        _layer3_instance = Layer3Tactical()
    return _layer3_instance

def run_layer3() -> dict:
    return get_layer3().run()
