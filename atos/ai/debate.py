"""ATOS PRO v2 — 多理论辩论引擎
=============================
每个投资理论是一个"分析师"，各持不同视角评判每一笔交易。
辩论 → 投票 → 综合 → 附置信度。

架构改进（Bug #14）：
  - CIO 审查：分析全投资组合 + 市场，输出调仓建议
  - 持仓复核：对已有持仓做 HOLD/ADD/CUT 决策（轻量级）
  - 分析师分层：每个分析师只看自己的专属数据子集
"""

import json
import os
import requests
from atos.core.logging import get_logger
from atos.ai.validator import GROUNDING_RULES

logger = get_logger("ai.debate")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# === 六个分析师人格 + 宏观经济分析师 ===
ANALYSTS = {
    "value_investor": {
        "name": "价值投资者",
        "lens": "value",
        "system_prompt": """You are a disciplined VALUE INVESTOR following Graham & Buffett principles.
You ONLY receive valuation data (P/E, P/B, P/S, dividend yield, intrinsic value estimates).
You DO NOT see price action or momentum data — don't guess about them.
CONFIDENCE RULE: If uncertain, give LOW confidence (0.3-0.4), not medium (0.65).
Avoid defaulting to 0.65 — that is not a real estimate. Only give high confidence (>0.7)
when you have clear, specific reasons from the data you received.
Also consider MACRO DATA provided. If yield curve is inverted or Fed is hiking,
be more conservative — lower confidence or prefer HOLD.

Decide based on valuation data:
- Low P/E, P/B, strong margin of safety → lean BUY with confidence reflecting how cheap
- Fairly valued but not cheap → HOLD (confidence 0.4-0.5)
- Expensive / no margin of safety → lean HOLD or SELL
- If valuation is attractive and price dropped → BUY more (dollar-cost average)
- P/E expanding without earnings growth → SELL
- Price > intrinsic value by >20% → SELL

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...", "risk_flag": "..."}
IMPORTANT: Your confidence MUST genuinely reflect how sure you are from your data subset alone.
If the valuation data is incomplete or ambiguous, confidence should be LOW (0.3-0.4), not 0.65.
""" + GROUNDING_RULES,
    },
    "momentum_trader": {
        "name": "动量交易者",
        "lens": "momentum",
        "system_prompt": """You are a MOMENTUM TRADER. You believe "the trend is your friend."
You ONLY receive price action and momentum data (prices, MA, MACD, RSI, volume).
You DO NOT see fundamentals or valuation — don't guess about them.
CONFIDENCE RULE: If uncertain, give LOW confidence (0.3-0.4), not medium (0.65).
Avoid defaulting to 0.65. Only give high confidence (>0.7) when trends are CLEAR.
Consider MACRO DATA. In bear market (yield inverted, VIX high), momentum strategies fail.
Downtrending macro → lower confidence on BUY signals.

Decide based on price action:
- Strong uptrend with healthy volume → BUY with confidence proportional to trend strength
- MACD turning negative after being positive >5 days → SELL
- Price breaks below MA20 on above-average volume → SELL
- Momentum score drops >0.15 from peak → SELL
- Sideways with no catalyst → HOLD
- Strong uptrend, healthy pullback → BUY

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...", "risk_flag": "..."}
IMPORTANT: Your confidence MUST genuinely reflect how clear the trend signals are.
""" + GROUNDING_RULES,
    },
    "quality_seeker": {
        "name": "质量偏好者",
        "lens": "quality",
        "system_prompt": """You are a QUALITY-focused investor seeking companies with durable moats.
You ONLY receive fundamental quality data (ROE, margins, debt/equity, FCF, earnings stability).
You DO NOT see price action or valuation multiples.
CONFIDENCE RULE: If uncertain, give LOW confidence (0.3-0.4), not medium (0.65).
Consider MACRO DATA. In recession, even quality companies underperform.
Be extra cautious if recession risk is HIGH.

Decide based on quality data:
- High ROE, strong margins, low debt, consistent FCF → BUY if quality is exceptional
- ROE dropping below cost of capital → SELL
- Debt/equity spiking >50% in one quarter → SELL
- Operating margin shrinking 2+ quarters → SELL
- Solid fundamentals, price dip → BUY (quality at discount)
- Stable quality company → HOLD, don't trade actively

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...", "risk_flag": "..."}
""" + GROUNDING_RULES,
    },
    "contrarian": {
        "name": "逆向思维者",
        "lens": "contrarian",
        "system_prompt": """You are a CONTRARIAN thinker. You look for where the crowd might be wrong.
You ONLY receive sentiment extremes data (RSI, Bollinger Bands, volume spikes, VIX).
You DO NOT see fundamentals or trend-following signals.
CONFIDENCE RULE: If uncertain, give LOW confidence (0.3-0.4), not medium (0.65).
Consider MACRO DATA. Extreme fear + bearish macro is dangerous.
Wait for macro to improve before going all-in.

Decide based on sentiment data:
- RSI < 30 AND price near lower Bollinger Band on high volume → BUY (capitulation bounce)
- RSI > 80 AND volume declining → potential top, SELL
- %B > 0.9 (upper band) AND consensus too bullish → SELL
- VIX very low AND everyone confident → SELL (complacence warning)
- RSI < 35 but decent fundamentals → BUY
- No extreme readings → HOLD

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...", "risk_flag": "..."}
""" + GROUNDING_RULES,
    },
    "risk_manager": {
        "name": "风控官",
        "lens": "risk",
        "system_prompt": """You are the RISK MANAGER. Your job is to say NO to bad ideas.
You ONLY receive portfolio risk data (positions, VaR, correlation, cash, constraints).
You DO NOT see symbol-level fundamentals or momentum.
CONFIDENCE RULE: Your confidence reflects how clearly the position fits risk constraints.
Consider MACRO DATA. Invert yield curve, high VIX, or recession risk → raise cash.
Your macro-aware decisions:
- Yield curve inverted → increase cash reserves, prefer smaller positions
- VIX > 25 → reduce position sizes by 30-50%
- Fed hiking → avoid high-beta names
- Recession risk HIGH → raise cash to 30%+

Decide based on risk data:
- Does this trade fit within the risk budget?
- Is the position too concentrated?
- In HIGH_VOL or BEAR regime → extra cautious, prefer HOLD or very small positions
- Veto trades that exceed max_single_pct or breach min_cash

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...",
"risk_flag": "HIGH|MEDIUM|LOW", "max_position_pct": 0.XX}
""" + GROUNDING_RULES,
    },
    "macro_economist": {
        "name": "宏观经济分析师",
        "lens": "macro",
        "system_prompt": """You are a MACRO ECONOMIST. You analyze global macroeconomic conditions.
You receive: interest rates (Fed funds, 10Y/2Y yields, yield curve), inflation trends, 
employment data, global market performance, VIX/fear-greed, and Fed policy expectations.

CONFIDENCE RULE: Only give high confidence (0.7+) when macro data is CLEAR and CONFIRMED.
Mixed signals = LOW confidence (0.3-0.5).

Decide based on MACRO data:
- Yield curve inverted (10Y-2Y < 0) → BEARISH for stocks, prefer HOLD/SELL
- Fed in HIKING cycle → BEARISH for growth stocks, prefer value/defensive
- Fed in CUTTING cycle → BULLISH for stocks in 3-6 months (lag effect)
- VIX > 30 (EXTREME_FEAR) → risk-off, prefer HOLD with high cash
- VIX < 13 (EXTREME_GREED) → warning sign, reduce long exposure
- Rising yields + falling stocks = classic risk-off → SELL/HOLD
- Falling yields + strong global markets = risk-on → BUY
- Recession risk HIGH → prefer defensive (healthcare, consumer staples)
- USD strengthening → headwind for commodities, emerging markets
- Global markets mostly UP → confirms bullish macro view
- Inflation rising → prefer real assets, SELL long duration bonds
- Gold rallying + USD falling → risk rotation signal
- Yield curve steepening from inversion → early recovery signal, cautiously BULLISH

Output JSON: {"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reason": "...", "risk_flag": "..."}
""" + GROUNDING_RULES,
    },
}


