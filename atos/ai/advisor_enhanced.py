"""
ATOS AI — 增强决策顾问 (v6)
===========================
替换低胜率(6.4%)的旧AI辩论系统。
新架构: 因子引擎(主) + 情报增强(辅) + AI confidence调整

核心改进:
  1. AI不再是"否决者"，而是"信心调节器"
  2. 融合实时情报(新闻+情绪+VIX)到决策
  3. 硬性风险规则（不可被AI覆盖）
  4. 历史决策追踪 → 学习什么有效
  5. 简单直接，不做复杂的多代理辩论

用法:
  from atos.ai.advisor_enhanced import get_enhanced_advice
  advice = get_enhanced_advice(snapshot, intel_briefing)
"""

import json, os, datetime, math
from typing import Dict, List, Optional
from atos.core.logging import get_logger

logger = get_logger("ai.advisor_v6")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DECISIONS_PATH = os.path.join(BASE, "data", "ai_decisions.json")


# ═══════════════════════════════════════════
# Hard Risk Rules (AI cannot override these)
# ═══════════════════════════════════════════

HARD_RULES = {
    "max_rsi_buy": 75,        # RSI > 75 → 禁止买入
    "min_rsi_buy": 28,        # RSI < 28 → 不要抄底
    "require_trend": True,     # 必须趋势向上 (UP/WEAK_UP)
    "max_single_position": 0.15,  # 单仓上限15%
    "min_volume_ratio": 0.3,  # 不能严重缩量
    "max_fear_greed": 85,     # 极度贪婪 → 减仓
    "min_fear_greed": 15,     # 极度恐惧 → 可加仓
    "vix_high": 28,           # VIX > 28 → 减半仓位
    "vix_extreme": 35,        # VIX > 35 → 只卖不买
}

# Regime-based position sizing
REGIME_SIZING = {
    "BULL_STRONG": {"base_pct": 0.10, "max_pct": 0.15, "boost": 1.5},
    "BULL_WEAK":   {"base_pct": 0.08, "max_pct": 0.12, "boost": 1.2},
    "SIDEWAYS":    {"base_pct": 0.06, "max_pct": 0.10, "boost": 1.0},
    "BEAR":        {"base_pct": 0.04, "max_pct": 0.06, "boost": 0.6},
    "HIGH_VOL":    {"base_pct": 0.03, "max_pct": 0.06, "boost": 0.5},
    "UNKNOWN":     {"base_pct": 0.05, "max_pct": 0.08, "boost": 0.8},
}


def _load_decisions() -> dict:
    """加载历史AI决策"""
    if os.path.exists(DECISIONS_PATH):
        try:
            with open(DECISIONS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"decisions": [], "stats": {"total": 0, "correct": 0, "win_rate": 0.5}}


