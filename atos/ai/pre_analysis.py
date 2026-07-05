"""
ATOS PRO v5 — 算法预分析引擎（移植自 AI Hedge Fund 59K Stars）
==============================================================
核心创新：Algorithmic Pre-Analysis + LLM Decision
  不是让 AI 凭空分析，而是先用算法算出精确数字，
  再把结构化结果喂给 LLM 做最终判断。

移植的三大系统：
  1. 巴菲特式深度基本面分析（ROE/护城河/所有者收益/DCF估值）
  2. 波动率+相关性调整的风控仓位限制
  3. 确定性行动约束（防止 LLM 幻觉下单）

参考项目：
  - AI Hedge Fund (virattt/ai-hedge-fund, 59K stars)
  - TradingAgents (TauricResearch/TradingAgents, 71K stars)
"""

import json
import math
import numpy as np
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from atos.core.logging import get_logger

logger = get_logger("ai.pre_analysis")


# ════════════════════════════════════════════════════════════
# Part 1: 深度基本面分析（移植自 AI Hedge Fund warren_buffett.py）
# ════════════════════════════════════════════════════════════

def analyze_fundamentals(metrics: Dict[str, float]) -> Dict[str, Any]:
    """分析公司基本面 — 移植自 AI Hedge Fund analyze_fundamentals()

    检查: ROE、负债率、营业利润率、流动比率
    """
    score = 0
    reasons = []

    # ROE > 15%
    roe = metrics.get("roe", 0)
    if roe > 0.15:
        score += 2
        reasons.append(f"优秀ROE {roe:.1%}")
    elif roe > 0.10:
        score += 1
        reasons.append(f"良好ROE {roe:.1%}")
    elif roe > 0:
        reasons.append(f"ROE偏低 {roe:.1%}")
    else:
        reasons.append("ROE为负")

    # 负债率 < 50%
    debt_to_equity = metrics.get("debt_to_equity", 1.0)
    if debt_to_equity < 0.5:
        score += 2
        reasons.append("低负债率，财务保守")
    elif debt_to_equity < 1.0:
        score += 1
        reasons.append(f"适中负债率 {debt_to_equity:.1f}")
    else:
        reasons.append(f"高负债率 {debt_to_equity:.1f}")

    # 营业利润率 > 15%
    op_margin = metrics.get("operating_margin", 0)
    if op_margin > 0.20:
        score += 2
        reasons.append(f"高营业利润率 {op_margin:.1%}")
    elif op_margin > 0.10:
        score += 1
        reasons.append(f"良好利润率 {op_margin:.1%}")
    elif op_margin > 0:
        reasons.append(f"利润率偏低 {op_margin:.1%}")
    else:
        reasons.append("营业利润为负")

    # 流动比率 > 1.5
    current_ratio = metrics.get("current_ratio", 1.0)
    if current_ratio > 1.5:
        score += 1
        reasons.append("流动性充足")
    elif current_ratio < 0.8:
        reasons.append(f"流动性偏紧 {current_ratio:.1f}")

    return {"score": score, "max_score": 7, "details": "; ".join(reasons)}