# === 快照数据过滤 ===
_LENS_FILTER_KEYS = {
    "value": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "pe_ratio", "pb_ratio", "ps_ratio", "dividend_yield",
            "intrinsic_value", "market_cap", "book_value_per_share",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "valuation ratios, intrinsic value estimates, book value",
    },
    "momentum": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "price", "ma20", "ma50", "ma200", "macd", "macd_signal",
            "rsi_14", "volume", "avg_volume", "momentum_1m", "momentum_3m",
            "momentum_6m", "momentum_12m", "trend_direction", "trend_strength",
            "factor_rankings", "symbol", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "price, MAs, MACD, RSI, volume, momentum scores, trend",
    },
    "quality": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "roe", "roa", "profit_margin", "operating_margin",
            "debt_to_equity", "free_cash_flow", "earnings_growth",
            "revenue_growth", "current_ratio", "quick_ratio",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "ROE, margins, debt/equity, FCF, earnings stability",
    },
    "contrarian": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "rsi_14", "bollinger_pct_b", "bollinger_upper", "bollinger_lower",
            "volume", "avg_volume", "volume_ratio", "vix",
            "sentiment_score", "short_interest_ratio", "put_call_ratio",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "RSI extremes, Bollinger Bands, volume spikes, VIX, sentiment",
    },
    "risk": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "positions", "portfolio_value", "cash", "cash_pct",
            "max_single_pct", "min_cash", "var_95", "cvar_95",
            "portfolio_beta", "max_drawdown", "correlation_matrix",
            "sector_exposure", "market_regime", "volatility_regime",
            "constraints", "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "position sizes, VaR, correlation, cash, constraints",
    },
    "macro": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "macro_data", "macro_narrative",
            "market_regime", "positions", "portfolio_value",
            "symbol", "price", "sector",
        ],
        "description": "interest rates, yield curve, global markets, VIX, inflation, Fed policy",
    },
}