def _save_decision(symbol: str, verdict: str, confidence: float,
                   reason: str, market_regime: str):
    """保存AI决策"""
    data = _load_decisions()
    data["decisions"].append({
        "time": datetime.datetime.now().isoformat(),
        "symbol": symbol,
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason[:200],
        "regime": market_regime,
        "outcome": None,  # Will be filled later
    })
    # Keep last 200
    data["decisions"] = data["decisions"][-200:]
    os.makedirs(os.path.dirname(DECISIONS_PATH), exist_ok=True)
    with open(DECISIONS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _apply_hard_rules(symbol: str, signal: dict, market: dict,
                      intel: dict = None) -> Optional[str]:
    """
    应用硬性风险规则。返回 None = 通过, 否则返回拒绝原因。
    这些规则 AI 不可覆盖。
    """
    rsi = signal.get("rsi", 50)
    trend = signal.get("trend", "NEUTRAL")
    price = signal.get("price", 0)
    ma50 = signal.get("ma50", price)
    vol_r = signal.get("volume_ratio", 1.0)

    # RSI 检查
    if rsi > HARD_RULES["max_rsi_buy"]:
        return f"RSI={rsi:.0f}>75 超买"

    if rsi < HARD_RULES["min_rsi_buy"]:
        return f"RSI={rsi:.0f}<28 弱势"

    # 趋势检查
    if HARD_RULES["require_trend"] and trend == "DOWN":
        return "趋势DOWN"

    # 缩量检查
    if vol_r < HARD_RULES["min_volume_ratio"]:
        return f"严重缩量 vol_r={vol_r:.2f}"

    # 价格 vs MA50
    if ma50 > 0 and price < ma50 * 0.92:
        return f"价格远低于MA50({price:.0f}<{ma50:.0f})"

    # VIX 检查 (from intel)
    if intel:
        vix_data = intel.get("market_sentiment", {}).get("vix", {})
        vix = vix_data.get("vix", 18)
        if vix > HARD_RULES["vix_extreme"]:
            return f"VIX={vix:.0f}>35 极度恐慌"

    # Fear & Greed
    if intel:
        fg = intel.get("market_sentiment", {}).get("fear_greed", {})
        fg_score = fg.get("score", 50)
        if fg_score > HARD_RULES["max_fear_greed"]:
            return f"极度贪婪({fg_score}), 谨慎追高"

    return None  # All rules pass


def _compute_confidence(symbol: str, factor_score: float, signal: dict,
                        market: dict, intel: dict = None) -> float:
    """
    计算综合信心分数 (0.0-1.0)。
    融合: 因子分(40%) + 技术面(30%) + 情绪面(15%) + 情报面(15%)
    """
    confidence = 0.0

    # 1. 因子分数 (40%)
    confidence += factor_score * 0.40

    # 2. 技术面 (30%)
    tech_score = 0.0
    rsi = signal.get("rsi", 50)
    trend = signal.get("trend", "NEUTRAL")
    macd = signal.get("macd_hist", 0)

    if trend == "UP": tech_score += 0.4
    elif trend == "WEAK_UP": tech_score += 0.25
    elif trend == "DOWN": tech_score -= 0.3

    if 40 <= rsi <= 65: tech_score += 0.25
    elif 35 <= rsi < 40: tech_score += 0.15
    elif 65 < rsi <= 72: tech_score += 0.05
    elif rsi > 75: tech_score -= 0.2

    if macd > 0: tech_score += 0.15
    else: tech_score -= 0.1

    if signal.get("volume_ratio", 1.0) > 1.2: tech_score += 0.1

    tech_score = max(0, min(1.0, tech_score + 0.5))  # Normalize to 0-1
    confidence += tech_score * 0.30

    # 3. 情绪面 (15%)
    if intel:
        sentiment = intel.get("market_sentiment", {})
        bias = sentiment.get("bias", "NEUTRAL")
        if bias == "BULLISH": confidence += 0.15
        elif bias == "BEARISH": confidence += 0.02
        else: confidence += 0.08
    else:
        confidence += 0.08

    # 4. 情报面 (15%) — news impact on this specific stock
    if intel:
        watchlist_news = intel.get("watchlist_news", {})
        stock_news = watchlist_news.get(symbol, [])
        if stock_news:
            max_impact = max(n.get("impact_score", 0) for n in stock_news)
            confidence += max_impact * 0.15

    # 5. 体制调整
    regime = market.get("regime", "UNKNOWN")
    if "BULL" in regime:
        confidence *= 1.10
    elif "BEAR" in regime:
        confidence *= 0.75

    return round(min(1.0, confidence), 3)


def get_enhanced_advice(snapshot: dict,
                        intel_briefing: dict = None) -> dict:
    """
    增强版 AI 决策（替换旧 AI veto）。

    Args:
        snapshot: {
            market: {spy_price, vix, regime, spy_trend},
            total_equity, cash,
            positions: [{symbol, qty, avg_price, pnl_pct, ...}],
            candidates: [{symbol, price, rsi, trend, factor_score, ...}],
        }
        intel_briefing: from atos.intel.briefing.get_pre_trade_briefing()

    Returns:
        {
            decisions: [{symbol, action, confidence, reason}],
            risk_adjustment: float,     # 整体仓位系数
            market_read: str,           # 市场解读
            trading_allowed: bool,
            suggested_cash_pct: float,
        }
    """
    market = snapshot.get("market", {})
    regime = market.get("regime", "UNKNOWN")
    vix = market.get("vix", 18)
    candidates = snapshot.get("candidates", [])
    positions = snapshot.get("positions", [])
    total_equity = snapshot.get("total_equity", 300000)

    # Hard risk checks
    trading_allowed = True
    risk_reasons = []

    if vix > HARD_RULES["vix_extreme"]:
        trading_allowed = False
        risk_reasons.append(f"VIX={vix:.0f}>35 极度恐慌, 暂停交易")

    if intel_briefing:
        risk_flags = intel_briefing.get("risk_flags", [])
        high_risks = [f for f in risk_flags if f.get("level") == "HIGH"]
        if len(high_risks) >= 2:
            trading_allowed = False
            risk_reasons.append(f"{len(high_risks)}个高风险信号")

    # Process each candidate
    decisions = []
    for cand in candidates[:10]:
        sym = cand.get("symbol", "?")
        factor_score = cand.get("factor_score", cand.get("score", 0.5))
        signal = {
            "rsi": cand.get("rsi", 50),
            "trend": cand.get("trend", "NEUTRAL"),
            "macd_hist": cand.get("macd_hist", 0),
            "volume_ratio": cand.get("volume_ratio", 1.0),
            "price": cand.get("price", 0),
            "ma50": cand.get("ma50", 0),
            "bollinger": cand.get("bollinger", {}),
        }

        # 1. Hard rules (always enforced)
        block_reason = _apply_hard_rules(sym, signal, market, intel_briefing)
        if block_reason:
            decisions.append({
                "symbol": sym, "action": "SKIP",
                "confidence": 0.0, "reason": f"硬规则: {block_reason}",
            })
            continue

        # 2. Confidence scoring (AI-enhanced)
        confidence = _compute_confidence(sym, factor_score, signal,
                                         market, intel_briefing)

        # 3. Decision (v18: lowered thresholds — AI is advisor, not gatekeeper)
        if confidence >= 0.45:
            action = "BUY"
            reason = f"综合信心{confidence:.2f} (因子{factor_score:.2f})"
        elif confidence >= 0.30:
            action = "WATCH"
            reason = f"信心不足{confidence:.2f}, 观察"
        else:
            action = "SKIP"
            reason = f"信心太低{confidence:.2f}"

        decisions.append({
            "symbol": sym, "action": action,
            "confidence": confidence, "reason": reason,
            "factor_score": factor_score,
        })

        _save_decision(sym, action, confidence, reason, regime)

    # Position sizing adjustment
    sizing = REGIME_SIZING.get(regime, REGIME_SIZING["UNKNOWN"])
    risk_adj = sizing["boost"]

    # VIX adjustment
    if vix > HARD_RULES["vix_high"]:
        risk_adj *= 0.6
    elif vix > 22:
        risk_adj *= 0.8

    # Intel adjustment
    if intel_briefing:
        sentiment = intel_briefing.get("market_sentiment", {})
        if sentiment.get("bias") == "BEARISH":
            risk_adj *= 0.75
        elif sentiment.get("bias") == "BULLISH":
            risk_adj *= 1.15

    risk_adj = max(0.2, min(1.5, risk_adj))

    # Suggested cash %
    if regime == "BEAR":
        suggested_cash = 0.30
    elif vix > 25:
        suggested_cash = 0.20
    else:
        suggested_cash = 0.05

    buys = [d for d in decisions if d["action"] == "BUY"]
    skips = [d for d in decisions if d["action"] == "SKIP"]

    return {
        "decisions": decisions,
        "buy_count": len(buys),
        "skip_count": len(skips),
        "risk_adjustment": round(risk_adj, 2),
        "market_read": f"{regime} | VIX={vix:.0f} | "
                       f"{'可交易' if trading_allowed else '暂停'}",
        "trading_allowed": trading_allowed,
        "risk_reasons": risk_reasons,
        "suggested_cash_pct": suggested_cash,
        "suggested_position_pct": round(sizing["base_pct"] * risk_adj, 3),
        "hard_rules_applied": True,
    }


def get_ai_verdict_for_symbol(symbol: str, factor_score: float,
                               signal_data: dict, market: dict,
                               intel: dict = None) -> dict:
    """单只标的的快速 AI 判断（轻量版）"""
    block = _apply_hard_rules(symbol, signal_data, market, intel)
    if block:
        return {"verdict": "BLOCKED", "reason": block, "confidence": 0.0}

    conf = _compute_confidence(symbol, factor_score, signal_data, market, intel)

    if conf >= 0.55:
        verdict = "BUY"
    elif conf >= 0.40:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    return {"verdict": verdict, "confidence": conf,
            "reason": f"综合信心{conf:.2f}"}