def analyze_moat(metrics_history: List[Dict[str, float]]) -> Dict[str, Any]:
    """分析护城河 — 移植自 AI Hedge Fund analyze_moat()

    检查: ROE稳定性、利润率稳定性、资产效率
    """
    if len(metrics_history) < 3:
        return {"score": 0, "max_score": 5, "details": "历史数据不足，无法评估护城河"}

    score = 0
    reasons = []

    # 1. ROE 一致性
    roes = [m.get("roe", 0) for m in metrics_history if m.get("roe") is not None]
    if len(roes) >= 3:
        high_roe_count = sum(1 for r in roes if r > 0.15)
        roe_consistency = high_roe_count / len(roes)
        if roe_consistency >= 0.8:
            score += 2
            reasons.append(f"ROE高度稳定: {high_roe_count}/{len(roes)}期>15%（强护城河）")
        elif roe_consistency >= 0.5:
            score += 1
            reasons.append(f"ROE较稳定: {high_roe_count}/{len(roes)}期>15%")

    # 2. 利润率趋势
    margins = [m.get("operating_margin", 0) for m in metrics_history if m.get("operating_margin") is not None]
    if len(margins) >= 3:
        recent_avg = sum(margins[:2]) / 2
        older_avg = sum(margins[-2:]) / 2
        if recent_avg > older_avg + 0.02:
            score += 1
            reasons.append("利润率持续扩张（定价权强）")
        elif recent_avg < older_avg - 0.02:
            reasons.append("利润率萎缩（定价权弱）")

    # 3. 资产效率
    asset_turnovers = [m.get("asset_turnover", 0) for m in metrics_history if m.get("asset_turnover", 0) > 0]
    if any(t > 1.0 for t in asset_turnovers):
        score += 1
        reasons.append("资产利用效率高")

    # 4. 稳定性综合
    if len(roes) >= 5 and len(margins) >= 5:
        roe_avg = sum(roes) / len(roes)
        roe_var = sum((r - roe_avg)**2 for r in roes) / len(roes)
        roe_stability = 1 - (roe_var**0.5) / max(roe_avg, 0.01)

        margin_avg = sum(margins) / len(margins)
        margin_var = sum((m - margin_avg)**2 for m in margins) / len(margins)
        margin_stability = 1 - (margin_var**0.5) / max(margin_avg, 0.01)

        overall_stability = (roe_stability + margin_stability) / 2
        if overall_stability > 0.7:
            score += 1
            reasons.append(f"业绩稳定性极高 ({overall_stability:.0%})")

    score = min(score, 5)
    return {"score": score, "max_score": 5, "details": "; ".join(reasons)}


def estimate_owner_earnings(
    net_income: float,
    depreciation: float,
    capex: float,
    revenue: float = None,
) -> Dict[str, Any]:
    """估算所有者收益（巴菲特最看重的指标）

    移植自 AI Hedge Fund calculate_owner_earnings()

    公式: 净利润 + 折旧 - 维护性资本支出
    维护性资本支出 ≈ total_capex * 0.85（假设15%为增长性支出）
    """
    if not all([net_income, depreciation, capex]):
        return {"owner_earnings": None, "details": "数据不足"}

    # 维护性资本支出估算（保守估计为总资本支出的 85%）
    maintenance_capex = abs(capex) * 0.85
    owner_earnings = net_income + depreciation - maintenance_capex

    return {
        "owner_earnings": owner_earnings,
        "components": {
            "net_income": net_income,
            "depreciation": depreciation,
            "maintenance_capex": maintenance_capex,
            "total_capex": abs(capex),
        },
        "details": (
            f"净利润{net_income:,.0f} + 折旧{depreciation:,.0f} "
            f"- 维护性资本支出{maintenance_capex:,.0f} = 所有者收益{owner_earnings:,.0f}"
        ),
    }