# === 单标的辩论（按分析师生） ===
def debate(symbol: str, snapshot: dict) -> dict:
    """Full 5-analyst debate for a single symbol.
    
    Each analyst gets only their lens-filtered data subset.
    Returns consolidated decision with analyst opinions and debate summary.
    """
    if not API_KEY:
        return _debate_fallback(symbol, snapshot)
    
    # Prepare snapshot with focus on this symbol
    focus_snap = _prepare_symbol_snapshot(symbol, snapshot)
    
    opinions = {}
    votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
    weighted_votes = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
    total_confidence = 0
    reasons = []
    risk_flags = []
    
    for key, cfg in ANALYSTS.items():
        if key == "macro_economist":
            continue  # macro is handled separately by CIO
        filtered = _filter_snapshot_by_lens(focus_snap, cfg["lens"])
        result = _call_analyst_with_filtered_data(cfg, filtered)
        opinions[key] = result
        action = result.get("action", "HOLD")
        conf = float(result.get("confidence", 0.3))
        conf = max(0.1, min(0.95, conf))
        votes[action] = votes.get(action, 0) + 1
        weighted_votes[action] = weighted_votes.get(action, 0) + conf
        total_confidence += conf
        reasons.append(f"{cfg['name']}: {result.get('reason', '')[:60]}")
        risk_flags.append(result.get("risk_flag", ""))
    
    # Weighted vote tally
    if total_confidence > 0:
        for k in weighted_votes:
            weighted_votes[k] = round(weighted_votes[k] / total_confidence, 2)
    final_action = max(weighted_votes, key=weighted_votes.get)
    final_conf = round(weighted_votes[final_action], 2)
    
    return {
        "symbol": symbol,
        "final_action": final_action,
        "final_confidence": final_conf,
        "analyst_opinions": opinions,
        "debate_summary": "; ".join(reasons[:3]),
        "risk_flags": list(set(risk_flags)),
    }


def batch_debate(symbols: list, snapshot: dict, max_symbols: int = 4) -> list:
    """Debate multiple candidate symbols.
    
    For each symbol, runs the full 5-analyst debate.
    Returns list of debate results sorted by final_confidence descending.
    """
    symbols = symbols[:max_symbols]
    results = []
    for sym in symbols:
        try:
            result = debate(sym, snapshot)
            results.append(result)
        except Exception as e:
            logger.error(f"Debate failed for {sym}: {e}")
            results.append({
                "symbol": sym,
                "final_action": "HOLD",
                "final_confidence": 0.3,
                "analyst_opinions": {},
                "debate_summary": f"Error: {e}",
                "risk_flags": [],
            })
    # Sort by confidence descending
    results.sort(key=lambda r: r.get("final_confidence", 0), reverse=True)
    return results


def _prepare_symbol_snapshot(symbol: str, snapshot: dict) -> dict:
    """Build a snapshot focused on a single symbol for debate."""
    focus = dict(snapshot)
    focus["focus_symbol"] = symbol
    focus["analyst_role"] = "debate_analyst"
    focus["analyst_lens"] = "all"
    return focus


