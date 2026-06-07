"""
ATOS PRO v2 — 机构级长期投资引擎
===================================
融合三大流派：
  1. Greenblatt 神奇公式 — EBIT/EV + ROC 双排名（质量+便宜）
  2. Klarman/Marks — 安全边际+催化剂+反周期+现金是弹药
  3. Fama-French-Carhart 多因子 — Market/SMB/HML/MOM/RMW/CMA

策略：每月排名 → 选 Top 20-30 → 每只持有 1 年 → 滚动换仓
目标：年化 15-25%，最大回撤 < 25%，5 年以上周期
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from atos.core.logging import get_logger

logger = get_logger("longterm.engine")

# yfinance 缓存 — 5分钟内不重复下载（适用于 engine.py 内所有 yfinance 调用）
_LT_CACHE = {}
_LT_CACHE_TTL = timedelta(minutes=5)

def _get_cached_lt(symbol: str, period="1y", interval="1d"):
    """带缓存的 yfinance 下载，同一标的 5 分钟内只下载一次"""
    key = f"lt:{symbol}:{period}:{interval}"
    now = datetime.now()
    if key in _LT_CACHE:
        ts, df = _LT_CACHE[key]
        if now - ts < _LT_CACHE_TTL:
            return df
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    _LT_CACHE[key] = (datetime.now(), df)
    return df


# ═══════════════════════════════════════════
# 1. Greenblatt 神奇公式
# ═══════════════════════════════════════════

def magic_formula_rank(symbols: list[str]) -> list[dict]:
    """
    Greenblatt 神奇公式：
    1. 按 EBIT/EV（便宜度）排名
    2. 按 ROC（质量）排名
    3. 两个排名相加 → 总分越低越好
    """
    results = []
    for sym in symbols:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}

            ebit = info.get("ebitda", 0)  # 近似用 EBITDA
            ev = info.get("enterpriseValue", 0)
            total_assets = info.get("totalAssets", 0)
            current_assets = info.get("totalCurrentAssets", 0)
            current_liab = info.get("totalCurrentLiabilities", 0)
            cash = info.get("totalCash", 0)
            goodwill = info.get("goodwill", 0) or 0
            intangibles = info.get("intangibleAssets", 0) or 0
            market_cap = info.get("marketCap", 0)
            total_debt = info.get("totalDebt", 0)
            ltm_revenue = info.get("totalRevenue", 0)

            if ev <= 0 or market_cap <= 0 or ebit <= 0:
                continue

            # 1) Earnings Yield = EBIT / Enterprise Value
            earnings_yield = ebit / ev

            # 2) Return on Capital = EBIT / (Net Fixed Assets + Net Working Capital)
            net_fixed = total_assets - current_assets - goodwill - intangibles
            net_fixed = max(net_fixed, 0)
            excess_cash = max(cash - ltm_revenue * 0.05, 0) if ltm_revenue > 0 else 0
            net_working_capital = max(current_assets - excess_cash - current_liab, 0)
            invested_capital = net_fixed + net_working_capital
            roc = ebit / invested_capital if invested_capital > 0 else 0

            results.append({
                "symbol": sym,
                "earnings_yield": round(earnings_yield, 4),
                "roc": round(roc, 4),
                "market_cap": market_cap,
                "ev": ev,
                "ebit": ebit,
            })
        except Exception:
            continue

    if len(results) < 2:
        return results

    # 排名
    results.sort(key=lambda x: x["earnings_yield"], reverse=True)
    for i, r in enumerate(results):
        r["ey_rank"] = i + 1

    results.sort(key=lambda x: x["roc"], reverse=True)
    for i, r in enumerate(results):
        r["roc_rank"] = i + 1

    for r in results:
        r["magic_score"] = r["ey_rank"] + r["roc_rank"]

    results.sort(key=lambda x: x["magic_score"])

    logger.info(f"神奇公式排名完成: {len(results)} 只, Top3: "
                f"{[(r['symbol'], r['magic_score']) for r in results[:3]]}")

    return results


# ═══════════════════════════════════════════
# 2. Klarman 安全边际 + 催化剂评分
# ═══════════════════════════════════════════

def klarman_margin_check(symbol: str) -> dict:
    """
    Klarman 风格的综合安全检查：
    - 安全边际有多大？
    - 有没有催化剂？
    - 下行风险是什么？
    """
    try:
        stock = yf.Ticker(symbol)
        info = stock.info or {}
    except Exception:
        return {"symbol": symbol, "klarman_score": 0}

    price = info.get("currentPrice", 0)
    book_value = info.get("bookValue", 0)
    cash_per_share = info.get("totalCashPerShare", 0)
    debt_to_equity = info.get("debtToEquity", 0)
    fcf = info.get("freeCashflow", 0)
    shares_out = info.get("sharesOutstanding", 1)
    revenue_growth = info.get("revenueGrowth", 0) or 0
    profit_margins = info.get("profitMargins", 0) or 0
    current_ratio = info.get("currentRatio", 0)
    short_ratio = info.get("shortRatio", 0) or 0  # 做空比例
    short_pct = info.get("shortPercentOfFloat", 0) or 0
    target_price = info.get("targetMeanPrice", 0) or 0

    score = 50

    # 安全边际
    if price > 0:
        if book_value > 0:
            pb = price / book_value
            if pb < 0.8:  score += 15   # 破净
            elif pb < 1.5: score += 5

        if cash_per_share > 0:
            cash_ratio = cash_per_share / price
            if cash_ratio > 0.5: score += 10  # 现金 > 股价一半
            elif cash_ratio > 0.3: score += 5

    # 催化剂（做空比例高 = 可能挤压空头）
    if short_pct > 15: score += 10   # 高做空 → 潜在轧空
    elif short_pct > 5: score += 3

    if target_price > price * 1.3: score += 8

    # 下行保护
    if current_ratio and current_ratio > 2: score += 5
    if debt_to_equity and debt_to_equity < 30: score += 5
    if fcf > 0: score += 5
    if profit_margins > 0.1: score += 5

    # 扣分
    if revenue_growth < -0.15: score -= 10
    if debt_to_equity and debt_to_equity > 200: score -= 15
    if profit_margins < -0.1: score -= 10
    if price <= 0: score = 0

    score = max(0, min(100, score))

    return {
        "symbol": symbol,
        "klarman_score": score,
        "price_to_book": round(price / book_value, 2) if book_value and book_value > 0 else None,
        "cash_ratio": round(cash_per_share / price, 2) if price and cash_per_share else None,
        "short_pct": round(short_pct, 1) if short_pct else None,
        "catalyst_present": bool(short_pct > 15 or (target_price and target_price > price * 1.3)),
    }


# ═══════════════════════════════════════════
# 3. Fama-French 多因子暴露估算
# ═══════════════════════════════════════════

def estimate_factor_exposures(symbol: str) -> dict:
    """
    估算标的核心因子暴露（简化版）。

    Fama-French-Carhart 六因子:
      Market (Beta) — 市场风险
      SMB — 小盘股溢价
      HML — 价值 vs 成长
      MOM — 动量
      RMW — 盈利能力
      CMA — 投资保守性
    """
    try:
        stock = yf.Ticker(symbol)
        info = stock.info or {}

        market_cap = info.get("marketCap", 0)
        pe = info.get("trailingPE") or info.get("forwardPE", 0)
        pb = info.get("priceToBook", 0)
        roe = info.get("returnOnEquity", 0) or 0
        debt_to_equity = info.get("debtToEquity", 0) or 0
        beta = info.get("beta", 1.0)

        # SMB: 市值越小分越高
        if market_cap < 2e9:       smb_score = 1.0  # 小盘
        elif market_cap < 10e9:    smb_score = 0.5  # 中盘
        else:                      smb_score = 0.0  # 大盘

        # HML: PB 越低越偏价值
        if pb and pb > 0:
            hml_score = max(0, min(1, 1 / pb / 5))  # PB=1 → 0.2, PB=0.5 → 0.4
        else:
            hml_score = 0.3

        # MOM: 用 Beta 近似（高 Beta 往往有动量）
        mom_score = min(1.0, max(0, (beta - 0.5) / 1.5)) if beta else 0.3

        # RMW: 盈利能力
        rmw_score = min(1.0, roe / 0.3) if roe and roe > 0 else 0.3

        # CMA: 投资保守性（低负债 = 高得分）
        cma_score = max(0, 1 - debt_to_equity / 200) if debt_to_equity else 0.5

        return {
            "symbol": symbol,
            "beta": round(beta, 2) if beta else 1.0,
            "smb": round(smb_score, 2),
            "hml": round(hml_score, 2),
            "mom": round(mom_score, 2),
            "rmw": round(rmw_score, 2),
            "cma": round(cma_score, 2),
        }
    except Exception:
        return {"symbol": symbol, "beta": 1.0}


# ═══════════════════════════════════════════
# 4. 综合长期排名
# ═══════════════════════════════════════════

def comprehensive_long_term_rank(symbols: list[str]) -> list[dict]:
    """
    三个框架综合打分：
      Greenblatt 40% — 便宜 + 质量
      Klarman   35% — 安全边际 + 催化剂
      FF 因子   25% — 多因子暴露合理性
    """
    magic = magic_formula_rank(symbols)
    magic_map = {r["symbol"]: r for r in magic}

    results = []
    for sym in symbols:
        if sym not in magic_map:
            continue

        klarman = klarman_margin_check(sym)
        factors = estimate_factor_exposures(sym)

        magic_data = magic_map[sym]

        # 归一化神奇公式得分（越低越好 → 反转）
        max_score = max(r["magic_score"] for r in magic) if magic else 100
        magic_norm = max(0, 100 - (magic_data["magic_score"] / max_score * 100))

        # 综合
        composite = (
            magic_norm * 0.40 +
            klarman["klarman_score"] * 0.35 +
            (factors.get("hml", 0.3) * 40 + factors.get("rmw", 0.3) * 30 +
             factors.get("cma", 0.3) * 30) * 0.25
        )

        decision = (
            "STRONG_LONG" if composite > 75 and klarman["klarman_score"] > 60
            else "LONG" if composite > 60
            else "WATCH" if composite > 40
            else "AVOID"
        )

        results.append({
            "symbol": sym,
            "composite_score": round(composite, 1),
            "decision": decision,
            "magic_rank": magic_data.get("magic_score"),
            "klarman_score": klarman["klarman_score"],
            "catalyst": klarman["catalyst_present"],
            "pb_ratio": klarman.get("price_to_book"),
            "short_interest": klarman.get("short_pct"),
            "beta": factors.get("beta", 1),
        })

    results.sort(key=lambda x: x["composite_score"], reverse=True)
    logger.info(f"长期综合排名: {len(results)} 只, "
                f"强力买入={sum(1 for r in results if r['decision']=='STRONG_LONG')}")

    return results


# ═══════════════════════════════════════════
# 5. 长期组合构建
# ═══════════════════════════════════════════

def build_long_term_portfolio(rankings: list[dict],
                                max_positions: int = 20,
                                min_composite: float = 55,
                                capital: float = None) -> dict:
    """
    从排名中构建长期投资组合。
    Greenblatt 建议 20-30 只，每只占 ~3-5%。
    """
    candidates = [r for r in rankings if r["composite_score"] >= min_composite]
    selected = candidates[:max_positions]

    # 等权（长期投资不频繁调仓）
    weight = 1.0 / len(selected) if selected else 0

    portfolio = []
    for r in selected:
        portfolio.append({
            "symbol": r["symbol"],
            "weight": round(weight, 4),
            "composite_score": r["composite_score"],
            "decision": r["decision"],
            "catalyst": r.get("catalyst"),
        })

    logger.info(f"长期组合: {len(portfolio)} 只, 每只 {weight:.1%}")

    return {
        "positions": portfolio,
        "total_positions": len(portfolio),
        "weight_per_position": weight,
        "rebalance_frequency": "monthly",
        "hold_period": "12_months",
        "tax_strategy": "sell_losers_early_winners_late",
    }


# ═══════════════════════════════════════════
# 大师原则（注入 AI）
# ═══════════════════════════════════════════

LONG_TERM_PRINCIPLES = """
LONG-TERM INVESTING PRINCIPLES (Synthesized from the Greats):

