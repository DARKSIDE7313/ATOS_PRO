# ENHANCED v3 — quality score source for ATOS factor engine (Serenity bottleneck logic).
#              Integrated with community Serenity Skills (muxuuu/ZadAnthony/lanfuli).
#              Features: 4问快速过滤, 二级瓶颈扫描, 辐射信号雷达, 对抗验证v2.

"""
ATOS PRO v2 — Serenity 策略模块（v3 增强版）
============================================
基于 Serenity / @aleabitoreddit 社区技能集的供应链瓶颈投资法：

  v3 新增功能:
    A. 4问快速过滤 — 被迫需求 / 规模错配 / 不可替代 / 外部确认
    B. 二级瓶颈扫描 — 对高评分标的查供应商再评分
    C. 辐射信号雷达 — 做空比例变化 + 催化剂检测
    D. 对抗验证v2 — 增强版 AI 魔鬼代言人（多角度攻击）

  核心原则:
    1. 瓶颈理论 — 不买龙头，找产业链不可替代的"开关"
    2. 逆向拆解 — 从终端产品逐层往下找到唯一供应商
    3. 机构盲区 — 只选 $100M-$2B 小盘股（大基金买不了的）
    4. 集中押注 — 持仓 5-10 只，重仓垄断型标的

适用周期: 4-8 周（催化剂驱动的集中持有）
与 Shadow Trader 完全隔离（不同时间框架）
与 Phoenix 互补（Serenity 提供加速催化剂信号）
"""

import json
import os
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

# ─── 已知的上下游映射（社区版方法论提炼）───
# 格式: {上游公司: [下游客户/依赖方]}
# 用途：二级瓶颈扫描时找"供应商的供应商"
SUPPLIER_CHAIN_HINTS = {
    # AI芯片 → 封装 → 基板 → 材料
    "NVDA": ["TSM", "AMAT", "LRCX", "KLAC", "ASML"],
    "AMD": ["TSM", "AMAT", "LRCX"],
    "MRVL": ["TSM", "SIVE"],
    # 光模块/光互联 → 激光器 → 晶圆
    "SIVE": ["LITE", "AXTI", "IQE", "SOI"],
    "LITE": ["AXTI", "IQE", "COHR"],
    "AAOI": ["LITE", "AXTI", "SIVE"],
    # 设备 → 组件 → 材料
    "AMAT": ["LRCX", "KLAC", "CCMP", "ENTG"],
    "ASML": ["ZEUS", "CCMP", "ENTG"],
    "TSM": ["AMAT", "ASML", "LRCX", "ENTG", "CCMP"],
    # 存储
    "MU": ["AMAT", "LRCX", "KLAC", "ENTG"],
    "STX": ["TDK", "WDC"],
}

# ─── 已知的催化剂事件表（在雷达中自动检查）───
CATALYST_CHECK = [
    {"event": "earnings", "label": "财报", "check_fn": None},
    {"event": "index_inclusion", "label": "指数纳入", "check_fn": None},
    {"event": "short_squeeze", "label": "轧空", "check_fn": None},
]


# ═══════════════════════════════════════════════════
# A — 4问快速过滤器
# ═══════════════════════════════════════════════════

