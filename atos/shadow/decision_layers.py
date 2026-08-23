"""
ATOS PRO — 决策层 (Phase 5 框架重塑)
========================================
从 shadow_trader.py run_shadow_cycle 抽离的 sections 5/5b/5c:
  - compute_quality_gate():  v27 趋势自适应质量门控 (替代低胜率 AI 辩论)
  - fetch_intel_briefing():  实时情报简报 (每6周期≈30分钟刷新)
  - run_ai_counsel():        v6 增强AI决策 (每8周期≈40分钟, 硬规则+信心评分)

全部阈值与逻辑逐字保留。函数属性节流器迁移到 CycleState 单例。
"""

from typing import Optional

from atos.core.logging import get_logger
from atos.shadow.cycle_state import CycleState

logger = get_logger("shadow_trader")


def compute_quality_gate(top_picks, signals, spy_trend: str) -> dict:
    """智能质量门控（替代低胜率 AI 辩论：基于因子质量+动量+RSI）。

    🆕 每周期运行（不再跳周期），严格过滤低质量信号。
    返回 ai_veto_map: {sym: True=否决, False=通过}
    """
    ai_veto_map = {}
    try:
        for pick in (top_picks or [])[:8]:
            sym = pick["symbol"]
            sig = signals.get(sym, {})
            bd = pick.get("breakdown", {})
            factor_score = pick.get("score", 0)

            # v27: 趋势自适应质量门控 — 与入场过滤对齐
            quality_factors = sum(1 for k in ["value","momentum","quality","technical"] if bd.get(k, 0) > 0.2)
            if spy_trend == "BULL":
                macd_ok = sig.get("macd_hist", 0) > -3.0
                rsi_ok = 25 < sig.get("rsi", 50) < 78
            elif spy_trend == "CAUTIOUS":
                macd_ok = sig.get("macd_hist", 0) > -1.5
                rsi_ok = 30 < sig.get("rsi", 50) < 72
            else:
                macd_ok = sig.get("macd_hist", 0) > 0.001
                rsi_ok = 35 < sig.get("rsi", 50) < 68
            trend_ok = sig.get("trend", "") in ("UP", "WEAK_UP")

            quality_score = (
                quality_factors * 20 +
                (10 if macd_ok else 0) +
                (10 if trend_ok else 0) +
                (5 if rsi_ok else 0) -
                (30 if factor_score < 0.30 else 0)
            )

            veto_threshold = 25 if spy_trend == "BULL" else (35 if spy_trend == "CAUTIOUS" else 50)
            if quality_score < veto_threshold:
                ai_veto_map[sym] = True
                logger.info(f"🚫 否决 {sym}: Q={quality_score} (因子{quality_factors}/4 macd={macd_ok} trend={trend_ok} rsi={rsi_ok}) [{spy_trend}]")
            else:
                ai_veto_map[sym] = False
        vetoed_count = sum(1 for v in ai_veto_map.values() if v)
        logger.info(f"🎯 质量门控({spy_trend}): {len(ai_veto_map)}候选中 {vetoed_count}否决 {len(ai_veto_map)-vetoed_count}通过")
    except Exception as e:
        logger.warning(f"质量门控跳过: {e}")
    return ai_veto_map


def fetch_intel_briefing(top_picks, signals, account_cycle_count: int) -> Optional[dict]:
    """实时情报简报（每周期尝试，内部按 INTEL_INTERVAL 节流）。

    Returns: briefing dict 或 None (跳过/未到期/异常)
    """
    intel_briefing = None
    try:
        from atos.intel.briefing import get_pre_trade_briefing, briefing_to_prompt
        INTEL_INTERVAL = 6  # 每6周期（30分钟）刷新一次情报
        state = CycleState.get()
        if account_cycle_count - state.last_intel_cycle >= INTEL_INTERVAL:
            watchlist = [p["symbol"] for p in top_picks[:8]] if top_picks else \
                        list(signals.keys())[:10]
            intel_briefing = get_pre_trade_briefing(symbols=watchlist, max_news=12)
            state.last_intel_cycle = account_cycle_count
            # 记录情报摘要到日志
            sentiment = intel_briefing.get("market_sentiment", {})
            flags = intel_briefing.get("risk_flags", [])
            logger.info(f"📡 情报简报: 情绪={sentiment.get('bias','?')} "
                       f"新闻={len(intel_briefing.get('top_news',[]))}条 "
                       f"风险={len(flags)}个")
    except Exception as e:
        logger.debug(f"情报简报跳过: {e}")
    return intel_briefing