## Greenblatt's Magic Formula
- Rank stocks by EBIT/Enterprise Value (cheapness) + Return on Capital (quality)
- Buy top 20-30, hold 1 year, rotate monthly
- Portfolio must be diversified across sectors
- Expect 1 in 4 years of underperformance — patience is the edge

## Klarman's Margin of Safety
- Only buy when price < 70% of conservative intrinsic value
- Cash is a strategic asset — not being invested is better than overpaying
- Look for catalysts: spinoffs, restructurings, short squeezes, asset sales
- Start with what can go wrong, size positions for survival
- Risk is permanent capital loss, NOT price volatility

## Howard Marks' Cycle Awareness
- Markets swing between euphoria and panic — recognize where we are
- Be aggressive when others are fearful, cautious when others are greedy
- The best bargains exist when forced sellers dominate
- When junk companies easily issue debt, it's time for caution

## Fama-French-Carhart Factors
- Small caps (SMB) outperform large caps over 10+ years
- Value (HML) outperforms growth over full cycles
- High profitability (RMW) + conservative investment (CMA) = quality premium
- Momentum (MOM) works but reverses sharply — use with caution

## Universal Truths
- Do the work. Read the financials. No shortcuts.
- Think independently. The best trades look stupid to everyone else.
- Hold for 12+ months. Value needs time to surface.
- A 33% loss needs a 50% gain to recover. Avoid losers above all.
"""