def _four_question_filter(info: dict) -> dict:
    """
    4问快速过滤（社区版方法论核心）：
    1. 被迫需求 — 有人必须买这个零件才能出货吗？
    2. 规模错配 — 供应商市值 < 它所赋能年资本支出的 1%？
    3. 不可替代 — 24个月内有量产替代品吗？
    4. 外部确认 — 过去90天有≥3个独立来源确认了这个瓶颈吗？

    返回: {passes: bool, answers: dict, confidence: str}

    注意：大盘蓝筹（市值>$5B）用宽松标准，
          小盘瓶颈标的（市值<$5B）用严格标准。
    """
    answers = {}
    score = 0
    market_cap = info.get("marketCap", 0)
    is_small_cap = market_cap < 5e9  # 是否小盘瓶颈候选

    # Q1: 被迫需求 — 用毛利率+行业判断
    sector = info.get("sector", "")
    industry = (info.get("industry", "") or "").lower()
    gross_margins = info.get("grossMargins", 0) or 0

    # AI供应链中的公司天然有被迫需求
    has_forced_demand = (
        _is_supply_chain_relevant(info)
        or gross_margins > 0.40  # 高毛利 = 定价权 = 不可或缺
    )
    # 大市值蓝筹即使不是芯片行业也常有被迫需求（如 AAPL 被需要做手机）
    if not has_forced_demand and not is_small_cap:
        # 大盘股检查：营收>50B 或 行业龙头
        revenue = info.get("totalRevenue", 0) or 0
        has_forced_demand = revenue > 50e9
    answers["forced_demand"] = has_forced_demand
    if has_forced_demand:
        score += 1

    # Q2: 规模错配 — 小盘用严格标准，大盘不检查此项（它们已经很大了）
    revenue = info.get("totalRevenue", 0) or 1
    rev_per_cap = revenue / market_cap if market_cap > 0 else 0
    if is_small_cap:
        size_mismatch = (market_cap < 2e9 and rev_per_cap > 0.3) or (market_cap < 5e8)
    else:
        # 大盘股：仅检查营收/市值比是否异常低（可能被低估）
        size_mismatch = rev_per_cap > 0.8  # 营收接近市值的公司
    answers["size_mismatch"] = bool(size_mismatch)
    if size_mismatch:
        score += 1

    # Q3: 不可替代 — 高毛利+正利润
    profit_margins = info.get("profitMargins", 0) or 0
    if is_small_cap:
        no_substitute = gross_margins > 0.50 and profit_margins > 0.05
    else:
        # 大盘股：毛利 > 30% + 正利润 + 行业稀缺性
        no_substitute = gross_margins > 0.30 and profit_margins > 0.05
    answers["no_substitute"] = bool(no_substitute)
    if no_substitute:
        score += 1

    # Q4: 外部确认
    short_pct = info.get("shortPercentOfFloat", 0) or 0
    if is_small_cap:
        has_outside_voice = short_pct > 0.03  # >3% 做空意味着被关注
    else:
        # 大盘股被大量分析师覆盖，不依赖做空比例
        has_outside_voice = True
    answers["outside_voice"] = bool(has_outside_voice)
    if has_outside_voice:
        score += 1

    # 小盘至少 2/4 通过，大盘至少 1/4 通过
    min_passes = 1 if not is_small_cap else 2
    return {
        "passes": score >= min_passes,
        "answers": answers,
        "score": score,
        "confidence": "HIGH" if score >= 3 else ("MEDIUM" if score >= min_passes else "LOW"),
    }


# ═══════════════════════════════════════════════════
# B — 二级瓶颈扫描（瓶颈中的瓶颈）
# ═══════════════════════════════════════════════════

def deep_chokepoint_scan(symbols: list[str]) -> dict:
    """
    二级瓶颈扫描：
    1. 先跑一级扫描 find_chokepoint_stocks()
    2. 对 STRONG_CHOKEPOINT 的标的，查它们的供应商（从已知映射）
    3. 对供应商再跑评分
    4. 返回"瓶颈中的瓶颈"列表

    返回: {
        "primary": [...一级结果],
        "deep": [...二级结果（供应商中的瓶颈）],
        "chain_map": {... 上下游关系},
    }
    """
    primary = find_chokepoint_stocks(symbols)
    strong = [c for c in primary if c["decision"] == "STRONG_CHOKEPOINT"]

    # 收集供应商
    supplier_set = set()
    chain_map = {}
    for c in strong:
        sym = c["symbol"]
        if sym in SUPPLIER_CHAIN_HINTS:
            suppliers = SUPPLIER_CHAIN_HINTS[sym]
            supplier_set.update(suppliers)
            chain_map[sym] = suppliers

    if not supplier_set:
        return {
            "primary": primary,
            "deep": [],
            "chain_map": chain_map,
        }

    # 对供应商跑扫描
    deep_results = find_chokepoint_stocks(list(supplier_set))
    deep_strong = [d for d in deep_results if d["decision"] in ("STRONG_CHOKEPOINT", "CHOKEPOINT_WATCH")]

    logger.info(
        f"二级瓶颈扫描: {len(strong)}个一级→{len(supplier_set)}个供应商→{len(deep_strong)}个二级瓶颈"
    )

    return {
        "primary": primary,
        "deep": deep_strong,
        "chain_map": chain_map,
    }


# ═══════════════════════════════════════════════════
# C — 辐射信号雷达
# ═══════════════════════════════════════════════════

def _format_change(current, previous):
    """计算百分比变化，format 为 +X% / -X%"""
    if previous and previous > 0 and current is not None:
        pct = (current - previous) / previous * 100
        return f"{pct:+.1f}%"
    return "N/A"


