"""
ATOS PRO v2 — AI 决策引擎 v2
============================
多理论辩论 + 记忆学习 + 置信度校准。

流程：
  1. 接收快照（信号+因子+市场状态）
  2. 查询历史记忆 → 获取相似情境的经验
  3. 多分析师辩论（价值/动量/质量/逆向/风控）
  4. 综合投票 → 最终决策 + 置信度
  5. 置信度校准（历史记忆调整）
  6. 记录决策到记忆库
"""

import json
import os
import requests
from atos.core.logging import get_logger
from atos.ai.debate import debate, batch_debate, cio_review, position_review
from atos.ai.memory import (
    get_similar_history, get_mistake_patterns,
    get_ai_confidence_adjustment, record_decision, get_memory_stats,
    write_trade_journal,
)

logger = get_logger("ai.engine_v2")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"  # Bug #12: 升级到 v4 Pro（原 deepseek-chat）
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

from atos.ai.validator import GROUNDING_RULES

SYNTHESIZER_PROMPT = """
You are the CHIEF INVESTMENT OFFICER synthesizing a multi-analyst debate.

INPUT:
- 5 analysts voted BUY/SELL/HOLD with confidence and reasoning.
- Factor rankings, market regime, VIX, cash buffer requirements.
- RISK METRICS: VaR(95%), CVaR(95%), portfolio beta, stress test results.
- Historical memory of similar decisions and their outcomes.

""" + GROUNDING_RULES + """

TASK:
1. Weigh analysts based on regime AND risk metrics.
2. Check historical memory for repeated mistakes.
3. Output FINAL decision as JSON with fields: short_term_actions, long_term_actions, risk_notes, lessons_applied, market_read.

RULES:
- Confidence < 0.5: reduce target_pct by half
- Confidence < 0.3: skip the trade entirely
- If history shows same symbol+regime has >2 losses, be extra cautious
- Diversify: no more than 2 stocks from same sector
"""


