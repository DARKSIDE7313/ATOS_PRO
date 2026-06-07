"""
ATOS PRO v2 — 长期价值投资系统
===============================
核心理念来自 Michael Burry / Benjamin Graham / Warren Buffett：

  "用 50 美分买 1 美元的东西"

策略原则：
  1. 安全边际 — 只在股价低于内在价值 30%+ 时买入
  2. 深度基本面 — 自由现金流、企业价值、负债率
  3. 逆向思维 — 在不受欢迎的行业里找机会
  4. 集中持仓 — 12-18 只，不做过度分散
  5. 卖出纪律 — 跌到新低就卖，不等"反弹"
  6. 长期持有 — 至少 6-12 个月，用时间兑现价值
"""

import yfinance as yf
from atos.core.logging import get_logger

logger = get_logger("longterm.value")


def calculate_intrinsic_value(symbol: str) -> dict:
    """
    Burry 风格的内在价值估算。

    方法：自由现金流折现 (DCF) + 净资产价值 (Book Value) 综合。
    重点不是精确，而是找到明显便宜的。

    返回:
      {
        "intrinsic_value": 估值,
        "current_price": 现价,
        "margin_of_safety": 安全边际（负值=高估, 正=低估）,
        "burry_score": 0-100（综合吸引力评分）,
        "quality_flags": [],
        "risk_flags": [],
      }
    """
    try:
        stock = yf.Ticker(symbol)
        info = stock.info or {}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}

    price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
    if price <= 0:
        return {"error": "无价格数据", "symbol": symbol}

    # ── 财务数据 ──
    fcf = info.get("freeCashflow", 0)           # 自由现金流
    ebitda = info.get("ebitda", 0)               # 息税折旧摊销前利润
    total_debt = info.get("totalDebt", 0)
    total_cash = info.get("totalCash", 0)
    book_value = info.get("bookValue", 0)
    market_cap = info.get("marketCap", 0)
    ev = info.get("enterpriseValue", 0)
    revenue_growth = info.get("revenueGrowth", 0)
    profit_margins = info.get("profitMargins", 0)
    roe = info.get("returnOnEquity", 0)
    current_ratio = info.get("currentRatio", 0)
    debt_to_equity = info.get("debtToEquity", 0)
    pe = info.get("trailingPE") or info.get("forwardPE", 0)

    # ── 内在价值估算 ──
    # 方法 1: 10x 自由现金流（保守）
    dcf_value = fcf * 10 / (info.get("sharesOutstanding", 1) or 1) if fcf and fcf > 0 else 0

    # 方法 2: 净资产 (Book Value) × 1.2
    bv_value = book_value * 1.2 if book_value and book_value > 0 else 0

    # 方法 3: 8x EBITDA（EV 框架）
    ev_ebitda = ev / ebitda if ebitda and ebitda > 0 else 0
    fair_ev = ebitda * 8 if ebitda and ebitda > 0 else 0
    fair_price_from_ebitda = (fair_ev - total_debt + total_cash) / \
                              (info.get("sharesOutstanding", 1) or 1) if ebitda and ebitda > 0 else 0

    # 综合估值（取可用方法的平均）
    methods = [v for v in [dcf_value, bv_value, fair_price_from_ebitda] if v > 0]
    intrinsic = sum(methods) / len(methods) if methods else price * 0.5  # 无数据时保守估算

    # ── 安全边际 ──
    margin = (intrinsic - price) / price if price > 0 else -1

    # ── 质量检查 ──
    quality = []
    risk = []

    if fcf and fcf > 0:
        quality.append(f"FCF>0 (${fcf/1e9:.1f}B)")
    else:
        risk.append("自由现金流为负")

    if debt_to_equity and debt_to_equity < 50:
        quality.append(f"低负债 (D/E={debt_to_equity:.0f})")
    elif debt_to_equity and debt_to_equity > 150:
        risk.append(f"高负债 (D/E={debt_to_equity:.0f})")

    if profit_margins and profit_margins > 0.10:
        quality.append(f"高利润率 ({profit_margins:.1%})")
    elif profit_margins and profit_margins < 0:
        risk.append(f"亏损 ({profit_margins:.1%})")

    if revenue_growth and revenue_growth > 0.05:
        quality.append(f"营收增长 ({revenue_growth:.1%})")
    elif revenue_growth and revenue_growth < -0.1:
        risk.append(f"营收萎缩 ({revenue_growth:.1%})")

    if current_ratio and current_ratio > 1.5:
        quality.append(f"流动性好 (CR={current_ratio:.1f})")
    elif current_ratio and current_ratio < 0.8:
        risk.append(f"流动性差 (CR={current_ratio:.1f})")

    # ── Burry 综合评分 (0-100) ──
    score = 50  # 基准
    if margin > 0.3:  score += 25   # 大幅低估
    elif margin > 0.1: score += 15
    elif margin < -0.2: score -= 20

    if fcf > 0:       score += 10
    if debt_to_equity and debt_to_equity < 50: score += 10
    if profit_margins and profit_margins > 0.1: score += 10
    if ev_ebitda and ev_ebitda < 10: score += 5  # 估值合理

    if pe and pe < 0: score -= 15        # 亏损
    if margin < -0.5: score -= 20        # 严重高估

    score = max(0, min(100, score))

    # ── 决策 ──
    if score >= 75 and margin > 0.2:
        decision = "STRONG_BUY"
    elif score >= 60 and margin > 0.1:
        decision = "BUY"
    elif score >= 40:
        decision = "WATCH"
    else:
        decision = "AVOID"

    return {
        "symbol": symbol,
        "current_price": round(price, 2),
        "intrinsic_value": round(intrinsic, 2),
        "margin_of_safety": round(margin, 4),
        "burry_score": score,
        "decision": decision,
        "dcf_value": round(dcf_value, 2) if dcf_value else None,
        "bv_value": round(bv_value, 2) if bv_value else None,
        "ev_ebitda": round(ev_ebitda, 1) if ev_ebitda else None,
        "quality_flags": quality,
        "risk_flags": risk,
        "sector": info.get("sector", "Unknown"),
    }


