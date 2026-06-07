"""
ATOS PRO v2 — 透明报告生成器
=============================
每次运行后生成：
  1. 交易明细（买什么、卖什么、为什么）
  2. AI 辩论记录（每个分析师投了什么票、原因）
  3. 当日反思（哪里好、哪里不好、怎么改进）
"""

import os
import json
import datetime

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "reports", "transparency"
)
os.makedirs(REPORTS_DIR, exist_ok=True)


def generate_report(account, cycle: int, regime: dict, vix: float,
                    factor_rankings: list, debate_results: list = None,
                    trades: list = None, ai_risks: str = "",
                    errors: list = None) -> str:
    """生成完整透明报告"""
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    filename = f"report_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_cycle{cycle}.md"
    filepath = os.path.join(REPORTS_DIR, filename)

    # ===== 构建报告 =====
    lines = [
        f"# ATOS v2 透明报告 — Cycle {cycle}",
        f"**时间**: {today}",
        f"**市场**: {regime.get('regime', '?')} | VIX={vix:.1f} | 风险系数={regime.get('risk_multiplier', '?')}",
        "",
        "---",
        "",
        "## 账户状态",
        f"| 项目 | 金额 |",
        f"|------|------|",
        f"| 现金 | ${account.cash:,.2f} |",
        f"| 持仓市值 | ${account.total_equity - account.cash:,.2f} |",
        f"| **总资产** | **${account.total_equity:,.2f}** |",
        f"| 收益率 | {(account.total_equity - account.initial_cash)/account.initial_cash*100:+.2f}% |",
        "",
    ]

    # 持仓
    if account.positions:
        lines.append("## 当前持仓")
        lines.append("| 标的 | 数量 | 成本价 | 现价 | 市值 | 盈亏% |")
        lines.append("|------|------|--------|------|------|-------|")
        for sym, pos in account.positions.items():
            pnl_pct = (pos['last_price'] - pos['avg_price']) / pos['avg_price'] * 100 if pos['avg_price'] > 0 else 0
            mkt_val = pos['qty'] * pos['last_price']
            lines.append(
                f"| {sym} | {pos['qty']} | ${pos['avg_price']:.2f} | "
                f"${pos['last_price']:.2f} | ${mkt_val:,.0f} | {pnl_pct:+.1f}% |"
            )
    else:
        lines.append("## 当前持仓")
        lines.append("无持仓")
    lines.append("")

    # 交易记录
    if trades:
        lines.append("## 今日交易")
        lines.append("| 时间 | 方向 | 标的 | 数量 | 价格 | 盈亏 | 原因 |")
        lines.append("|------|------|------|------|------|------|------|")
        for t in trades:
            lines.append(
                f"| {t.get('date', '')[:19]} | {t['action']} | {t['symbol']} | "
                f"{t['shares']}股 | ${t['price']:.2f} | ${t.get('pnl',0):,.2f} | {t.get('reason','')[:40]} |"
            )
        lines.append("")

    # 因子排名
    if factor_rankings:
        lines.append("## 因子排名 Top 10")
        lines.append("| 排名 | 标的 | 总分 | 价值 | 动量 | 质量 | 技术 |")
        lines.append("|------|------|------|------|------|------|------|")
        for i, r in enumerate(factor_rankings[:10], 1):
            bd = r.get('breakdown', {})
            lines.append(
                f"| {i} | {r['symbol']} | {r['score']:.3f} | "
                f"{bd.get('value',0):.2f} | {bd.get('momentum',0):.2f} | "
                f"{bd.get('quality',0):.2f} | {bd.get('technical',0):.2f} |"
            )
        lines.append("")

    # AI 辩论详情
    if debate_results:
        lines.append("## AI 辩论详情")
        for d in debate_results:
            lines.append(f"### {d.get('symbol', '?')} — 最终: **{d.get('final_action', '?')}** (置信度 {d.get('final_confidence', 0):.0%})")
            votes = d.get('votes', {})
            lines.append(f"投票: 买入{votes.get('BUY',0)} | 卖出{votes.get('SELL',0)} | 观望{votes.get('HOLD',0)}")
            lines.append("")
            lines.append("| 分析师 | 决定 | 置信度 | 理由 |")
            lines.append("|--------|------|--------|------|")
            for key, op in d.get('analyst_opinions', {}).items():
                name = op.get('name', key)
                action = op.get('action', '?')
                conf = op.get('confidence', 0)
                reason = (op.get('reason', '') or '')[:80]
                risk = op.get('risk_flag', '')
                flag = f" ⚠️{risk}" if risk and risk != 'LOW' and risk != 'NONE' else ''
                lines.append(f"| {name} | {action} | {conf:.0%} | {reason}{flag} |")
            lines.append("")

    # CIO 综合
    if ai_risks:
        lines.append("## CIO 综合意见")
        lines.append(f"> {ai_risks}")
        lines.append("")

    # 反思（自动生成）
    lines.append("## 🔍 今日反思")
    lines.append("")
    lines.append("### ✅ 做对了什么")
    good = _analyze_good(account, trades, debate_results)
    for g in good:
        lines.append(f"- {g}")
    lines.append("")
    lines.append("### ❌ 哪里可以改进")
    bad = _analyze_bad(account, trades, debate_results, errors)
    for b in bad:
        lines.append(f"- {b}")
    lines.append("")
    lines.append("### 💡 下一步行动")
    actions = _suggest_actions(account, trades, debate_results)
    for a in actions:
        lines.append(f"- {a}")
    lines.append("")

    if errors:
        lines.append("## ⚠️ 错误记录")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    content = "\n".join(lines)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def _analyze_good(account, trades, debate_results) -> list:
    good = []
    if not trades:
        good.append("今天没有交易——在市场不明朗时保持观望是纪律性的表现")
        return good

    # 有共识的买入是好事
    if debate_results:
        high_conf = [d for d in debate_results if d.get('final_confidence', 0) > 0.6 and d.get('final_action') == 'BUY']
        if high_conf:
            good.append(f"高置信度买入信号: {', '.join(d['symbol'] for d in high_conf[:3])}")

    # 风险控制好
    if account.cash / account.total_equity > 0.05:
        good.append(f"保持了 {account.cash/account.total_equity*100:.0f}% 的现金缓冲，风险可控")

    if not good:
        good.append("系统按计划运行，无异常")
    return good