def calculate_intrinsic_value_dcf(
    owner_earnings: float,
    shares_outstanding: float,
    growth_rate: float = 0.05,
    discount_rate: float = 0.10,
) -> Dict[str, Any]:
    """三阶段 DCF 估值 — 移植自 AI Hedge Fund

    阶段1: 5年较高增长
    阶段2: 5年过渡增长
    终值: Gordon Growth Model
    """
    if owner_earnings <= 0 or shares_outstanding <= 0:
        return {"intrinsic_value": None, "details": "数据无效"}

    stage1_years = 5
    stage2_years = 5
    terminal_growth = 0.025
    stage1_growth = min(growth_rate, 0.08)
    stage2_growth = min(growth_rate * 0.5, 0.04)
    discount = max(discount_rate, 0.08)

    # 阶段1
    stage1_pv = sum(
        owner_earnings * (1 + stage1_growth)**y / (1 + discount)**y
        for y in range(1, stage1_years + 1)
    )

    # 阶段2
    stage1_final = owner_earnings * (1 + stage1_growth)**stage1_years
    stage2_pv = sum(
        stage1_final * (1 + stage2_growth)**y / (1 + discount)**(stage1_years + y)
        for y in range(1, stage2_years + 1)
    )

    # 终值
    final_earnings = stage1_final * (1 + stage2_growth)**stage2_years
    terminal_earnings = final_earnings * (1 + terminal_growth)
    terminal_value = terminal_earnings / (discount - terminal_growth)
    terminal_pv = terminal_value / (1 + discount)**(stage1_years + stage2_years)

    total_value = stage1_pv + stage2_pv + terminal_pv
    conservative_value = total_value * 0.85  # 巴菲特式安全边际：15%折扣

    per_share = conservative_value / shares_outstanding

    return {
        "intrinsic_value": conservative_value,
        "intrinsic_value_per_share": per_share,
        "owner_earnings": owner_earnings,
        "assumptions": {
            "stage1_growth": stage1_growth,
            "stage2_growth": stage2_growth,
            "terminal_growth": terminal_growth,
            "discount_rate": discount,
            "safety_margin": 0.15,
        },
        "details": (
            f"三阶段DCF: 阶段1={stage1_growth:.1%}(5年) → "
            f"阶段2={stage2_growth:.1%}(5年) → "
            f"终值={terminal_growth:.1%} | "
            f"每股内在价值=${per_share:.2f}"
        ),
    }


def pre_analyze_stock(symbol: str, yf_info: Dict = None) -> Dict[str, Any]:
    """对单只股票做完整算法预分析

    这是 AI Hedge Fund 最核心的模式：
    先用算法算出所有客观数字，再把这些结构化结果喂给 LLM

    返回:
        {
            "fundamentals": {...},     # 基本面分项评分
            "moat": {...},             # 护城河评估
            "valuation": {...},        # DCF估值
            "total_score": int,        # 总分
            "max_score": int,          # 满分
            "summary_for_llm": str,    # 给LLM的结构化摘要
        }
    """
    info = yf_info or {}
    result = {"symbol": symbol}

    # 1. 基本面
    metrics = {
        "roe": info.get("returnOnEquity", 0) or 0,
        "debt_to_equity": (info.get("debtToEquity", 0) or 0) / 100 if info.get("debtToEquity") else 1.0,
        "operating_margin": info.get("operatingMargins", 0) or 0,
        "current_ratio": info.get("currentRatio", 0) or 1.0,
    }
    fundamentals = analyze_fundamentals(metrics)
    result["fundamentals"] = fundamentals

    # 2. 护城河（用当前数据做简化版）
    moat = analyze_moat([metrics])
    result["moat"] = moat

    # 3. DCF估值
    net_income = info.get("netIncomeToCommon", 0) or 0
    depreciation = info.get("depreciationAndAmortization", 0) or 0
    capex = info.get("capitalExpenditure", 0) or 0
    shares = info.get("sharesOutstanding", 0) or 0
    revenue_growth = info.get("revenueGrowth", 0) or 0

    oe = estimate_owner_earnings(net_income, depreciation, capex)
    if oe["owner_earnings"] and shares > 0:
        growth = max(0.03, min(revenue_growth, 0.15)) if revenue_growth else 0.05
        valuation = calculate_intrinsic_value_dcf(oe["owner_earnings"], shares, growth_rate=growth)
    else:
        valuation = {"intrinsic_value": None, "intrinsic_value_per_share": None, "details": "数据不足"}

    # 每股价值 vs 当前价格
    current_price = info.get("currentPrice", info.get("regularMarketPrice", 0)) or 0
    iv_per_share = valuation.get("intrinsic_value_per_share")
    if iv_per_share and current_price > 0:
        margin_of_safety = (iv_per_share - current_price) / current_price
        valuation["margin_of_safety"] = round(margin_of_safety, 4)
        valuation["details"] += f" | 安全边际={margin_of_safety:.1%}"
    result["valuation"] = valuation

    # 4. 总分
    total = fundamentals["score"] + moat["score"]
    max_score = fundamentals["max_score"] + moat["max_score"]
    result["total_score"] = total
    result["max_score"] = max_score

    # 5. 给 LLM 的结构化摘要
    result["summary_for_llm"] = json.dumps({
        "symbol": symbol,
        "price": current_price,
        "fundamental_score": f"{total}/{max_score}",
        "roe": f"{metrics['roe']:.1%}",
        "debt_equity": f"{metrics['debt_to_equity']:.2f}",
        "op_margin": f"{metrics['operating_margin']:.1%}",
        "intrinsic_value": f"${iv_per_share:.2f}" if iv_per_share else "N/A",
        "margin_of_safety": f"{valuation.get('margin_of_safety', 0):.1%}" if iv_per_share else "N/A",
        "fundamentals_detail": fundamentals["details"],
        "moat_detail": moat["details"],
        "valuation_detail": valuation.get("details", "N/A"),
    }, ensure_ascii=False)

    return result


