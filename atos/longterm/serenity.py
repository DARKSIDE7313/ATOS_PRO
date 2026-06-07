# ENHANCED v2 — quality score source for ATOS factor engine (Serenity bottleneck logic).
#              Integrated with community Serenity Skills (muxuuu/ZadAnthony/lanfuli).

"""
ATOS PRO v2 — Serenity 策略模块（增强版）
=========================================
基于 Serenity / @aleabitoreddit 社区技能集的供应链瓶颈投资法：

  1. 瓶颈理论 — 不买龙头，找产业链不可替代的"开关"
  2. 四问快速过滤 — 被迫需求 / 规模错配 / 不可替代 / 外部确认
  3. 逆向拆解 — 从终端产品逐层往下找到唯一供应商
  4. 机构盲区 — 只选 $100M-$2B 小盘股（大基金买不了的）
  5. AI 对抗验证 — 让 AI 攻击自己的投资逻辑
  6. 集中押注 — 持仓 5-10 只，重仓垄断型标的
  7. 瓶颈中的瓶颈 — 沿供应链往上追一级供应商

适用周期: 4-8 周（催化剂驱动的集中持有）
与 Shadow Trader 完全隔离（不同时间框架）
与 Phoenix 互补（Serenity 提供加速催化剂信号）
"""

import yfinance as yf
from atos.core.logging import get_logger

logger = get_logger("longterm.serenity")

# ─── 供应链行业分类 ───
CHOKEPOINT_SECTORS = [
    "Semiconductors", "Semiconductor Equipment", "Electronic Components",
    "Specialty Chemicals", "Industrial Materials", "Communication Equipment",
    "Hardware", "Technology", "Materials",
]

CHOKEPOINT_KEYWORDS = [
    "semi", "optic", "photon", "wafer", "substrate",
    "laser", "material", "component", "chip",
]


def _is_supply_chain_relevant(info: dict) -> bool:
    sector = info.get("sector", "")
    industry = info.get("industry", "")
    return (sector in CHOKEPOINT_SECTORS or
            any(kw in (industry or "").lower() for kw in CHOKEPOINT_KEYWORDS))


def _calc_chokepoint_score(market_cap: float, gross_margins: float,
                           short_pct: float, sector_ok: bool,
                           revenue_growth: float, profit_margins: float,
                           info: dict) -> int:
    """评分引擎：从 Serenity 社区方法论提炼的加权评分。"""
    score = 0

    # 1. 市值越小越被忽视
    if market_cap < 500e6:      score += 20
    elif market_cap < 1e9:      score += 15
    elif market_cap < 2e9:      score += 10

    # 2. 毛利率 = 定价权 = 垄断信号
    if gross_margins > 0.60:    score += 20
    elif gross_margins > 0.40:  score += 10

    # 3. 做空比例 = 轧空催化剂燃料
    if short_pct > 15:          score += 25
    elif short_pct > 10:        score += 15
    elif short_pct > 5:         score += 5

    # 4. AI 供应链加成
    if sector_ok:                score += 15

    # 5. 成长性
    if revenue_growth > 0.20:    score += 10
    if profit_margins > 0.10:    score += 10

    # 6. 增强评分（社区版方法论补充）
    beta = info.get("beta", 1.0)
    if beta < 1.5:               score += 5   # 不太波动的垄断更有价值

    return score


def find_chokepoint_stocks(symbols: list[str]) -> list[dict]:
    """
    瓶颈检测（增强版 v2）：
    1. 市值 $100M-$5B（机构买不了的"小"公司）
    2. 毛利率 > 40%（有定价权 = 可能是垄断）
    3. 做空比例 > 5%（有轧空催化剂）
    4. 行业是半导体/材料/光通信/光子学（AI 供应链）
    5. 低 beta — 垄断型不太波动
    """
    candidates = []
    for sym in symbols:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}

            market_cap = info.get("marketCap", 0)
            sector = info.get("sector", "")
            industry = info.get("industry", "")
            gross_margins = info.get("grossMargins", 0) or 0
            short_pct = info.get("shortPercentOfFloat", 0) or 0
            revenue_growth = info.get("revenueGrowth", 0) or 0
            profit_margins = info.get("profitMargins", 0) or 0
            price = info.get("currentPrice", 0)
            beta = info.get("beta", 1.0)

            # 市值过滤
            if market_cap < 100e6 or market_cap > 5e9:
                continue

            sector_ok = _is_supply_chain_relevant(info)

            score = _calc_chokepoint_score(
                market_cap, gross_margins, short_pct,
                sector_ok, revenue_growth, profit_margins, info
            )

            # 决策阈值
            if score >= 55:
                decision = "STRONG_CHOKEPOINT"
            elif score >= 35:
                decision = "CHOKEPOINT_WATCH"
            else:
                decision = "PASS"

            candidates.append({
                "symbol": sym,
                "market_cap_m": round(market_cap / 1e6, 0),
                "sector": sector,
                "industry": industry,
                "gross_margins": round(gross_margins, 3) if gross_margins else None,
                "short_pct": round(short_pct, 1) if short_pct else None,
                "revenue_growth": round(revenue_growth, 3) if revenue_growth else None,
                "profit_margins": round(profit_margins, 3) if profit_margins else None,
                "beta": round(beta, 2) if beta else None,
                "serenity_score": score,
                "decision": decision,
                "price": price,
            })

        except Exception:
            continue

    candidates.sort(key=lambda x: x["serenity_score"], reverse=True)
    strong = [c for c in candidates if c["decision"] == "STRONG_CHOKEPOINT"]

    logger.info(f"瓶颈扫描 v2: {len(candidates)}个候选, {len(strong)}个强力瓶颈")
    return candidates


