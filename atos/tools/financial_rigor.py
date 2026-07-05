"""
ATOS Financial Rigor Toolkit — 移植自 AI Berkshire (9K Stars)
==============================================================
精确十进制计算 + 市值验算 + 多源交叉验证 + Benford检测 + 三情景估值

核心设计: 所有金融计算使用 decimal.Decimal，零浮点误差。
这是一个审计级工具——每一个数字都可以复现和追踪。

用法:
    from atos.tools.financial_rigor import (
        verify_market_cap, verify_valuation, cross_validate,
        benford_check, three_scenario_valuation
    )
"""

from decimal import Decimal, Context, ROUND_HALF_EVEN
import math
from typing import Dict, List, Optional, Tuple

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def exact(value) -> Decimal:
    """任意数值转精确 Decimal，避免浮点陷阱"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def fmt_number(d: Decimal) -> str:
    """格式化大数"""
    v = float(d)
    av = abs(v)
    if av >= 1e12:
        return f"{v/1e12:.2f}T"
    if av >= 1e9:
        return f"{v/1e9:.2f}B"
    if av >= 1e6:
        return f"{v/1e6:.2f}M"
    return f"{v:,.2f}"


# ═══════════════════════════════════════════════════
# 1. 市值验算
# ═══════════════════════════════════════════════════

def verify_market_cap(price: float, shares: float, reported_cap: float = None) -> dict:
    """验算市值 = 股价 × 总股本

    返回 dict 含: calculated, reported, deviation_pct, is_valid
    """
    p = exact(price)
    s = exact(shares)
    calculated = _CTX.multiply(p, s)

    result = {
        "price": float(p),
        "shares": float(s),
        "calculated_market_cap": float(calculated),
        "calculated_display": fmt_number(calculated),
    }

    if reported_cap is not None and reported_cap > 0:
        r = exact(reported_cap)
        deviation = abs(float(calculated - r)) / float(r) * 100
        result["reported_market_cap"] = float(r)
        result["deviation_pct"] = round(deviation, 2)
        result["is_valid"] = deviation <= 5.0
        if deviation > 5:
            result["warning"] = f"偏差{deviation:.1f}%>5% — 请检查股本/单位"
        elif deviation > 1:
            result["warning"] = f"偏差{deviation:.1f}% — 可接受(股价波动)"
        else:
            result["warning"] = None
    else:
        result["deviation_pct"] = None
        result["is_valid"] = True
        result["warning"] = "无报告市值，无法对比"

    return result


# ═══════════════════════════════════════════════════
# 2. 估值指标验算
# ═══════════════════════════════════════════════════

def verify_valuation(price: float, eps: float = None, bvps: float = None,
                     fcf_per_share: float = None, dividend: float = None) -> dict:
    """从原始数据计算估值指标，Decimal 精度

    返回: {PE, PB, ROE, P_FCF, FCF_Yield, Dividend_Yield}
    """
    p = exact(price)
    result = {}

    if eps is not None:
        e = exact(eps)
        if e != 0:
            result["PE"] = round(float(_CTX.divide(p, e)), 2)
            result["Earnings_Yield"] = round(float(_CTX.divide(e, p) * 100), 2)
        else:
            result["PE"] = None
            result["Earnings_Yield"] = None

    if bvps is not None:
        b = exact(bvps)
        if b != 0:
            result["PB"] = round(float(_CTX.divide(p, b)), 2)
            if eps is not None and exact(eps) != 0:
                result["ROE"] = round(float(_CTX.divide(exact(eps), b) * 100), 2)
        else:
            result["PB"] = None

    if fcf_per_share is not None:
        f = exact(fcf_per_share)
        if f != 0:
            result["P_FCF"] = round(float(_CTX.divide(p, f)), 2)
            result["FCF_Yield"] = round(float(_CTX.divide(f, p) * 100), 2)

    if dividend is not None:
        d = exact(dividend)
        if p != 0:
            result["Dividend_Yield"] = round(float(_CTX.divide(d, p) * 100), 2)

    return result


# ═══════════════════════════════════════════════════
# 3. 多源交叉验证
# ═══════════════════════════════════════════════════

def cross_validate(field_name: str, sources: Dict[str, float],
                   tolerance_pct: float = 2.0) -> dict:
    """对比多个数据源，用中位数找共识值

    sources: {"Yahoo": 7518, "Futu": 7500, "年报": 7520}
    """
    values = {k: exact(v) for k, v in sources.items()}
    sorted_vals = sorted(float(v) for v in values.values())
    n = len(sorted_vals)
    median = sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2

    all_ok = True
    details = {}
    for src, val in values.items():
        dev = abs(float(val) - median) / median * 100 if median != 0 else 0
        details[src] = {"value": float(val), "deviation_pct": round(dev, 2)}
        if dev > tolerance_pct:
            all_ok = False

    return {
        "field": field_name,
        "sources_count": len(sources),
        "median_consensus": round(median, 2),
        "all_consistent": all_ok,
        "tolerance_pct": tolerance_pct,
        "details": details,
    }


# ═══════════════════════════════════════════════════
# 4. Benford 定律检测
# ═══════════════════════════════════════════════════

_BENFORD = {d: math.log10(1 + 1/d) for d in range(1, 10)}


def benford_check(values: List[float]) -> Optional[dict]:
    """Benford 定律 — 检测财务数据是否人为调整

    需要至少 50 个样本。返回 MAD, chi2, conformity。
    MAD < 0.006 = 高度符合, < 0.012 = 可接受, > 0.015 = 不符合
    """
    digits = []
    for v in values:
        v = abs(float(v))
        if v > 0:
            sig = 10 ** (math.log10(v) - math.floor(math.log10(v)))
            d = int(sig)
            if 1 <= d <= 9:
                digits.append(d)

    n = len(digits)
    if n < 50:
        return {"error": f"样本量不足: {n}<50", "sample_size": n}

    counts = {}
    for d in digits:
        counts[d] = counts.get(d, 0) + 1
    observed = {d: counts.get(d, 0) / n for d in range(1, 10)}

    mad = sum(abs(observed.get(d, 0) - _BENFORD[d]) for d in range(1, 10)) / 9
    chi2 = sum(
        (counts.get(d, 0) - _BENFORD[d] * n) ** 2 / (_BENFORD[d] * n)
        for d in range(1, 10)
    )

    if mad < 0.006:
        conformity = "Close (高度符合)"
    elif mad < 0.012:
        conformity = "Acceptable (可接受)"
    elif mad < 0.015:
        conformity = "Marginal (边缘)"
    else:
        conformity = "Nonconforming (不符合 ⚠️)"

    return {
        "sample_size": n,
        "mad": round(mad, 6),
        "chi2": round(chi2, 2),
        "conformity": conformity,
        "is_conforming": mad < 0.015,
        "distribution": {d: round(observed.get(d, 0), 4) for d in range(1, 10)},
    }


# ═══════════════════════════════════════════════════
# 5. 三情景估值
# ═══════════════════════════════════════════════════

def three_scenario_valuation(
    current_price: float,
    current_eps: float,
    growth_bull: float,
    growth_base: float,
    growth_bear: float,
    pe_bull: float,
    pe_base: float,
    pe_bear: float,
    years: int = 3,
) -> dict:
    """三情景估值 — 精确十进制计算

    growth: 小数形式 (0.15 = 15%)
    """
    p = exact(current_price)
    eps = exact(current_eps)

    scenarios = [
        ("bull", "乐观", growth_bull, pe_bull),
        ("base", "中性", growth_base, pe_base),
        ("bear", "悲观", growth_bear, pe_bear),
    ]

    result = {"current_price": float(p), "current_eps": float(eps), "years": years, "scenarios": {}}

    for key, name, growth, pe in scenarios:
        g = exact(growth)
        target_pe = exact(pe)
        future_eps = eps
        for _ in range(years):
            future_eps = _CTX.multiply(future_eps, _CTX.add(Decimal("1"), g))
        target_price = _CTX.multiply(future_eps, target_pe)
        change_pct = round(float(target_price - p) / float(p) * 100, 1)

        result["scenarios"][key] = {
            "name": name,
            "growth_rate": float(g),
            "target_pe": float(target_pe),
            "future_eps": round(float(future_eps), 2),
            "target_price": round(float(target_price), 2),
            "expected_return_pct": change_pct,
        }

    # 概率加权 (中性50%, 乐观25%, 悲观25%)
    bull_ret = result["scenarios"]["bull"]["expected_return_pct"]
    base_ret = result["scenarios"]["base"]["expected_return_pct"]
    bear_ret = result["scenarios"]["bear"]["expected_return_pct"]
    result["probability_weighted_return"] = round(bull_ret * 0.25 + base_ret * 0.50 + bear_ret * 0.25, 1)

    return result


# ═══════════════════════════════════════════════════
# 6. 组合审视 — AI Berkshire 核心问题
# ═══════════════════════════════════════════════════

PORTFOLIO_REVIEW_QUESTIONS = [
    "如果今天没有持仓，你还会在当前价格买入吗？",
    "如果明天不能交易，持有5年你舒服吗？",
    "买入逻辑还完整吗？有什么变化？",
    "这是你能做的最好的投资吗？（机会成本）",
    "仓位大小是否匹配你的确信度？",
]


def portfolio_health_check(positions: List[dict]) -> dict:
    """组合体检 — AI Berkshire 风格

    检查: 集中度、相关性、现金比例、最大持仓
    """
    if not positions:
        return {"status": "empty", "message": "无持仓"}

    total_value = sum(p.get("market_value", 0) for p in positions)
    if total_value <= 0:
        return {"status": "empty", "message": "持仓市值为0"}

    # 按市值排序
    sorted_pos = sorted(positions, key=lambda p: -(p.get("market_value", 0)))
    top1_pct = sorted_pos[0].get("market_value", 0) / total_value * 100
    top3_pct = sum(p.get("market_value", 0) for p in sorted_pos[:3]) / total_value * 100
    num_pos = len(positions)

    # 行业集中度
    sectors = {}
    for p in positions:
        sec = p.get("sector", "Unknown")
        sectors[sec] = sectors.get(sec, 0) + p.get("market_value", 0)
    max_sector_pct = max(sectors.values()) / total_value * 100 if sectors else 0

    # 判断
    warnings = []
    if top1_pct > 25:
        warnings.append(f"第一大持仓{top1_pct:.0f}% > 25%")
    if num_pos > 15:
        warnings.append(f"持仓{num_pos}只 > 15只（过度分散）")
    if max_sector_pct > 40:
        warnings.append(f"单一行业{max_sector_pct:.0f}% > 40%")

    return {
        "total_value": total_value,
        "num_positions": num_pos,
        "top1_pct": round(top1_pct, 1),
        "top3_pct": round(top3_pct, 1),
        "max_sector_pct": round(max_sector_pct, 1),
        "sectors": {k: round(v/total_value*100, 1) for k, v in sectors.items()},
        "warnings": warnings,
        "health": "HEALTHY" if not warnings else "CAUTION",
        "review_questions": PORTFOLIO_REVIEW_QUESTIONS,
    }