# ════════════════════════════════════════════════════════════
# Part 2: 波动率+相关性风控限制（移植自 AI Hedge Fund risk_manager.py）
# ════════════════════════════════════════════════════════════

def calculate_volatility_metrics(prices: List[float], lookback: int = 60) -> Dict[str, float]:
    """计算波动率指标"""
    if len(prices) < 2:
        return {"daily_volatility": 0.05, "annualized_volatility": 0.25, "volatility_percentile": 50.0}

    import numpy as np
    returns = np.diff(prices) / prices[:-1]
    recent = returns[-min(lookback, len(returns)):]

    daily_vol = float(np.std(recent)) if len(recent) > 1 else 0.025
    annualized_vol = daily_vol * np.sqrt(252)

    return {
        "daily_volatility": round(daily_vol, 6),
        "annualized_volatility": round(annualized_vol, 4),
        "data_points": len(recent),
    }


def volatility_adjusted_limit(annual_vol: float) -> float:
    """根据波动率计算仓位上限 — 移植自 AI Hedge Fund

    低波动 <15%: 最多 25%
    中波动 15-30%: 15-20%
    高波动 30-50%: 10-15%
    极高 >50%: 最多 10%
    """
    base_limit = 0.20
    if annual_vol < 0.15:
        multiplier = 1.25  # up to 25%
    elif annual_vol < 0.30:
        multiplier = 1.0 - (annual_vol - 0.15) * 0.5  # 20% → 12.5%
    elif annual_vol < 0.50:
        multiplier = 0.75 - (annual_vol - 0.30) * 0.5  # 15% → 5%
    else:
        multiplier = 0.50  # max 10%
    multiplier = max(0.25, min(1.25, multiplier))
    return round(base_limit * multiplier, 4)


def correlation_multiplier(avg_correlation: float) -> float:
    """根据与现有持仓的平均相关性调整仓位 — 移植自 AI Hedge Fund"""
    if avg_correlation >= 0.80:
        return 0.70
    if avg_correlation >= 0.60:
        return 0.85
    if avg_correlation >= 0.40:
        return 1.00
    if avg_correlation >= 0.20:
        return 1.05
    return 1.10


def compute_position_limit(
    total_equity: float,
    annual_vol: float,
    avg_correlation: float = 0.4,
) -> Dict[str, Any]:
    """综合波动率和相关性计算单只标的仓位上限"""
    vol_limit_pct = volatility_adjusted_limit(annual_vol)
    corr_mult = correlation_multiplier(avg_correlation)
    combined_pct = vol_limit_pct * corr_mult
    position_limit = total_equity * combined_pct

    return {
        "position_limit_pct": round(combined_pct, 4),
        "position_limit_dollar": round(position_limit, 2),
        "volatility_limit_pct": round(vol_limit_pct, 4),
        "correlation_multiplier": round(corr_mult, 4),
        "reasoning": (
            f"波动率{annual_vol:.1%} → 基础上限{vol_limit_pct:.1%} "
            f"× 相关性系数{corr_mult:.2f} → 最终上限{combined_pct:.1%}"
        ),
    }