def screen_long_term_candidates(symbols: list[str], min_score: int = 60) -> list[dict]:
    """从标的池中筛选适合长期持有的"""
    candidates = []
    for i, sym in enumerate(symbols):
        result = calculate_intrinsic_value(sym)
        if "error" in result:
            continue
        if result["burry_score"] >= min_score:
            candidates.append(result)
        if (i + 1) % 10 == 0:
            logger.info(f"长期筛选进度: {i+1}/{len(symbols)}")

    candidates.sort(key=lambda x: x["burry_score"], reverse=True)
    logger.info(f"长期投资候选: {len(candidates)} 只 (评分≥{min_score})")
    return candidates


# ── Burry 十大原则 (注入 AI 提示词) ──
BURRY_PRINCIPLES = """
LONG-TERM INVESTING PRINCIPLES (Michael Burry / Benjamin Graham):

1. MARGIN OF SAFETY — Only buy when price < 70% of intrinsic value. If you can't calculate intrinsic value, don't buy.
2. FREE CASH FLOW IS KING — Ignore P/E ratios. Focus on FCF, enterprise value, and EV/EBITDA.
3. CONTRARIAN — The best opportunities are in out-of-favor industries where "road kill" companies trade at irrational discounts.
4. CONCENTRATED PORTFOLIO — Hold 12-18 positions, fully invested. Diversification is protection against ignorance.
5. SELL DISCIPLINE — Sell when price makes a new 52-week low. Don't "wait for it to come back" — that's how you lose everything.
6. DO THE WORK — Read 10-Ks. Understand the business. If you can't explain what the company does in 2 sentences, don't own it.
7. DOWNSIDE PROTECTION — A 33% loss requires a 50% gain to recover. Avoiding losers is more important than picking winners.
8. EMBRACE VOLATILITY — Price swings are opportunity, not risk. Real risk is permanent capital loss from bad analysis.
9. THINK INDEPENDENTLY — Don't copy. Develop your own synthesis. The best trades are the ones everyone else thinks are stupid.
10. PATIENCE — Hold for 6-24 months. Value takes time to be recognized. If you can't hold for 6 months, don't hold for 6 minutes.
"""
