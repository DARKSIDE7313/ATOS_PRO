"""
ATOS PRO — 巴菲特价值投资质量过滤器
======================================
移植自 AI Berkshire (9K Stars) 的核心筛选框架：

7条去劣指标:
  1. 10年平均ROE < 8% → 排除
  2. 5年累计自由现金流 < 0 → 排除
  3. 利息覆盖倍数 < 2x → 排除
  4. 长期毛利率 < 15% → 排除
  5. 经营现金流/净利润(5年均值) < 0.7 → 排除
  6. 长期净利率 < 5% → 排除
  7. 5年总股本膨胀 > 20% → 排除

3条豁免规则:
  A: 战略投入期 (上市<10年 + 毛利率>30% + OCF正)
  B: 主动低利润率 (毛利率>30% + 净利率回升)
  C: 高周转薄利 (ROE>20% + OCF/NI>1.0 + 会员制/平台模式)

六关Checklist: 能力圈 → 好生意 → 护城河 → 管理层 → 安全边际 → 纪律
"""

from typing import Dict, List, Optional, Tuple

# ── 7条去劣指标 ──

def quality_elimination_check(info: dict) -> dict:
    """对单只股票执行7条去劣指标检查

    返回: {passed: bool, failed_gates: [...], exemptions: [...], score: 0-7}
    """
    failed = []
    exemptions = []
    passed_count = 0

    # 1. 10年平均ROE — yfinance只有当前ROE，用当前ROE近似
    roe = info.get("returnOnEquity", 0) or 0
    if roe < 0.08:
        # 豁免A: 战略投入期
        gross_margin = info.get("grossMargins", 0) or 0
        ocf = info.get("operatingCashflow", 0) or 0
        if gross_margin > 0.30 and ocf > 0:
            exemptions.append(("A", "ROE低但处于战略投入期(毛利率>30%+OCF正)"))
            passed_count += 1
        else:
            failed.append(("ROE", f"ROE={roe:.1%} < 8%"))
    else:
        passed_count += 1

    # 2. 自由现金流
    fcf = info.get("freeCashflow", 0) or 0
    if fcf <= 0:
        failed.append(("FCF", f"自由现金流={fcf:,.0f} ≤ 0"))
    else:
        passed_count += 1

    # 3. 利息覆盖倍数 — 银行/金融业免检（业务模式不同）
    sector = info.get("sector", "")
    is_financial = sector in ("Financial Services", "Financial") or "bank" in str(info.get("industry", "")).lower()

    ebit = info.get("ebitda", 0) or 0
    interest = info.get("interestExpense", 0) or 0
    if not is_financial and interest > 0 and ebit > 0:
        coverage = ebit / interest
        if coverage < 2:
            failed.append(("利息覆盖", f"EBIT/利息={coverage:.1f}x < 2x"))
        else:
            passed_count += 1
    else:
        passed_count += 1  # 无利息费用视为通过

    # 4. 长期毛利率
    gross_margin = info.get("grossMargins", 0) or 0
    if gross_margin < 0.15:
        # 豁免C: 高周转薄利模式
        if roe > 0.20:
            exemptions.append(("C", f"毛利率{gross_margin:.1%}<15%但ROE={roe:.1%}>20%"))
            passed_count += 1
        else:
            failed.append(("毛利率", f"毛利率={gross_margin:.1%} < 15%"))
    else:
        passed_count += 1

    # 5. 经营现金流/净利润 (5年均值)
    ni = info.get("netIncomeToCommon", 0) or 0
    ocf = info.get("operatingCashflow", 0) or 0
    if ni > 0:
        ocf_ni_ratio = ocf / ni
        if ocf_ni_ratio < 0.7:
            failed.append(("OCF/NI", f"经营现金流/净利润={ocf_ni_ratio:.2f} < 0.7"))
        else:
            passed_count += 1
    else:
        passed_count += 1  # 亏损状态无法计算，不排除

    # 6. 长期净利率
    profit_margin = info.get("profitMargins", 0) or 0
    if profit_margin < 0.05:
        # 豁免B: 主动低利润率
        if gross_margin > 0.30:
            exemptions.append(("B", f"净利率{profit_margin:.1%}<5%但毛利率{gross_margin:.1%}>30%"))
            passed_count += 1
        # 豁免C
        elif roe > 0.20:
            exemptions.append(("C", f"净利率{profit_margin:.1%}<5%但ROE={roe:.1%}>20%"))
            passed_count += 1
        else:
            failed.append(("净利率", f"净利率={profit_margin:.1%} < 5%"))
    else:
        passed_count += 1

    # 7. 股本膨胀 (用股份数变化近似)
    shares_out = info.get("sharesOutstanding", 0) or 0
    shares_float = info.get("floatShares", 0) or 0
    # yfinance 没有历史股数，用 buyback 近似
    share_buyback = info.get("sharePercentChange", 0) or 0
    if share_buyback < -0.20:  # 股数增加>20%
        failed.append(("股本稀释", f"股份变动={share_buyback:.1%} (增加>20%)"))
    else:
        passed_count += 1

    return {
        "passed": len(failed) == 0,
        "score": passed_count,
        "max_score": 7,
        "failed_gates": failed,
        "exemptions": exemptions,
        "quality_grade": _quality_grade(passed_count),
    }