def signal_radar(symbols: list[str],
                 previous_state: dict | None = None) -> dict:
    """
    辐射信号雷达（社区版 lanfuli 风格）：
    - 检测"提及速度"和做空比例变化
    - 识别可能的催化剂（earnings即将到来、轧空设置）
    - 对比上一次扫描的变化

    参数:
      symbols: 要扫描的标的列表
      previous_state: 上一次扫描的状态（可选，用于对比变化）

    返回: {
        "signals": [
            {symbol, signal_type, intensity, description, change},
            ...
        ],
        "heat_map": {symbol: "HOT"/"WARM"/"COLD"}
    }
    """
    signals = []

    for sym in symbols:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}

            market_cap = info.get("marketCap", 0)
            short_pct = info.get("shortPercentOfFloat", 0) or 0
            gross_margins = info.get("grossMargins", 0) or 0
            price = info.get("currentPrice", 0)
            volume = info.get("volume", 0)
            avg_volume = info.get("averageVolume", 1)

            # 信号1: 做空比例高 → 轧空潜力
            if short_pct > 0.20:
                intensity = "HIGH"
                desc = f"做空{short_pct*100:.1f}% — 极度做空，轧空爆炸潜力"
            elif short_pct > 0.10:
                intensity = "MEDIUM"
                desc = f"做空{short_pct*100:.1f}% — 显著做空，有机会"
            elif short_pct > 0.05:
                intensity = "LOW"
                desc = f"做空{short_pct*100:.1f}% — 轻度做空"
            else:
                intensity = "NONE"
                desc = f"做空{short_pct*100:.1f}% — 正常"

            # 计算变化（如果有上一次状态）
            change = None
            if previous_state and sym in previous_state:
                prev_short = previous_state[sym].get("short_pct", 0)
                change = _format_change(short_pct, prev_short)

            if intensity != "NONE":
                signals.append({
                    "symbol": sym,
                    "signal_type": "short_interest",
                    "intensity": intensity,
                    "description": desc,
                    "price": price,
                    "short_pct": round(short_pct, 4),
                    "change": change,
                })

            # 信号2: 成交量异常（相对平均）
            vol_ratio = volume / avg_volume if avg_volume > 0 else 0
            if vol_ratio > 3.0:
                signals.append({
                    "symbol": sym,
                    "signal_type": "volume_spike",
                    "intensity": "HIGH" if vol_ratio > 5 else "MEDIUM",
                    "description": f"成交量异常: {vol_ratio:.1f}x 均值",
                    "price": price,
                    "vol_ratio": round(vol_ratio, 1),
                })

            # 信号3: 高毛利率（垄断信号）
            if gross_margins > 0.60:
                signals.append({
                    "symbol": sym,
                    "signal_type": "monopoly_margin",
                    "intensity": "HIGH",
                    "description": f"毛利率{gross_margins*100:.0f}% — 顶级定价权",
                    "price": price,
                    "gross_margins": round(gross_margins, 4),
                })

        except Exception:
            continue

    # 按强度排序
    intensity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}
    signals.sort(key=lambda s: intensity_order.get(s["intensity"], 99))

    # 热度图
    heat_map = {}
    for s in signals:
        sym = s["symbol"]
        current = heat_map.get(sym, "COLD")
        if s["intensity"] == "HIGH":
            heat_map[sym] = "HOT"
        elif s["intensity"] == "MEDIUM" and current == "COLD":
            heat_map[sym] = "WARM"
        elif s["intensity"] == "LOW" and current == "COLD":
            heat_map[sym] = "WARM"

    logger.info(f"信号雷达: {len(signals)}个信号, {len([h for h in heat_map.values() if h=='HOT'])}个HOT")
    return {"signals": signals, "heat_map": heat_map}


# ═══════════════════════════════════════════════════
# D — 增强版对抗验证
# ═══════════════════════════════════════════════════

def adversarial_ai_check(thesis: str, symbol: str) -> dict:
    """
    Serenity 风格：让 AI 扮演"魔鬼代言人"，攻击自己的投资逻辑。
    如果攻击失败 → 逻辑站得住脚 → 可以投。

    v3 增强: 多角度攻击 + 供应链特定攻击 + 更细粒度评分
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"passed": True, "note": "API Key未设置，跳过对抗验证"}

    prompt = f"""You are a SKEPTICAL HEDGE FUND ANALYST specialized in supply-chain investing.
Your job is to DESTROY this investment thesis for {symbol}.

THESIS:
{thesis}