def run_ai_counsel(account, top_picks, signals, spy_c, current_vix,
                   regime, spy_trend, intel_briefing) -> tuple:
    """v6: 增强AI决策 (每8周期≈40分钟, 轻量快速)。

    替换低胜率(6.4%)的旧AI辩论，使用硬规则+信心评分+情报融合。

    Returns:
        (ai_veto_map, is_market_hours)
        ai_veto_map: None=本周期未运行(调用方保留质量门控结果);
                     dict=已运行(调用方替换, 与原逻辑一致, 异常时为空dict)
        is_market_hours 可能被 AI 叫停改为 False
    """
    AI_ENHANCED_INTERVAL = 8
    is_market_hours = True

    if account.cycle_count % AI_ENHANCED_INTERVAL != 0:
        return None, is_market_hours

    ai_veto_map = {}
    try:
        from atos.ai.advisor_enhanced import get_enhanced_advice

        # Build candidate list from top picks
        ai_candidates = []
        for pick in (top_picks or [])[:8]:
            sym = pick["symbol"]
            sig = signals.get(sym, {})
            ai_candidates.append({
                "symbol": sym,
                "price": sig.get("price", 0),
                "rsi": sig.get("rsi", 50),
                "trend": sig.get("trend", "NEUTRAL"),
                "factor_score": pick.get("score", 0),
                "macd_hist": sig.get("macd_hist", 0),
                "volume_ratio": sig.get("volume_ratio", 1.0),
                "ma50": sig.get("ma50", 0),
                "bollinger": sig.get("bollinger", {}),
            })

        ai_snapshot = {
            "market": {
                "spy_price": spy_c[-1] if spy_c else 745,
                "vix": round(current_vix, 1),
                "regime": regime.get("regime", "UNKNOWN") if isinstance(regime, dict) else "UNKNOWN",
                "spy_trend": spy_trend,
            },
            "total_equity": account.total_equity,
            "cash": account.cash,
            "candidates": ai_candidates,
            "positions": account.position_list,
        }

        ai_enhanced_advice = get_enhanced_advice(ai_snapshot, intel_briefing)

        # Apply decisions
        buy_count = ai_enhanced_advice.get("buy_count", 0)
        skip_count = ai_enhanced_advice.get("skip_count", 0)
        risk_adj = ai_enhanced_advice.get("risk_adjustment", 1.0)
        logger.info(f"🧠 AI v6: {buy_count}买/{skip_count}跳过 | "
                   f"风险系数={risk_adj:.0%} | "
                   f"{ai_enhanced_advice.get('market_read','')}")

        # If AI says no trading, override
        if not ai_enhanced_advice.get("trading_allowed", True):
            logger.warning(f"🚫 AI暂停交易: {ai_enhanced_advice.get('risk_reasons',[])}")
            is_market_hours = False

        # Apply risk adjustment to position sizing
        if risk_adj < 0.5:
            account.max_positions = max(3, account.max_positions // 2)
            logger.info(f"🛡️ AI降低仓位上限至 {account.max_positions}")

        # Build AI decision map for factor-based buying
        ai_decisions = ai_enhanced_advice.get("decisions", [])
        ai_veto_map = {}
        for d in ai_decisions:
            sym = d["symbol"]
            if d["action"] == "SKIP":
                ai_veto_map[sym] = True
            elif d["action"] == "BUY":
                ai_veto_map[sym] = False
        # Only veto SKIPs, WATCH passes through to factor engine
        vetoed_count = sum(1 for v in ai_veto_map.values() if v)
        if vetoed_count > 0:
            logger.info(f"🧠 AI v6 否决: {vetoed_count}/{len(ai_decisions)}只")

    except Exception as e:
        logger.warning(f"AI v6跳过: {e}")
        ai_veto_map = {}

    return ai_veto_map, is_market_hours