def get_advice_v2(snapshot: dict) -> dict:
    """
    AI 决策引擎 v2 主入口（Bug #14 重构版）。

    流程：
      1. 查询历史记忆
      2. 运行 CIO 全组合审查（1次 API 调用代替原来的 5×N 次）
      3. 对已有持仓做 position_review（HOLD/ADD/CUT）
      4. 对候选标的做 batch_debate（只辩论得分 > 0.65 的 3-4 个标的）
      5. 记录决策 + 写交易日志
    """
    if not API_KEY:
        logger.warning("DEEPSEEK_API_KEY 未设置，使用风控保守模式")
        return _fallback_v2()

    # 1. 查询历史记忆
    regime = snapshot.get("market_regime", {}).get("regime", "UNKNOWN")
    history = get_similar_history(regime, limit=10)
    mistakes = get_mistake_patterns(min_count=2)
    mem_stats = get_memory_stats()

    logger.info(
        f"记忆: {mem_stats['total_decisions']}条决策 | "
        f"{mem_stats['win_count']}胜{mem_stats['loss_count']}负 | "
        f"胜率={mem_stats['win_rate']:.1%} | "
        f"错误模式={mem_stats['patterns_detected']}个"
    )

    # 2. 对已有持仓做 position_review（轻量级，代替 5-分析师辩论）
    position_reviews = position_review(snapshot)
    logger.info(f"Position review: {len(position_reviews)} positions reviewed")

    # 3. 选出候选标的进行辩论（减少数量 + 预过滤）
    factor_rankings = snapshot.get("factor_rankings", [])
    # 只保留得分 > 0.65 的标的
    high_score_symbols = [
        r["symbol"] for r in factor_rankings
        if r.get("score", 0) > 0.65
    ] if factor_rankings else []

    if not high_score_symbols:
        # 没有高得分标的，从 top 取少量
        top_symbols = [r["symbol"] for r in factor_rankings[:4]] if factor_rankings else []
        logger.info("No high-score symbols (>0.65), using top 4 without filter")
    else:
        # 按得分排序取前 4 个
        high_score_symbols.sort(
            key=lambda s: next(
                (r.get("score", 0) for r in factor_rankings if r["symbol"] == s), 0
            ),
            reverse=True,
        )
        top_symbols = high_score_symbols[:4]
        logger.info(f"Pre-filter: debating top {len(top_symbols)} "
                     f"(from {len(high_score_symbols)} symbols with score > 0.65)")

    # 4. 对候选标的进行多分析师辩论
    #    只辩论我们还没有持仓的候选（已有持仓由 position_review 处理）
    existing_symbols = {p.get("symbol") for p in snapshot.get("positions", [])}
    debate_symbols = [s for s in top_symbols if s not in existing_symbols][:4]

    debate_results = []
    if debate_symbols:
        debate_results = batch_debate(debate_symbols, snapshot, max_symbols=4)
        for r in debate_results:
            # 置信度记忆调整
            adj = get_ai_confidence_adjustment(r["symbol"], regime)
            r["final_confidence"] = round(
                max(0.1, min(0.95, r["final_confidence"] + adj)), 3
            )
            r["confidence_adjustment"] = adj

            # 记录决策
            factor_score = next(
                (r2["score"] for r2 in factor_rankings if r2["symbol"] == r["symbol"]), 0.5
            )
            decision_id = record_decision(
                symbol=r["symbol"],
                action=r["final_action"],
                confidence=r["final_confidence"],
                factor_score=factor_score,
                reasons={k: v.get("reason", "") for k, v in r["analyst_opinions"].items()},
                debate_summary=r["debate_summary"],
                market_regime=regime,
                snapshot=snapshot,
            )
            r["decision_id"] = decision_id  # 追踪链路：BUY→SELL→outcome

    # 5. CIO 全组合审查（1次 API 调用替代原来的 5×N 次）
    cio_result = cio_review(snapshot)

    # 合并 CIO 结果 + 持仓复核 + 辩论结果
    merged = _merge_decisions(cio_result, position_reviews, debate_results, snapshot, regime, mem_stats, mistakes, history)

    # 6. 写交易日志
    try:
        write_trade_journal(merged, snapshot, regime)
    except Exception as e:
        logger.error(f"写交易日志失败: {e}")

    return merged


def _fallback_v2(debate_results: list = None) -> dict:
    """没有 API 时的保守回退"""
    actions = []
    if debate_results:
        for r in debate_results[:3]:
            if r["final_confidence"] > 0.55 and r["final_action"] == "BUY":
                actions.append({
                    "action": "BUY",
                    "symbol": r["symbol"],
                    "target_pct": min(0.10, 0.05 + r["final_confidence"] * 0.05),
                    "confidence": r["final_confidence"],
                    "reason": f"辩论多数通过 (置信度记忆调整: {r.get('confidence_adjustment', 0):+.2f})",
                })
    if not actions:
        actions = [{"action": "HOLD", "symbol": "SPY", "target_pct": 0,
                     "confidence": 0.3, "reason": "无高置信度买入信号"}]
    return {
        "short_term_actions": actions,
        "long_term_actions": [],
        "risk_notes": "fallback: 保守模式，无 AI API 或调用失败",
        "lessons_applied": [],
        "market_read": "无法获取 AI 分析",
    }


