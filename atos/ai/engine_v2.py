"""ATOS PRO v3 — AI 决策引擎 v3（重写版）
===================================
核心变更（v3大改）:
  1. AI 只有否决权（veto），不能生成交易指令
  2. 主决策由因子引擎完成（规则驱动+统计）
  3. 删除所有 AI 买入/卖出指令生成逻辑
  4. CIO 只做组合级健康检查
  5. 大幅降低 API 调用频次和成本

流程：
  1. 查询历史记忆（统计胜率）
  2. CIO 组合健康检查（1次API/天）
  3. 对候选标的做 AI 否决（veto）
  4. 记录否决结果
"""

import json
import os
import datetime
from atos.ai.debate import cio_review, position_review, vetos
from atos.ai.memory import (
    get_memory_stats, get_mistake_patterns, get_similar_history,
    get_ai_confidence_adjustment, record_decision,
)
from atos.core.logging import get_logger

logger = get_logger("ai.engine_v2")


def get_advice_v3(snapshot: dict) -> dict:
    """AI 决策引擎 v3 主入口。

    相比 v2 的变化：
    - 不再生成 short_term_actions / long_term_actions
    - 只输出 veto_map: {symbol: {veto: bool, reason: str}}
    - 只输出 CIO 健康检查
    实际交易指令由 shadow_trader 中的因子引擎生成。
    """
    regime = snapshot.get("market_regime", {}).get("regime", "UNKNOWN")
    mem_stats = get_memory_stats()
    mistakes = get_mistake_patterns(min_count=2)

    logger.info(
        f"记忆: {mem_stats['total_decisions']}条决策 | "
        f"{mem_stats['win_count']}胜{mem_stats['loss_count']}负 | "
        f"胜率={mem_stats['win_rate']:.1%} | "
        f"错误模式={mem_stats['patterns_detected']}个"
    )

    # 1. 对已有持仓做复核（纯规则，不调API）
    position_reviews = position_review(snapshot)
    if position_reviews:
        cut_count = sum(1 for r in position_reviews if r['action'] == 'CUT')
        logger.info(f"持仓复核: {len(position_reviews)}个持仓, {cut_count}个建议CUT")

    # 2. CIO 组合健康检查（1次API/天）
    ci = cio_review(snapshot)

    # 3. 对候选标的做 AI 否决
    factor_rankings = snapshot.get("factor_rankings", [])
    candidate_symbols = [r["symbol"] for r in factor_rankings[:8] if r.get("score", 0) > 0.50] if factor_rankings else []

    veto_map = {}
    existing_symbols = {p.get("symbol") for p in snapshot.get("positions", [])}
    buy_candidates = [s for s in candidate_symbols if s not in existing_symbols][:5]

    if buy_candidates:
        veto_map = vetos(buy_candidates, snapshot)

    # 4. 记录本次决策统计
    for sym, v in veto_map.items():
        if v.get("veto", False):
            record_decision(
                symbol=sym,
                action="VETO",
                confidence=0.8,
                factor_score=0.5,
                reasons={"veto_reason": v.get("reason", "")},
                debate_summary=f"AI veto: {v.get('reason', '')[:80]}",
                market_regime=regime,
                snapshot=snapshot,
            )

    # 5. 风控建议（合并CIO + 持仓复核）
    risk_notes = ci.get("risk_notes", "")
    cut_symbols = [r["position"] for r in position_reviews if r["action"] == "CUT"]
    if cut_symbols:
        risk_notes += f" | 建议减仓: {', '.join(cut_symbols[:5])}"

    return {
        "short_term_actions": [],  # v3: AI不再生成交易指令
        "long_term_actions": [],
        "position_reviews": position_reviews,
        "veto_map": veto_map,
        "portfolio_health": ci.get("portfolio_health", "UNKNOWN"),
        "cio_market_read": ci.get("market_read", ""),
        "risk_notes": risk_notes,
        "market_read": ci.get("market_read", ""),
        "cycle_summary": (
            f"CIO health={ci.get('portfolio_health','?')} | "
            f"{len(position_reviews)} pos review | "
            f"{sum(1 for v in veto_map.values() if v['veto'])} vetoes"
        ),
    }


# ============================================================
# 兼容 v2 接口 — 旧代码调 get_advice_v2 不会崩溃
# ============================================================
def get_advice_v2(snapshot: dict) -> dict:
    """兼容v2接口，内部调用v3逻辑。"""
    return get_advice_v3(snapshot)