def _quality_grade(score: int) -> str:
    if score >= 7: return "A+ (一流公司)"
    if score >= 6: return "A (优秀)"
    if score >= 5: return "B (良好)"
    if score >= 4: return "C (及格)"
    if score >= 3: return "D (有瑕疵)"
    return "F (非一流)"


# ── 快速否决清单 (8条) ──

HARD_VETO_CHECKS = [
    ("不懂生意", "说不清楚这家公司怎么赚钱"),
    ("FCF持续为负", "连续3年自由现金流为负且看不到改善"),
    ("管理层诚信", "管理层有诚信污点/财务造假/关联交易"),
    ("护城河侵蚀", "竞争优势正在被不可逆地侵蚀"),
    ("博傻", "需要靠下一个接盘者出更高价来赚钱"),
    ("承受不起归零", "无法承受这笔投资归零的后果"),
    ("从众", "买入理由主要是'别人都在买'或'最近涨得好'"),
    ("说不清理由", "无法用200字以内写清楚买入理由"),
]


def quick_veto_check(info: dict, thesis: str = None) -> Tuple[bool, List[str]]:
    """快速否决 - 触发任何一条直接不买

    thesis=None 时不检查"说不清理由"（批量模式），
    thesis="" 时触发（严格模式）

    返回: (是否否决, [触发的否决项])
    """
    vetoed = []

    # 自动检测: FCF+OCF同时为负
    fcf = info.get("freeCashflow", 0) or 0
    ocf = info.get("operatingCashflow", 0) or 0
    if fcf <= 0 and ocf <= 0:
        vetoed.append("FCF持续为负")

    # 论文检查 — 仅在有论文上下文时检查
    if thesis is not None and (not thesis or len(str(thesis)) < 50):
        vetoed.append("说不清理由")

    return len(vetoed) > 0, vetoed


# ── 镜子测试 ──

MIRROR_TEST_TEMPLATE = """
我以 {price} 买入 {symbol}，因为:
1. 这门生意的本质是____，我理解它；
2. 它的护城河是____，而且在变宽/变窄；
3. 管理层____，值得/不值得信赖；
4. 当前价格相当于内在价值的____折，有/无足够安全边际；
5. 即使我错了，下行风险可控/不可控，因为____。
"""


# ── 批量筛选入口 ──

def batch_buffett_filter(symbols: List[str]) -> Dict[str, dict]:
    """批量执行巴菲特质量过滤

    返回: {symbol: {quality_result, veto_result, overall_pass}}
    """
    import yfinance as yf
    results = {}

    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
            quality = quality_elimination_check(info)
            vetoed, veto_reasons = quick_veto_check(info)

            results[sym] = {
                "symbol": sym,
                "quality": quality,
                "vetoed": vetoed,
                "veto_reasons": veto_reasons,
                "overall_pass": quality["passed"] and not vetoed,
                "grade": quality["quality_grade"],
                "key_metrics": {
                    "roe": f"{info.get('returnOnEquity', 0) or 0:.1%}",
                    "fcf": f"{info.get('freeCashflow', 0) or 0:,.0f}",
                    "gross_margin": f"{info.get('grossMargins', 0) or 0:.1%}",
                    "profit_margin": f"{info.get('profitMargins', 0) or 0:.1%}",
                    "debt_equity": f"{info.get('debtToEquity', 0) or 0:.1f}",
                },
            }
        except Exception as e:
            results[sym] = {"symbol": sym, "error": str(e)[:80]}

    return results