def _analyze_bad(account, trades, debate_results, errors) -> list:
    bad = []
    if errors:
        bad.extend(errors)

    # 集中度检查
    if account.positions and account.total_equity > 0:
        for sym, pos in account.positions.items():
            weight = pos['qty'] * pos['last_price'] / account.total_equity
            if weight > 0.20:
                bad.append(f"{sym} 仓位 {weight:.0%} 超过 20% 上限，过度集中")

    # 低置信度交易
    if debate_results:
        low_conf = [d for d in debate_results if d.get('final_confidence', 0) < 0.5 and d.get('final_action') != 'HOLD']
        if low_conf:
            bad.append(f"低置信度交易: {', '.join(d['symbol'] for d in low_conf[:3])}，应考虑观望")

    # 频繁交易
    if trades and len(trades) > 10:
        bad.append(f"今日交易 {len(trades)} 笔，可能过度交易")

    if not bad:
        bad.append("暂无发现明显问题")
    return bad


def _suggest_actions(account, trades, debate_results) -> list:
    actions = []

    # 如果现金太多，寻找机会
    cash_pct = account.cash / account.total_equity if account.total_equity > 0 else 1.0
    if cash_pct > 0.30:
        actions.append("现金占比超过 30%，可在下一个强信号时适当加仓")
    elif cash_pct < 0.05:
        actions.append("现金不足 5%，暂停新开仓，等待获利了结")

    # 检查是否需要人工干预
    if account.total_equity < account.initial_cash * 0.95:
        actions.append("资产回撤超过 5%，建议人工检查策略是否需要调整")

    # AI 记忆有数据后
    actions.append("定期查看 AI 记忆库: python3 -c \"from atos.ai.memory import get_memory_stats; print(get_memory_stats())\"")

    return actions if actions else ["系统运行正常，继续监控"]
