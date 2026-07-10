"""
ATOS PRO v2 — 市场温度计模块
==============================
Howard Marks 风格的多维度市场状态评估。
综合判断当前市场处于周期什么位置。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
from atos.core.logging import get_logger

logger = get_logger("phoenix.market_thermometer")

# Futu 数据源（优先使用）
try:
    from atos.data.futu_provider import get_futu
    _futu = get_futu()
except Exception:
    _futu = None


class MarketThermometer:
    """
    市场温度计 — 7 维度综合评分。
    
    返回 -100（极度悲观/最佳买入点）到 +100（极度乐观/最佳卖出点）。
    
    使用温度值自动调整各层的仓位水平。
    """

    def __init__(self):
        self.cache = {}
        self.cache_expiry = datetime.timedelta(hours=4)

    def _get_cached_or_fetch(self, key: str, fetch_fn, max_age_hours: int = 4):
        """缓存结果，避免频繁请求"""
        now = datetime.datetime.now()
        if key in self.cache:
            val, ts = self.cache[key]
            if (now - ts) < datetime.timedelta(hours=max_age_hours):
                return val
        val = fetch_fn()
        self.cache[key] = (val, now)
        return val

    def get_sp500_pe(self) -> float:
        """获取标普 500 整体 PE（Futu优先，yfinance后备）"""
        def _fetch():
            if _futu:
                pe = _futu.get_sp500_pe()
                if pe > 0:
                    return pe
            spy = yf.Ticker("SPY")
            info = spy.info or {}
            pe = info.get("trailingPE", 0) or info.get("forwardPE", 0) or 0
            if pe > 0:
                return float(pe)
            voo = yf.Ticker("VOO")
            info2 = voo.info or {}
            return float(info2.get("trailingPE", 20) or 20)
        return self._get_cached_or_fetch("sp500_pe", _fetch, 24)

    def get_vix(self) -> float:
        """获取 VIX 恐慌指数（Futu优先，yfinance后备）"""
        def _fetch():
            if _futu:
                v = _futu.get_vix()
                if 5 < v < 100:
                    return v
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="5d", interval="1d")
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[-1])
            return 20.0
        return self._get_cached_or_fetch("vix", _fetch, 2)

    def get_yield_curve(self) -> float:
        """10年期 - 3月期国债利差（基点）"""
        def _fetch():
            try:
                tnx = yf.Ticker("^TNX")
                irx = yf.Ticker("^IRX")
                tnx_hist = tnx.history(period="5d", interval="1d")
                irx_hist = irx.history(period="5d", interval="1d")
                if tnx_hist is not None and not tnx_hist.empty and irx_hist is not None and not irx_hist.empty:
                    tnx_yield = float(tnx_hist["Close"].iloc[-1])
                    irx_yield = float(irx_hist["Close"].iloc[-1])
                    return tnx_yield - irx_yield
            except Exception:
                pass
            # 备用
            try:
                spy = yf.Ticker("SPY")
                info = spy.info or {}
                return float(info.get("yield", 0) or 0)
            except Exception:
                return 0.0
        return self._get_cached_or_fetch("yield_curve", _fetch, 6)

    def get_sp500_sma200_pct(self) -> float:
        """标普 500 当前价格相对 200 日均线的位置百分比（Futu K线优先）"""
        def _fetch():
            if _futu:
                pct = _futu.get_sp500_ma200_pct()
                if abs(pct) < 0.5:  # 合理范围
                    return pct
            spy = yf.Ticker("SPY")
            hist = spy.history(period="1y", interval="1d")
            if hist is None or hist.empty:
                return 0.0
            close = hist["Close"].squeeze()
            if len(close) < 200:
                return 0.0
            sma200 = close.rolling(200).mean().iloc[-1]
            current = float(close.iloc[-1])
            return (current - sma200) / sma200
        return self._get_cached_or_fetch("sp500_sma200", _fetch, 12)

    def get_investor_sentiment(self) -> float:
        """
        投资者情绪代理指标。
        正 = 乐观，负 = 悲观。
        用信用利差（高收益债利差）作为代理。
        """
        def _fetch():
            hyg = yf.Ticker("HYG")
            ief = yf.Ticker("IEF")
            try:
                hyg_hist = hyg.history(period="1mo", interval="1d")
                ief_hist = ief.history(period="1mo", interval="1d")
                if hyg_hist is not None and not hyg_hist.empty and ief_hist is not None and not ief_hist.empty:
                    hyg_close = float(hyg_hist["Close"].iloc[-1])
                    ief_close = float(ief_hist["Close"].iloc[-1])
                    ratio = hyg_close / ief_close if ief_close > 0 else 1
                    # 利差扩大 = 悲观，利差缩小 = 乐观
                    hyg_ma20 = float(hyg_hist["Close"].squeeze().rolling(20).mean().iloc[-1])
                    return float(hyg_close / hyg_ma20 - 1) * 100
            except Exception:
                pass
            return 0.0
        return self._get_cached_or_fetch("sentiment", _fetch, 24)

    def get_buffett_indicator(self) -> float:
        """
        巴菲特指标：Wilshire 5000 总市值 / GNP。
        用 SPY 市值 × 流通股近似。
        """
        def _fetch():
            try:
                spy = yf.Ticker("SPY")
                info = spy.info or {}
                market_cap = info.get("marketCap", 0) or 0
                # SPY 跟踪标普500，市值不准确；用典型值百分比
                # 标普500总市值约 45万亿，GDP约 28万亿
                # 比值=160%为"充分估值"
                # 直接用 SPY 价格涨跌反映趋势
                hist = spy.history(period="5y", interval="1mo")
                if hist is not None and not hist.empty:
                    close = hist["Close"].squeeze()
                    pct_5y = float(close.iloc[-1] / close.iloc[0] - 1)
                    # 转成类似巴菲特指标的感觉：> 0 高估，< 0 低估
                    return pct_5y * 2
            except Exception:
                pass
            return 0.0
        return self._get_cached_or_fetch("buffett", _fetch, 168)  # 每周

    def get_momentum_score(self) -> float:
        """6 个月动量分数"""
        def _fetch():
            spy = yf.Ticker("SPY")
            hist = spy.history(period="1y", interval="1mo")
            if hist is None or hist.empty:
                return 0.0
            close = hist["Close"].squeeze()
            if len(close) < 6:
                return 0.0
            mom_6m = float(close.iloc[-1] / close.iloc[-6] - 1)
            mom_3m = float(close.iloc[-1] / close.iloc[-3] - 1)
            mom_1m = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) > 1 else 0
            # 加权：近期动量权重更大
            return mom_1m * 0.5 + mom_3m * 0.3 + mom_6m * 0.2
        return self._get_cached_or_fetch("momentum", _fetch, 6)

    def comprehensive_score(self, fast: bool = False) -> dict:
        """
        综合市场温度评分。

        Args:
            fast: True=只用Futu快速指标（<3秒），False=完整7维度（~60秒）

        返回：
            score: -100~+100
            -100 = 极度悲观（最佳买入区）
            +100 = 极度乐观（最佳卖出区）
        """
        if fast:
            return self._fast_score()

        logger.info("计算市场温度综合评分...")

        pe = self.get_sp500_pe()
        vix = self.get_vix()
        sma200 = self.get_sp500_sma200_pct()
        yield_curve = self.get_yield_curve()
        sentiment = self.get_investor_sentiment()
        buffett = self.get_buffett_indicator()
        momentum = self.get_momentum_score()

        return self._compute_score(pe, vix, sma200, yield_curve, sentiment, buffett, momentum)

    def _fast_score(self) -> dict:
        """
        快速评分（仅用Futu能瞬间获取的3个核心指标）。
        PE(30%) + VIX(30%) + SMA200(40%)
        """
        pe = self.get_sp500_pe()
        vix = self.get_vix()
        sma200 = self.get_sp500_sma200_pct()

        return self._compute_score(pe, vix, sma200,
                                    yield_curve=0, sentiment=0,
                                    buffett=0, momentum=0,
                                    fast=True)

    def _compute_score(self, pe, vix, sma200, yield_curve, sentiment, buffett, momentum,
                       fast: bool = False) -> dict:
        """计算综合评分（内部方法）"""
        scores = {}

        # 1. PE 评分
        if pe > 0:
            pe_score = max(-100, min(100, (20 - pe) * 4))
        else:
            pe_score = 0
        scores["pe"] = pe_score

        # 2. VIX 评分
        vix_score = max(-100, min(100, (30 - vix) * 3))
        scores["vix"] = vix_score

        # 3. 相对 200日均线评分
        sma_score = max(-100, min(100, -sma200 * 200))
        scores["sma200"] = sma_score

        # 4-7. 慢速指标（fast模式跳过）
        if not fast:
            curve_score = max(-100, min(100, yield_curve * 20))
            scores["yield_curve"] = curve_score
            sent_score = max(-100, min(100, -sentiment * 5))
            scores["sentiment"] = sent_score
            buffett_score = max(-100, min(100, -buffett * 30))
            scores["buffett"] = buffett_score
            mom_score = max(-100, min(100, -momentum * 200))
            scores["momentum"] = mom_score

        # 综合权重
        if fast:
            weights = {"pe": 0.30, "vix": 0.30, "sma200": 0.40}
        else:
            weights = {"pe": 0.20, "vix": 0.20, "sma200": 0.15,
                       "yield_curve": 0.15, "sentiment": 0.10,
                       "buffett": 0.10, "momentum": 0.10}

        total = sum(scores[k] * weights[k] for k in weights if k in scores)
        market_phase = self._classify_phase(total)

        result = {
            "score": round(total, 1),
            "phase": market_phase,
            "sub_scores": {k: round(v, 1) for k, v in scores.items()},
            "fast_mode": fast,
            "raw_data": {
                "sp500_pe": round(pe, 1) if pe else None,
                "vix": round(vix, 1) if vix else None,
                "sma200_pct": round(sma200 * 100, 1),
            }
        }

        logger.info(f"市场温度: {result['score']} — {market_phase}" + (" [快速]" if fast else ""))
        return result

    def _classify_phase(self, score: float) -> str:
        """分数 → 市场阶段"""
        if score < -60:
            return "EXTREME_PESSIMISM"     # 极度悲观（历史大底区域）
        if score < -30:
            return "PESSIMISM"             # 悲观（买入区）
        if score < -10:
            return "SLIGHT_PESSIMISM"      # 略悲观（正常买入区）
        if score < 10:
            return "NEUTRAL"               # 中性
        if score < 30:
            return "SLIGHT_OPTIMISM"       # 略乐观
        if score < 60:
            return "OPTIMISM"              # 乐观（减仓区）
        return "EXTREME_OPTIMISM"          # 极度乐观（泡沫区）

    def get_position_adjustment_factor(self) -> float:
        """根据市场温度返回仓位调整系数（使用缓存评分，不重新获取）"""
        # 先获取已缓存的评分
        score = self.comprehensive_score()["score"]
        if score < -60: return 1.5
        if score < -30: return 1.3
        if score < -10: return 1.1
        if score < 10:  return 1.0
        if score < 30:  return 0.85
        if score < 60:  return 0.6
        return 0.3


def get_market_thermometer() -> dict:
    """便捷入口"""
    mt = MarketThermometer()
    return mt.comprehensive_score()


def get_position_adjustment() -> float:
    """便捷入口：获取仓位调整系数"""
    mt = MarketThermometer()
    return mt.get_position_adjustment_factor()