def _call_analyst_with_filtered_data(cfg: dict, filtered_snapshot: dict) -> dict:
    """Call a single analyst with their lens-filtered data.
    
    Uses API if configured, otherwise falls back to rule-based.
    """
    if not API_KEY:
        return _analyst_rule_based(cfg, filtered_snapshot)
    
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": json.dumps(filtered_snapshot, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=45)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        # Clamp confidence
        conf = float(result.get("confidence", 0.5))
        result["confidence"] = max(0.1, min(0.95, conf))
        return result
    except Exception as e:
        logger.error(f"{cfg['name']} 调用失败: {e}")
        return _analyst_rule_based(cfg, filtered_snapshot)


def _analyst_rule_based(cfg: dict, filtered_snapshot: dict) -> dict:
    """Rule-based analyst decision when API is unavailable."""
    lens = cfg.get("lens", "")
    symbol = filtered_snapshot.get("focus_symbol", "")
    price = filtered_snapshot.get("price", 0)
    
    # Simple rule-based logic per lens
    if lens == "value":
        pe = filtered_snapshot.get("pe_ratio", 0)
        if pe and pe < 15:
            return {"action": "BUY", "confidence": 0.45, "reason": f"Low P/E ({pe})", "risk_flag": "LOW"}
        elif pe and pe > 30:
            return {"action": "SELL", "confidence": 0.35, "reason": f"High P/E ({pe})", "risk_flag": "MEDIUM"}
        return {"action": "HOLD", "confidence": 0.3, "reason": "No strong valuation signal", "risk_flag": "LOW"}
    
    elif lens == "momentum":
        rsi = filtered_snapshot.get("rsi_14", 50)
        if rsi and rsi < 30:
            return {"action": "BUY", "confidence": 0.4, "reason": f"RSI oversold ({rsi})", "risk_flag": "LOW"}
        elif rsi and rsi > 75:
            return {"action": "SELL", "confidence": 0.35, "reason": f"RSI overbought ({rsi})", "risk_flag": "MEDIUM"}
        return {"action": "HOLD", "confidence": 0.3, "reason": "Momentum neutral", "risk_flag": "LOW"}
    
    elif lens == "quality":
        roe = filtered_snapshot.get("roe", 0)
        if roe and roe > 0.2:
            return {"action": "BUY", "confidence": 0.4, "reason": f"Strong ROE ({roe:.1%})", "risk_flag": "LOW"}
        return {"action": "HOLD", "confidence": 0.3, "reason": "Quality metrics neutral", "risk_flag": "LOW"}
    
    elif lens == "contrarian":
        rsi = filtered_snapshot.get("rsi_14", 50)
        if rsi and rsi < 30:
            return {"action": "BUY", "confidence": 0.5, "reason": f"RSI extreme low ({rsi})", "risk_flag": "LOW"}
        elif rsi and rsi > 80:
            return {"action": "SELL", "confidence": 0.45, "reason": f"RSI extreme high ({rsi})", "risk_flag": "MEDIUM"}
        return {"action": "HOLD", "confidence": 0.25, "reason": "No extreme readings", "risk_flag": "LOW"}
    
    elif lens == "risk":
        pos_pct = filtered_snapshot.get("position_pct", 0)
        if pos_pct and pos_pct > 0.15:
            return {"action": "SELL", "confidence": 0.5, "reason": f"Overweight ({pos_pct:.1%})", "risk_flag": "HIGH"}
        return {"action": "HOLD", "confidence": 0.3, "reason": "Within risk limits", "risk_flag": "LOW"}
    
    return {"action": "HOLD", "confidence": 0.2, "reason": "No data for analysis", "risk_flag": "UNKNOWN"}


def _debate_fallback(symbol: str, snapshot: dict) -> dict:
    """Rule-based fallback when API key is missing."""
    return {
        "symbol": symbol,
        "final_action": "HOLD",
        "final_confidence": 0.3,
        "analyst_opinions": {
            "value_investor": {"action": "HOLD", "confidence": 0.3, "reason": "API unavailable, rule-based hold"},
            "momentum_trader": {"action": "HOLD", "confidence": 0.3, "reason": "API unavailable, rule-based hold"},
            "quality_seeker": {"action": "HOLD", "confidence": 0.3, "reason": "API unavailable, rule-based hold"},
            "contrarian": {"action": "HOLD", "confidence": 0.3, "reason": "API unavailable, rule-based hold"},
            "risk_manager": {"action": "HOLD", "confidence": 0.3, "reason": "API unavailable, rule-based hold"},
        },
        "debate_summary": "API unavailable, all analysts default to HOLD",
        "risk_flags": ["UNKNOWN"],
    }


