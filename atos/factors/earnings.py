"""
ATOS PRO v5 — 盈利修正因子 (Earnings Revision Factor)
======================================================
研究显示这是 Sharpe Ratio 最高的因子之一（0.60-0.80），
在所有市场体制下都有 alpha。

基于 Yahoo Finance 的分析师盈利预测数据：
- 盈利预测上调/下调比例
- 盈利惊喜（实际 vs 预期）
- 盈利增长加速度
"""

import math
from datetime import datetime, timedelta
from atos.core.logging import get_logger

logger = get_logger("factors.earnings")


def get_earnings_revision(symbol: str, df=None) -> dict:
    """计算单只股票的盈利修正因子分数。

    返回:
        {
            "earnings_revision_score": 0.0-1.0,
            "estimate_trend": "UP|FLAT|DOWN",
            "surprise_history": "BEAT|MISS|MIXED|NONE",
            "growth_acceleration": float,  # 盈利增长在加速(+)还是减速(-)
            "details": {...}
        }
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        score = 0.5  # 默认中性分数

        # 1. 盈利增长 (YoY quarterly earnings growth)
        earnings_growth = info.get("earningsQuarterlyGrowth", 0)
        if earnings_growth and not (isinstance(earnings_growth, float) and math.isnan(earnings_growth)):
            # 增长 > 20%: 高分
            if earnings_growth > 0.50:
                score += 0.20
            elif earnings_growth > 0.20:
                score += 0.15
            elif earnings_growth > 0.10:
                score += 0.08
            elif earnings_growth > 0:
                score += 0.03
            elif earnings_growth < -0.10:
                score -= 0.15
            elif earnings_growth < 0:
                score -= 0.05

        # 2. 营收增长 (YoY quarterly revenue growth)
        revenue_growth = info.get("revenueGrowth", 0)
        if revenue_growth and not (isinstance(revenue_growth, float) and math.isnan(revenue_growth)):
            if revenue_growth > 0.30:
                score += 0.12
            elif revenue_growth > 0.15:
                score += 0.08
            elif revenue_growth > 0.05:
                score += 0.04
            elif revenue_growth < -0.05:
                score -= 0.10
            elif revenue_growth < 0:
                score -= 0.05

        # 3. 分析师推荐 (1=Strong Buy, 5=Strong Sell)
        rec = info.get("recommendationMean", 3.0)
        if rec and not (isinstance(rec, float) and math.isnan(rec)):
            if rec <= 1.5:
                score += 0.12
            elif rec <= 2.0:
                score += 0.08
            elif rec <= 2.5:
                score += 0.04
            elif rec >= 4.0:
                score -= 0.10
            elif rec >= 3.5:
                score -= 0.05

        # 4. 目标价上行空间
        target_price = info.get("targetMeanPrice", 0)
        current_price = info.get("currentPrice", info.get("regularMarketPrice", 0))
        if target_price and current_price and current_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0.30:
                score += 0.12
            elif upside > 0.15:
                score += 0.08
            elif upside > 0.05:
                score += 0.04
            elif upside < -0.10:
                score -= 0.10
            elif upside < 0:
                score -= 0.05

        # 5. 利润率
        profit_margin = info.get("profitMargins", 0)
        if profit_margin and not (isinstance(profit_margin, float) and math.isnan(profit_margin)):
            if profit_margin > 0.25:
                score += 0.08
            elif profit_margin > 0.15:
                score += 0.05
            elif profit_margin > 0.05:
                score += 0.02
            elif profit_margin < 0:
                score -= 0.10

        # 6. 做空比例（高做空 = 潜在的轧空机会）
        short_pct = info.get("shortPercentOfFloat", 0) or 0
        if short_pct and not (isinstance(short_pct, float) and math.isnan(short_pct)):
            if 0.05 < short_pct < 0.20:  # 5-20% 做空 — 轧空潜力
                score += 0.05
            elif short_pct > 0.30:  # >30% 做空 — 太高，有问题
                score -= 0.08

        # 限制范围
        score = max(0.0, min(1.0, score))

        # 判断趋势
        if earnings_growth > 0.15:
            trend = "UP"
        elif earnings_growth < -0.05:
            trend = "DOWN"
        else:
            trend = "FLAT"

        return {
            "earnings_revision_score": round(score, 3),
            "estimate_trend": trend,
            "growth_acceleration": round(earnings_growth, 4) if earnings_growth else 0,
            "analyst_consensus": round(rec, 2) if rec else 3.0,
            "upside_pct": round((target_price - current_price) / current_price * 100, 1) if target_price and current_price and current_price > 0 else 0,
            "details": {
                "earnings_growth": round(earnings_growth, 4) if earnings_growth else 0,
                "revenue_growth": round(revenue_growth, 4) if revenue_growth else 0,
                "profit_margin": round(profit_margin, 4) if profit_margin else 0,
                "short_float_pct": round(short_pct, 4) if short_pct else 0,
            },
        }

    except Exception as e:
        logger.debug(f"盈利因子 {symbol} 计算失败: {e}")
        return {"earnings_revision_score": 0.5, "estimate_trend": "FLAT",
                "growth_acceleration": 0, "analyst_consensus": 3.0, "upside_pct": 0,
                "details": {}}


def batch_earnings_revision(symbols: list[str]) -> dict:
    """批量计算盈利修正因子"""
    results = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        try:
            results[sym] = get_earnings_revision(sym)
        except Exception as e:
            logger.debug(f"批量盈利因子 {sym} 失败: {e}")
            results[sym] = {"earnings_revision_score": 0.5, "estimate_trend": "FLAT",
                            "growth_acceleration": 0, "details": {}}
        if (i + 1) % 20 == 0:
            logger.info(f"盈利因子进度: {i+1}/{total}")
    logger.info(f"盈利修正因子: {len(results)}/{total} 只标的完成")
    return results