# ════════════════════════════════════════════════════════════
# Part 3: 确定性行动约束（移植自 AI Hedge Fund portfolio_manager.py）
# ════════════════════════════════════════════════════════════

def compute_allowed_actions(
    ticker: str,
    current_price: float,
    cash: float,
    current_shares: int = 0,
    max_position_pct: float = 0.20,
    total_equity: float = None,
) -> Dict[str, int]:
    """计算允许的交易行动 — 移植自 AI Hedge Fund compute_allowed_actions()

    这是防止 LLM 幻觉下单的关键：
    先算出所有可行的（买入多少股、卖出多少股），
    再让 LLM 从中选择，而不是让 LLM 凭空编造数量。

    返回:
        {"buy": max_shares, "sell": max_shares, "hold": 0}
    """
    if total_equity is None:
        total_equity = cash

    max_dollar = total_equity * max_position_pct
    actions = {"buy": 0, "sell": 0, "hold": 0}

    if current_price <= 0:
        return actions

    # 买入: 现金 ÷ 股价，但不超过仓位上限
    max_buy_cash = int(cash // current_price)
    max_buy_position = int(max_dollar // current_price)
    actions["buy"] = max(0, min(max_buy_cash, max_buy_position))

    # 卖出: 最多卖出所有持仓
    if current_shares > 0:
        actions["sell"] = current_shares

    return actions


def validate_llm_decision(
    decision: Dict[str, Any],
    allowed_actions: Dict[str, int],
) -> Tuple[bool, str]:
    """验证 LLM 的决策是否在允许范围内

    返回 (是否有效, 错误信息)
    """
    action = decision.get("action", "hold")
    quantity = decision.get("quantity", 0)

    if action not in allowed_actions:
        return False, f"不允许的操作: {action}"

    if action == "hold":
        return True, ""

    max_allowed = allowed_actions.get(action, 0)
    if quantity > max_allowed:
        return False, f"数量{quantity}超限（最多{max_allowed}）"

    if quantity < 0:
        return False, "数量不能为负"

    return True, ""


# ════════════════════════════════════════════════════════════
# Part 4: 完整预分析入口
# ════════════════════════════════════════════════════════════

def full_pre_analysis(
    symbol: str,
    yf_info: Dict = None,
    prices: List[float] = None,
    portfolio_context: Dict = None,
) -> Dict[str, Any]:
    """一站式预分析：基本面 + 估值 + 风险限制 + 行动约束

    这是给 ATOS v5 AI 引擎喂数据的标准格式
    """
    result = {}

    # 基本面 + 估值
    fundamental = pre_analyze_stock(symbol, yf_info)
    result["fundamental"] = fundamental

    # 风险限制
    if prices and len(prices) > 1:
        vol = calculate_volatility_metrics(prices[-60:])
    else:
        vol = {"annualized_volatility": 0.25}

    ctx = portfolio_context or {}
    limit = compute_position_limit(
        total_equity=ctx.get("total_equity", 100000),
        annual_vol=vol["annualized_volatility"],
        avg_correlation=ctx.get("avg_correlation", 0.4),
    )
    result["position_limit"] = limit

    # 行动约束
    actions = compute_allowed_actions(
        ticker=symbol,
        current_price=ctx.get("current_price", fundamental.get("valuation", {}).get("margin_of_safety", 0) or 0),
        cash=ctx.get("cash", 0),
        current_shares=ctx.get("current_shares", 0),
        max_position_pct=limit["position_limit_pct"],
        total_equity=ctx.get("total_equity", 0),
    )
    result["allowed_actions"] = actions

    return result