# === CIO 全投资组合审查 ===
CIO_PROMPT = """You are the CHIEF INVESTMENT OFFICER. You review the ENTIRE portfolio + market.

Your job is to analyze the portfolio holistically and output:
1. position_reviews: HOLD/ADD/CUT per position
2. new_position_candidates: symbols to consider buying
3. portfolio_actions: specific actions to take

Output STRICT JSON with keys: market_read, risk_notes, position_reviews, new_position_candidates, portfolio_actions
""" + GROUNDING_RULES


def cio_review(snapshot: dict) -> dict:
    """CIO analyzes the ENTIRE portfolio + market in ONE call.
    
    Falls back to rule-based if API unavailable.
    """
    if not API_KEY:
        return _cio_fallback(snapshot)
    
    try:
        cio_input = _build_cio_input(snapshot)
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": CIO_PROMPT},
                {"role": "user", "content": json.dumps(cio_input, ensure_ascii=False)},
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        result["_cio_source"] = "api"
        logger.info(f"CIO review complete: {len(result.get('position_reviews', []))} positions")
        return result
    except Exception as e:
        logger.error(f"CIO review failed: {e}")
        return _cio_fallback(snapshot)


def _build_cio_input(snapshot: dict) -> dict:
    """Build CIO input from snapshot."""
    return {
        "market_regime": snapshot.get("market_regime", "UNKNOWN"),
        "positions": [
            {"symbol": p.get("symbol", ""), "position_pct": p.get("position_pct", 0),
             "pnl_pct": p.get("pnl_pct", 0), "sector": p.get("sector", "")}
            for p in snapshot.get("positions", [])
        ],
        "cash_pct": snapshot.get("constraints", {}).get("current_cash_pct", 0),
        "factor_rankings": snapshot.get("factor_rankings", [])[:10],
    }


def _cio_fallback(snapshot: dict) -> dict:
    """Rule-based CIO fallback."""
    positions = snapshot.get("positions", [])
    position_reviews = []
    for p in positions:
        pnl = p.get("pnl_pct", 0)
        pos_pct = p.get("position_pct", 0)
        if pnl < -0.10 and pos_pct > 0.05:
            action = "CUT"; conf = 0.6
            reason = f"Loss {pnl:.1%} at {pos_pct:.1%} weight"
        elif pos_pct > 0.15:
            action = "CUT"; conf = 0.5
            reason = f"Overweight {pos_pct:.1%}"
        else:
            action = "HOLD"; conf = 0.4
            reason = "No clear signal"
        position_reviews.append({"action": action, "symbol": p.get("symbol", ""),
                                  "confidence": conf, "reason": reason})
    return {
        "market_read": "fallback: CIO API unavailable",
        "risk_notes": "Conservative fallback mode",
        "position_reviews": position_reviews,
        "new_position_candidates": [],
        "portfolio_actions": [],
        "_cio_source": "fallback",
    }


# === 持仓复核 ===
POSITION_REVIEW_PROMPT = """You are a PORTFOLIO ANALYST reviewing existing positions.
For each position, decide HOLD, ADD, or CUT based on P&L, position size, and factor score.
Output JSON: {"position": "AAPL", "action": "HOLD|ADD|CUT", "confidence": 0.0-1.0, "reason": "..."}
""" + GROUNDING_RULES


def position_review(snapshot: dict) -> list:
    """Review ALL existing positions: HOLD / ADD / CUT."""
    positions = snapshot.get("positions", [])
    if not positions:
        return []
    if not API_KEY:
        return _position_review_rule_based(snapshot)
    
    factor_rankings = snapshot.get("factor_rankings", [])
    ranking_map = {r["symbol"]: r.get("score", 0) for r in factor_rankings}
    results = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        try:
            pos_input = {
                "position": symbol, "position_pct": pos.get("position_pct", 0),
                "pnl_pct": pos.get("pnl_pct", 0),
                "factor_score": ranking_map.get(symbol, 0.5),
            }
            payload = {
                "model": MODEL, "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": POSITION_REVIEW_PROMPT},
                    {"role": "user", "content": json.dumps(pos_input, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            }
            headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=45)
            resp.raise_for_status()
            review = json.loads(resp.json()["choices"][0]["message"]["content"])
            conf = float(review.get("confidence", 0.5))
            review["confidence"] = max(0.1, min(0.95, conf))
            results.append(review)
        except Exception as e:
            logger.error(f"Position review {symbol} failed: {e}")
            results.append({"position": symbol, "action": "HOLD", "confidence": 0.3, "reason": f"API error: {e}"})
    return results