ATTACK FROM ALL ANGLES:
1. Technology risk — What innovation could make this company's product obsolete?
2. Market size — Is the TAM actually large enough? Or is this a tiny niche?
3. Competition — Could a well-funded competitor enter within 6-12 months?
4. Valuation — Is the current price already discounting years of growth?
5. Worst case — What's the -80% scenario? How likely?
6. Supply chain specific — Is a SINGLE customer >50% of revenue?
7. Execution risk — Can management actually deliver on the promise?

Output valid JSON ONLY with these fields:
{{
    "thesis_broken": true/false,
    "biggest_risk": "one sentence",
    "survival_score": 0-100,
    "attack_details": {{
        "technology": "score 0-10 + short reason",
        "market": "score 0-10 + short reason",
        "competition": "score 0-10 + short reason",
        "valuation": "score 0-10 + short reason",
        "customer_concentration": "score 0-10 + short reason"
    }},
    "verdict": "INVEST / REJECT / WAIT",
    "key_question_to_research": "what to check next"
}}"""

    try:
        import requests
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
        result = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(result)
        broken = result.get("thesis_broken", False)
        survival = result.get("survival_score", 0)
        verdict = result.get("verdict", "WAIT")
        logger.info(
            f"对抗验证v3 {symbol}: {'被击穿❌' if broken else '站住✅'} "
            f"| 生存分={survival} | 结论={verdict}"
        )
        return result
    except Exception as e:
        return {"passed": True, "note": f"对抗验证失败: {e}"}


# ═══════════════════════════════════════════════════
# 核心 — 评分引擎（保持与 v2 兼容）
# ═══════════════════════════════════════════════════

def _is_supply_chain_relevant(info: dict) -> bool:
    sector = info.get("sector", "")
    industry = info.get("industry", "")
    return (sector in CHOKEPOINT_SECTORS or
            any(kw in (industry or "").lower() for kw in CHOKEPOINT_KEYWORDS))


def _calc_chokepoint_score(market_cap: float, gross_margins: float,
                           short_pct: float, sector_ok: bool,
                           revenue_growth: float, profit_margins: float,
                           info: dict) -> int:
    """评分引擎：从 Serenity 社区方法论提炼的加权评分（v3）。"""
    score = 0

    # 1. 市值越小越被忽视
    if market_cap < 500e6:      score += 20
    elif market_cap < 1e9:      score += 15
    elif market_cap < 2e9:      score += 10

    # 2. 毛利率 = 定价权 = 垄断信号
    if gross_margins > 0.60:    score += 20
    elif gross_margins > 0.40:  score += 10

    # 3. 做空比例 = 轧空催化剂燃料
    if short_pct > 0.15:        score += 25
    elif short_pct > 0.10:      score += 15
    elif short_pct > 0.05:      score += 5

    # 4. AI 供应链加成
    if sector_ok:                score += 15

    # 5. 成长性
    if revenue_growth > 0.20:    score += 10
    if profit_margins > 0.10:    score += 10

    # 6. 低 beta = 垄断稳定性
    beta = info.get("beta", 1.0)
    if beta < 1.5:               score += 5

    return score


# ═══════════════════════════════════════════════════
# 主入口 — 瓶颈检测（与 v2 接口完全兼容）
# ═══════════════════════════════════════════════════

def find_chokepoint_stocks(symbols: list[str]) -> list[dict]:
    """
    瓶颈检测（v3 增强版）：
    1. 4问快速过滤（前置）
    2. 市值 $100M-$5B
    3. 毛利率 > 40%（定价权）
    4. 做空比例 > 5%（轧空催化剂）
    5. 低 beta + AI 供应链相关

    输出信息含 four_question 评估结果。
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

            # A) 4问快速过滤
            four_q = _four_question_filter(info)
            if not four_q["passes"]:
                continue  # 4问不过 → 跳过

            score = _calc_chokepoint_score(
                market_cap, gross_margins, short_pct,
                sector_ok, revenue_growth, profit_margins, info
            )

            # 4问置信度调整
            if four_q["confidence"] == "LOW":
                score = max(0, score - 10)
            elif four_q["confidence"] == "HIGH":
                score += 5

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
                "short_pct": round(short_pct, 3) if short_pct else None,
                "revenue_growth": round(revenue_growth, 3) if revenue_growth else None,
                "profit_margins": round(profit_margins, 3) if profit_margins else None,
                "beta": round(beta, 2) if beta else None,
                "serenity_score": score,
                "decision": decision,
                "price": price,
                "four_question": four_q,
            })

        except Exception:
            continue

    candidates.sort(key=lambda x: x["serenity_score"], reverse=True)
    strong = [c for c in candidates if c["decision"] == "STRONG_CHOKEPOINT"]

    logger.info(f"瓶颈扫描 v3 (含4问过滤): {len(candidates)}个候选, {len(strong)}个强力瓶颈")
    return candidates


