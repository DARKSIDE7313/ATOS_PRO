"""ATOS PRO v3 — 多理论辩论引擎（重写版）
===================================
核心变更（v3大改）:
  1. AI 只有**否决权（VETO）**，不再主动开仓
  2. 删除所有传染性卖出逻辑（禁止因A亏损而卖B）
  3. 每个标的独立决策
  4. CIO 审查只做组合级风险建议，不触发具体交易
  5. 主决策由因子引擎完成（shadow_trader中）

架构：
  - position_review: 对已有持仓做 HOLD/CUT（仅建议，不强制）
  - CIO review: 只输出市场解读和风险提示
  - 辩论：结果仅用于置信度评分，不直接生成交易
"""

import json
import os
import requests
from atos.core.logging import get_logger

logger = get_logger("ai.debate")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"  # 统一使用 deepseek-chat (直连DeepSeek API/OpenRouter 一致)
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ============================================================
# CIO — 全组合审查（只做建议，不动仓位）
# ============================================================
CIO_PROMPT = """You are the CHIEF INVESTMENT OFFICER. You review the ENTIRE portfolio.

CRITICAL RULE: You CANNOT generate trade orders. You ONLY output:
1. market_read: 1-2 sentence macro/tactical assessment
2. risk_notes: portfolio-level risks (sector concentration, VaR, drawdown)
3. portfolio_health: "HEALTHY" / "CAUTIOUS" / "DANGER"

Your analysis must:
- NOT recommend buying or selling any specific symbol
- NOT mention individual position actions
- Focus on portfolio-level risk and macro context

Output STRICT JSON: {"market_read": "...", "risk_notes": "...", "portfolio_health": "HEALTHY|CAUTIOUS|DANGER"}
"""