def adversarial_ai_check(thesis: str, symbol: str) -> dict:
    """
    Serenity 风格：让 AI 扮演"魔鬼代言人"，攻击自己的投资逻辑。
    如果攻击失败 → 逻辑站得住脚 → 可以投。
    """
    import requests, json, os

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"passed": True, "note": "API Key未设置，跳过对抗验证"}

    prompt = f"""You are a SKEPTICAL HEDGE FUND ANALYST. Your job is to DESTROY this investment thesis for {symbol}.

THESIS:
{thesis}

ATTACK FROM THESE ANGLES:
1. What technology could make this company's product obsolete?
2. Is the market actually big enough for this to matter?
3. Could a competitor enter this niche in <6 months?
4. Is the valuation already pricing in the growth?
5. What's the worst-case scenario — could you lose 80%+?

Output JSON: {{"thesis_broken": true/false, "biggest_risk": "...", "survival_score": 0-100, "verdict": "INVEST|REJECT|WAIT"}}"""

    try:
        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        result = json.loads(resp.json()["choices"][0]["message"]["content"])
        logger.info(f"对抗验证 {symbol}: {'通过' if not result.get('thesis_broken') else '被击穿'} | 生存分={result.get('survival_score',0)}")
        return result
    except Exception as e:
        return {"passed": True, "note": f"对抗验证失败: {e}"}


def serenity_portfolio(universe: list[str], max_positions: int = 8,
                        min_score: int = 45) -> dict:
    """
    Serenity 风格组合构建：
    - 只选瓶颈标的
    - 集中 5-10 只
    - 高评分者配更重
    - 持有 1-2 个月，不是长期
    """
    chokepoints = find_chokepoint_stocks(universe)
    selected = [c for c in chokepoints if c["serenity_score"] >= min_score][:max_positions]

    if not selected:
        logger.info("无合格瓶颈标的，等待更好的机会")
        return {"positions": [], "cash_weight": 1.0}

    total_score = sum(c["serenity_score"] for c in selected)
    positions = []

    for c in selected:
        weight = c["serenity_score"] / total_score if total_score > 0 else 1.0 / len(selected)
        # 重仓垄断级
        if c["serenity_score"] >= 55:
            weight *= 1.3

        positions.append({
            "symbol": c["symbol"],
            "weight": round(weight, 4),
            "serenity_score": c["serenity_score"],
            "market_cap_m": c["market_cap_m"],
            "short_pct": c.get("short_pct"),
            "reason": f"瓶颈标的, 市值${c['market_cap_m']:.0f}M, 做空{c.get('short_pct',0):.1f}%",
        })

    # 重新归一化
    total_w = sum(p["weight"] for p in positions)
    for p in positions:
        p["weight"] = round(p["weight"] / total_w, 4) if total_w > 0 else 0

    logger.info(f"Serenity组合: {len(positions)}只, Top: {positions[0]['symbol'] if positions else '无'}")

    return {
        "positions": positions,
        "total_positions": len(positions),
        "max_positions": max_positions,
        "hold_period": "1-2_months",
        "style": "chokepoint_concentration",
    }


def serenity_quality_filter(symbols: list[str]) -> dict:
    """
    使用 Serenity 瓶颈检测逻辑计算质量评分。

    对每只标的运行 chokepoint 分析，将原始 serenity_score (0-100)
    归一化为 0-1 composite 分数供 quality factor 使用。

    返回 {symbol: {composite, serenity_score, decision}}
    """
    chokepoints = find_chokepoint_stocks(symbols)
    cp_map = {c["symbol"]: c for c in chokepoints}

    results = {}
    for sym in symbols:
        if sym in cp_map:
            raw = cp_map[sym]["serenity_score"]
            # Normalize: max theoretical score ~100, cap at 1.0
            composite = min(raw / 100.0, 1.0)
            results[sym] = {
                "composite": round(composite, 4),
                "serenity_score": raw,
                "decision": cp_map[sym]["decision"],
                "market_cap_m": cp_map[sym].get("market_cap_m"),
            }
        else:
            # Symbols that fail chokepoint filter get a low base score
            results[sym] = {"composite": 0.3, "serenity_score": 0, "decision": "PASS"}

    logger.info(f"Serenity质量过滤: {len(results)} 只")
    return results


# ── Serenity 核心原则（注入 AI）──
SERENITY_PRINCIPLES = """
SERENITY'S CHOKEPOINT INVESTING PRINCIPLES:

1. DON'T BUY THE OBVIOUS — Skip NVDA/MSFT/GOOG. Find the ONE company that makes the ONE component nobody else can.
2. BOTTOM-UP REVERSE ENGINEER — Start from the final product (GPU, data center) and trace DOWN to raw materials.
3. INSTITUTIONAL BLIND SPOTS — Target $100M-$2B market cap. Wall Street literally CANNOT buy these — your edge.
4. MONOPOLY > MOMENTUM — Only invest when 1-2 companies GLOBALLY can produce this component.
5. ADVERSARIAL TESTING — Run your thesis through AI "devil's advocates" before committing capital.
6. SHORT INTEREST IS FUEL — >10% short float = potential squeeze. >20% = explosive upside.
7. GROSS MARGINS > 40% — Pricing power means monopoly. No monopoly = no edge.
8. HOLD 4-8 WEEKS — Catalyst-driven, not buy-and-hold. When the story peaks, get out.
9. CONCENTRATE — 5-10 positions max. Diversification is for people who don't know what they own.
10. CUT LOSERS FAST — If the bottleneck thesis breaks, sell immediately. Don't wait.
"""