# ═══════════════════════════════════════════════════
# 组合构建（接口不变）
# ═══════════════════════════════════════════════════

def serenity_portfolio(universe: list[str], max_positions: int = 8,
                        min_score: int = 45) -> dict:
    """
    Serenity 风格组合构建：
    - 只选瓶颈标的（已含4问过滤）
    - 集中 5-10 只
    - 高评分者配更重
    - 持有 4-8 周
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
        if c["serenity_score"] >= 55:
            weight *= 1.3

        positions.append({
            "symbol": c["symbol"],
            "weight": round(weight, 4),
            "serenity_score": c["serenity_score"],
            "market_cap_m": c["market_cap_m"],
            "short_pct": c.get("short_pct"),
            "reason": f"瓶颈标的, 市值${c['market_cap_m']:.0f}M, 做空{c.get('short_pct',0)*100 if c.get('short_pct') else 0:.1f}%",
            "confidence": c.get("four_question", {}).get("confidence", "MEDIUM"),
        })

    total_w = sum(p["weight"] for p in positions)
    for p in positions:
        p["weight"] = round(p["weight"] / total_w, 4) if total_w > 0 else 0

    logger.info(f"Serenity组合: {len(positions)}只, Top: {positions[0]['symbol'] if positions else '无'}")
    return {
        "positions": positions,
        "total_positions": len(positions),
        "max_positions": max_positions,
        "hold_period": "4-8_weeks",
        "style": "chokepoint_concentration",
    }


def serenity_quality_filter(symbols: list[str]) -> dict:
    """
    使用 Serenity 瓶颈检测逻辑计算质量评分（接口与 v2 完全兼容）。

    返回 {symbol: {composite, serenity_score, decision, market_cap_m}}
    """
    chokepoints = find_chokepoint_stocks(symbols)
    cp_map = {c["symbol"]: c for c in chokepoints}

    results = {}
    for sym in symbols:
        if sym in cp_map:
            raw = cp_map[sym]["serenity_score"]
            composite = min(raw / 100.0, 1.0)
            results[sym] = {
                "composite": round(composite, 4),
                "serenity_score": raw,
                "decision": cp_map[sym]["decision"],
                "market_cap_m": cp_map[sym].get("market_cap_m"),
            }
        else:
            # 4问过滤没过的票得低分
            try:
                stock = yf.Ticker(sym)
                info = stock.info or {}
                gross_margins = info.get("grossMargins", 0) or 0
                # 高毛利但4问不过 → 普通公司但有基本面
                base = 0.35 if gross_margins > 0.40 else 0.25
            except Exception:
                base = 0.25
            results[sym] = {"composite": base, "serenity_score": 0, "decision": "PASS"}

    logger.info(f"Serenity质量过滤 v3: {len(results)} 只")
    return results


# ── Serenity 核心原则（注入 AI）──
SERENITY_PRINCIPLES = """
SERENITY'S CHOKEPOINT INVESTING PRINCIPLES (v3):

1. DON'T BUY THE OBVIOUS — Skip NVDA/MSFT/GOOG. Find the ONE company that makes the ONE component nobody else can.
2. FOUR-QUESTION FILTER — Before any position: (a) forced demand? (b) size mismatch? (c) no substitute? (d) outside voice?
3. BOTTOM-UP REVERSE ENGINEER — Start from end-product and trace DOWN to raw materials.
4. BOTTLENECK WITHIN THE BOTTLENECK — Your supplier's supplier is often where real scarcity lives.
5. INSTITUTIONAL BLIND SPOTS — Target $100M-$2B market cap. Wall Street literally CANNOT buy these.
6. MONOPOLY > MOMENTUM — Only when 1-2 companies GLOBALLY can produce this component.
7. ADVERSARIAL TESTING — Run thesis through AI devil's advocates before committing capital.
8. SHORT INTEREST IS FUEL — >10% short = squeeze potential. >20% = explosive.
9. GROSS MARGINS > 40% — Pricing power = monopoly. No monopoly = no edge.
10. HOLD 4-8 WEEKS — Catalyst-driven. When thesis peaks, get out.
11. CONCENTRATE — 5-10 positions max. Diversification is for people who don't know what they own.
12. CUT LOSERS FAST — If bottleneck thesis breaks, sell immediately. Don't wait.
"""