def cio_review(snapshot: dict) -> dict:
    """CIO reviews the ENTIRE portfolio — outputs only market read + risk notes."""
    if not API_KEY:
        return _cio_fallback(snapshot)

    try:
        cio_input = _build_cio_input(snapshot)
        payload = {
            "model": MODEL,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": CIO_PROMPT},
                {"role": "user", "content": json.dumps(cio_input, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        result = json.loads(resp.json()["choices"][0]["message"]["content"])
        result["_cio_source"] = "api"
        logger.info(f"CIO review: {result.get('portfolio_health', 'N/A')}")
        return result
    except Exception as e:
        logger.error(f"CIO review failed: {e}")
        return _cio_fallback(snapshot)


def _build_cio_input(snapshot: dict) -> dict:
    """Build CIO input from snapshot — no position-level data that could trigger trades."""
    positions = snapshot.get("positions", [])
    return {
        "market_regime": snapshot.get("market_regime", "UNKNOWN"),
        "num_positions": len(positions),
        "position_count": len(positions),
        "sectors": list(set(p.get("sector", "Unknown") for p in positions)),
        "cash_pct": snapshot.get("constraints", {}).get("current_cash_pct", 0),
        "total_equity": snapshot.get("total_equity", 0),
        "vix": snapshot.get("vix", 18),
        "macro_data": snapshot.get("macro_data", {}),
    }


def _cio_fallback(snapshot: dict) -> dict:
    """Rule-based CIO fallback."""
    positions = snapshot.get("positions", [])
    total_equity = snapshot.get("total_equity", 0)
    cash_pct = snapshot.get("constraints", {}).get("current_cash_pct", 0)

    sector_count = len(set(p.get("sector", "Unknown") for p in positions))

    risk_notes = f"Portfolio: {len(positions)} positions across ~{max(1,sector_count)} sectors, cash {cash_pct:.1%}"
    if sector_count < 3 and len(positions) >= 5:
        risk_notes += " | ⚠️ Low sector diversification"
    if len(positions) > 10:
        risk_notes += " | ⚠️ Many positions — consider reducing count"

    health = "HEALTHY"
    if cash_pct < 0.05 and total_equity > 500000:
        health = "CAUTIOUS"
        risk_notes += " | Cash buffer low for $1M portfolio"

    return {
        "market_read": "CIO fallback: using rule-based analysis",
        "risk_notes": risk_notes,
        "portfolio_health": health,
        "_cio_source": "fallback",
    }


# ============================================================
# 持仓复核 — 只输出建议，不触发交易
# ============================================================
POSITION_REVIEW_PROMPT = """You are a PORTFOLIO ANALYST reviewing existing positions.

For each position, decide HOLD or CUT based on P&L, position size, and factor score.

CRITICAL RULE: Your decision for symbol X must ONLY consider data about symbol X.
DO NOT use other symbols' performance to decide X.

- P&L < -8% and position > 5% of portfolio → CUT
- Factor score < 0.4 and losing → CUT
- Otherwise → HOLD

Output JSON: {"position": "AAPL", "action": "HOLD|CUT", "confidence": 0.0-1.0, "reason": "..."}
"""


def position_review(snapshot: dict) -> list:
    """Review ALL existing positions: HOLD or CUT (NOT ADD — that's factor engine's job)."""
    positions = snapshot.get("positions", [])
    if not positions:
        return []

    factor_rankings = snapshot.get("factor_rankings", [])
    ranking_map = {r["symbol"]: r.get("score", 0) for r in factor_rankings}

    # 只用规则逻辑（LLM调用太慢且无优势）
    return _position_review_rule_based(positions, ranking_map)


def _position_review_rule_based(positions: list, ranking_map: dict) -> list:
    """Rule-based position review — clear, deterministic, no API calls.

    Each symbol is evaluated INDEPENDENTLY — no contagious selling.
    """
    results = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        pnl = pos.get("pnl_pct", 0)
        pos_pct = pos.get("position_pct", 0)
        score = ranking_map.get(symbol, 0.5)

        # 规则1: 亏损超8%且仓位>5% → CUT
        if pnl < -0.08 and pos_pct > 0.05:
            results.append({
                "position": symbol, "action": "CUT", "confidence": 0.7,
                "reason": f"Loss {pnl:.1%} at {pos_pct:.1%} weight"
            })
        # 规则2: 亏损超4%且因子评分低 → CUT
        elif pnl < -0.04 and score < 0.40:
            results.append({
                "position": symbol, "action": "CUT", "confidence": 0.6,
                "reason": f"Loss {pnl:.1%} + low factor score {score:.2f}"
            })
        # 规则3: 仓位过大 > 15% → CUT一半
        elif pos_pct > 0.15:
            results.append({
                "position": symbol, "action": "CUT", "confidence": 0.5,
                "reason": f"Overweight {pos_pct:.1%} — reducing"
            })
        else:
            results.append({
                "position": symbol, "action": "HOLD", "confidence": 0.8,
                "reason": f"PnL {pnl:+.2%}, score {score:.2f} — holding"
            })

    logger.info(f"Position review: {len(results)} positions -> "
                f"{sum(1 for r in results if r['action']=='CUT')} CUT, "
                f"{sum(1 for r in results if r['action']=='HOLD')} HOLD")
    return results


# ============================================================
# 单标的否决评估（AI唯一能做的事）
# ============================================================
VETO_PROMPT = """You are the RISK ADVISOR. Your ONLY job is to VETO bad trades.

You receive: the symbol, its factor scores, and the proposed action (BUY/HOLD).
You decide: does this trade have a FATAL flaw?

CRITICAL RULES:
- Base your veto ONLY on this symbol's data — ignore all other positions
- A veto means: "this trade is DANGEROUS and should not proceed"
- Veto reasons must be specific to this symbol (not "market is bad")
- DO NOT generate alternative trades — just veto or approve
- Default to APPROVE (do not veto) unless you have a clear, specific reason

Veto triggers (symbol-specific only):
- P/E > 50 with negative earnings growth → VETO (valuation bubble)
- RSI > 85 with declining volume → VETO (overbought exhaustion)
- Quality score < 0.3 with rising debt → VETO (fundamental deterioration)
- Price > upper Bollinger Band + RSI > 80 → VETO (extreme overbought)
- Volume < 20% of average → VETO (illiquid)

Output JSON: {"symbol": "...", "veto": true/false, "reason": "...", "risk_level": "LOW|MEDIUM|HIGH"}

IMPORTANT: Only veto when there is a CLEAR and SPECIFIC risk. Default to APPROVE.
"""


def vetos(symbols: list[str], snapshot: dict) -> dict:
    """Evaluate potential buy candidates. Returns veto decisions.

    AI only has VETO power — it can block bad trades but cannot force trades.
    """
    if not API_KEY or not symbols:
        return {}

    results = {}
    factor_rankings = snapshot.get("factor_rankings", [])
    ranking_map = {r["symbol"]: r.get("score", 0) for r in factor_rankings}
    universe_map = {u.get("symbol"): u for u in snapshot.get("universe", [])}

    for sym in symbols:
        # 先用规则过滤明显差的
        score = ranking_map.get(sym, 0.5)
        data = universe_map.get(sym, {})

        # 规则过滤
        rsi = data.get("rsi", 50)
        price = data.get("price", 0)
        ma200 = data.get("ma200", 0)
        pe = data.get("pe_ratio", 0)

        # 规则否决1: 超买（v4: 从RSI 85降到80，防追高）
        if rsi > 80 and price > ma200 * 1.15:
            results[sym] = {"veto": True, "reason": f"RSI {rsi:.0f}超买+价格偏离MA200>15%", "risk_level": "HIGH"}
            logger.info(f"🧠 AI否决 {sym}: {results[sym]['reason']}")
            continue

        # 规则否决2: 质量极差
        quality = data.get("quality_score", 0.5)
        if quality < 0.3 and score < 0.4:
            results[sym] = {"veto": True, "reason": f"质量评分{quality:.2f}极低+因子得分{score:.2f}", "risk_level": "HIGH"}
            logger.info(f"🧠 AI否决 {sym}: {results[sym]['reason']}")
            continue

        # 规则否决3: P/E过高
        if pe > 50 and price > ma200 * 1.15:
            results[sym] = {"veto": True, "reason": f"P/E {pe:.0f}过高+价格偏离", "risk_level": "MEDIUM"}
            logger.info(f"🧠 AI否决 {sym}: {results[sym]['reason']}")
            continue

        # 规则否决4: 成交量过低
        vol_ratio = data.get("volume_ratio", 1.0)
        if vol_ratio < 0.2:
            results[sym] = {"veto": True, "reason": f"成交量仅为均值的{vol_ratio:.0%} — 流动性不足", "risk_level": "MEDIUM"}
            logger.info(f"🧠 AI否决 {sym}: {results[sym]['reason']}")
            continue

        # 通过所有规则过滤 → 用 LLM 做最后一次审查（每5个标的才调一次API）
        if len(symbols) < 5 or sym in symbols[:2]:
            try:
                veto_input = {
                    "symbol": sym, "factor_score": score,
                    "rsi": rsi, "price": price, "ma200": ma200,
                    "pe_ratio": pe, "quality_score": quality,
                    "volume_ratio": vol_ratio,
                    "proposed_action": "BUY",
                    "sector": data.get("sector", "Unknown"),
                }
                payload = {
                    "model": MODEL, "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": VETO_PROMPT},
                        {"role": "user", "content": json.dumps(veto_input, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                }
                headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
                resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                result = json.loads(resp.json()["choices"][0]["message"]["content"])
                if result.get("veto", False):
                    results[sym] = {"veto": True, "reason": result.get("reason", "LLM否决"), "risk_level": result.get("risk_level", "MEDIUM")}
                    logger.info(f"🧠 AI-LLM否决 {sym}: {result.get('reason', '')[:60]}")
                    continue
            except Exception as e:
                logger.debug(f"AI否决 {sym} API调用失败: {e}")

        # 通过所有检查
        results[sym] = {"veto": False, "reason": "Passed all filters", "risk_level": "LOW"}

    logger.info(f"AI否决审查: {sum(1 for r in results.values() if r['veto'])}/{len(results)} 票否决")
    return results


# ============================================================
# 遗留接口兼容（删除batch_debate、debate等过时函数）
# 原函数保留空壳避免import错误
# ============================================================

def debate(symbol: str, snapshot: dict) -> dict:
    """DEPRECATED v3: 不再使用5分析师辩论"""
    return {"symbol": symbol, "final_action": "HOLD", "final_confidence": 0.3,
            "analyst_opinions": {}, "debate_summary": "DEPRECATED: AI no longer makes trade decisions",
            "risk_flags": []}


def batch_debate(symbols: list, snapshot: dict, max_symbols: int = 4) -> list:
    """DEPRECATED v3: 不再使用批辩论"""
    return []