def _position_review_rule_based(snapshot: dict) -> list:
    """Rule-based position review."""
    positions = snapshot.get("positions", [])
    factor_rankings = snapshot.get("factor_rankings", [])
    ranking_map = {r["symbol"]: r.get("score", 0) for r in factor_rankings}
    results = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        pnl = pos.get("pnl_pct", 0)
        pos_pct = pos.get("position_pct", 0)
        score = ranking_map.get(symbol, 0.5)
        if pnl < -0.10 and pos_pct > 0.05:
            results.append({"position": symbol, "action": "CUT", "confidence": 0.6, "reason": f"Loss {pnl:.1%}"})
        elif pos_pct > 0.15:
            results.append({"position": symbol, "action": "CUT", "confidence": 0.5, "reason": f"Overweight {pos_pct:.1%}"})
        elif score > 0.65:
            results.append({"position": symbol, "action": "ADD", "confidence": 0.4, "reason": f"Factor score {score:.2f}"})
        else:
            results.append({"position": symbol, "action": "HOLD", "confidence": 0.4, "reason": "No clear signal"})
    return results


# === 快照数据过滤 ===
_LENS_FILTER_KEYS = {
    "value": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "pe_ratio", "pb_ratio", "ps_ratio", "dividend_yield",
            "intrinsic_value", "market_cap", "book_value_per_share",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "valuation ratios, intrinsic value estimates, book value",
    },
    "momentum": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "price", "ma20", "ma50", "ma200", "macd", "macd_signal",
            "rsi_14", "volume", "avg_volume", "momentum_1m", "momentum_3m",
            "momentum_6m", "momentum_12m", "trend_direction", "trend_strength",
            "factor_rankings", "symbol", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "price, MAs, MACD, RSI, volume, momentum scores, trend",
    },
    "quality": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "roe", "roa", "profit_margin", "operating_margin",
            "debt_to_equity", "free_cash_flow", "earnings_growth",
            "revenue_growth", "current_ratio", "quick_ratio",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "ROE, margins, debt/equity, FCF, earnings stability",
    },
    "contrarian": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "rsi_14", "bollinger_pct_b", "bollinger_upper", "bollinger_lower",
            "volume", "avg_volume", "volume_ratio", "vix",
            "sentiment_score", "short_interest_ratio", "put_call_ratio",
            "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "RSI extremes, Bollinger Bands, volume spikes, VIX, sentiment",
    },
    "risk": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "positions", "portfolio_value", "cash", "cash_pct",
            "max_single_pct", "min_cash", "var_95", "cvar_95",
            "portfolio_beta", "max_drawdown", "correlation_matrix",
            "sector_exposure", "market_regime", "volatility_regime",
            "constraints", "factor_rankings", "symbol", "price", "sector",
            "macro_data", "macro_narrative",
        ],
        "description": "position sizes, VaR, correlation, cash, constraints",
    },
    "macro": {
        "keep": [
            "focus_symbol", "analyst_role", "analyst_lens",
            "macro_data", "macro_narrative",
            "market_regime", "positions", "portfolio_value",
            "symbol", "price", "sector",
        ],
        "description": "interest rates, yield curve, global markets, VIX, inflation, Fed policy",
    },
}


def _filter_snapshot_by_lens(snapshot: dict, lens: str,
                              include_meta: bool = True) -> dict:
    """Filter snapshot to only include data relevant to the analyst's lens."""
    filter_def = _LENS_FILTER_KEYS.get(lens, {})
    keep_keys = set(filter_def.get("keep", []))

    if not keep_keys:
        return snapshot

    filtered = {}
    if include_meta:
        filtered["analyst_role"] = snapshot.get("analyst_role", "")
        filtered["analyst_lens"] = lens
        filtered["focus_symbol"] = snapshot.get("focus_symbol", "")
        filtered["data_subset"] = filter_def.get("description", lens)

    for key in keep_keys:
        if key in snapshot:
            filtered[key] = snapshot[key]

    factor_rankings = snapshot.get("factor_rankings", [])
    if factor_rankings and "factor_rankings" in keep_keys:
        filtered["factor_rankings"] = factor_rankings

    logger.debug(f"  Filtered snapshot for lens={lens}: {len(filtered)} keys "
                 f"(subset: {filter_def.get('description', 'all')[:40]})")
    return filtered