def _merge_decisions(cio_result: dict, position_reviews: list,
                      debate_results: list, snapshot: dict,
                      regime: str, mem_stats: dict,
                      mistakes: list, history: list) -> dict:
    """合并 CIO 审查、持仓复核、辩论结果，运行验证，输出最终决策。"""
    # 1. 构建基于位置复核 + CIO 结果的持仓操作
    position_actions = []
    for pr in position_reviews:
        action = pr.get("action", "HOLD")
        if action == "CUT":
            # 每笔减仓
            position_actions.append({
                "action": "SELL",
                "symbol": pr.get("position", ""),
                "target_pct": 0,
                "confidence": pr.get("confidence", 0.5),
                "reason": f"[持仓复核] {pr.get('reason', '')}",
            })
        elif action == "ADD":
            position_actions.append({
                "action": "BUY",
                "symbol": pr.get("position", ""),
                "target_pct": 0.03,  # 小幅加仓
                "confidence": pr.get("confidence", 0.5),
                "reason": f"[持仓复核] {pr.get('reason', '')}",
            })

    # 2. 从 CIO 结果获取新候选标的
    cio_candidates = cio_result.get("new_position_candidates", [])

    # 3. 从辩论结果获取新标的
    debate_actions = []
    for r in debate_results:
        if r["final_action"] == "BUY" and r["final_confidence"] > 0.5:
            debate_actions.append({
                "action": "BUY",
                "symbol": r["symbol"],
                "target_pct": min(0.10, 0.05 + r["final_confidence"] * 0.05),
                "confidence": r["final_confidence"],
                "reason": f"[辩论] {r.get('debate_summary', '')[:80]}",
            })
        elif r["final_action"] == "SELL" and r["final_confidence"] > 0.5:
            debate_actions.append({
                "action": "SELL",
                "symbol": r["symbol"],
                "target_pct": 0,
                "confidence": r["final_confidence"],
                "reason": f"[辩论] {r.get('debate_summary', '')[:80]}",
            })

    # 4. 合并所有动作
    all_actions = position_actions + cio_candidates + debate_actions

    # 去重（保留高置信度的）
    seen = {}
    for act in all_actions:
        sym = act.get("symbol", "")
        if sym in seen:
            if act.get("confidence", 0) > seen[sym].get("confidence", 0):
                seen[sym] = act
        else:
            seen[sym] = act
    unique_actions = list(seen.values())

    # 5. 构建最终输出（兼容旧格式）
    final = {
        "short_term_actions": unique_actions[:8],
        "long_term_actions": [],
        "position_reviews": position_reviews,
        "cio_position_reviews": cio_result.get("position_reviews", []),
        "debate_results": [
            {
                "symbol": r["symbol"],
                "votes": r["votes"],
                "final_action": r["final_action"],
                "final_confidence": r["final_confidence"],
            }
            for r in debate_results
        ],
        "risk_notes": cio_result.get(
            "risk_notes",
            f"Portfolio: {len(position_reviews)} positions reviewed, "
            f"{len(debate_results)} candidates debated"
        ),
        "market_read": cio_result.get("market_read", "CIO analysis"),
        "lessons_applied": [m["description"] for m in mistakes[:3]] if mistakes else [],
        "_cio_source": cio_result.get("_cio_source", "unknown"),
        "cycle_summary": (
            f"CIO review + {len(position_reviews)} position reviews + "
            f"{len(debate_results)} debates → {len(unique_actions)} actions"
        ),
    }

    # 6. 反幻觉验证
    from atos.ai.validator import validate_ai_output
    real_prices = {
        u.get("symbol"): u.get("price", 0)
        for u in snapshot.get("universe", [])
    }
    allowed = set(u.get("symbol", "") for u in snapshot.get("universe", []))
    allowed.update(snapshot.get("universe_long", []))
    allowed.update(snapshot.get("universe_short", []))
    for pos in snapshot.get("positions", []):
        if pos.get("symbol"):
            allowed.add(pos["symbol"])

    try:
        validation = validate_ai_output(final, real_prices, allowed)
        if validation.get("circuit_open"):
            logger.critical("熔断触发，回退到纯风控模式")
            return _fallback_v2(debate_results)

        if validation.get("rejected"):
            logger.warning(f"AI幻觉被拦截: {len(validation['rejected'])} 条 → 这些建议已丢弃")
            final["short_term_actions"] = validation.get("safe_actions", final["short_term_actions"])
            final["long_term_actions"] = []

        logger.info(f"合并完成: {len(unique_actions)} actions "
                     f"(from CIO={len(cio_candidates)}, review={len(position_actions)}, "
                     f"debate={len(debate_actions)})")
    except Exception as e:
        logger.warning(f"验证失败（跳过）: {e}")

    return final
