"""
ATOS PRO — IC 反馈环 (Phase 5 框架重塑)
==========================================
从 shadow_trader.py run_shadow_cycle 抽离 (原 782-836 行)。
根据实盘收益评估因子预测能力 (IC = 信息系数), EMA 平滑后驱动方向自适应:
  - IC 持续 < -0.05 → 标记反转 (v27: 不反转选股, 只降仓50%)
  - IC 回正 > 0.02   → 恢复正常

跨周期状态从函数属性迁移到 CycleState 单例 (报告 §7.4 P1 状态对象化)。
"""

from atos.core.logging import get_logger
from atos.shadow.cycle_state import CycleState

logger = get_logger("shadow_trader")


def run_ic_feedback(signals: dict, factor_result: dict, regime: dict) -> None:
    """IC 反馈环 — 逐字迁移。state 从 CycleState 单例读写。"""
    state = CycleState.get()
    try:
        from atos.factors.engine import ic_analysis
        prev_scores = state.prev_scores
        prev_breakdown = state.prev_breakdown

        # 计算本周期实际收益（%）
        if prev_scores and factor_result:
            current_returns = {}
            for sym in prev_scores:
                sig = signals.get(sym, {})
                price_now = sig.get("price", 0)
                prev_price = state.prev_prices.get(sym, 0)
                if price_now > 0 and prev_price > 0:
                    current_returns[sym] = (price_now - prev_price) / prev_price

            if len(current_returns) >= 10:
                ic_result = ic_analysis(prev_scores, current_returns,
                                        regime["regime"], prev_breakdown)
                # Fix: IC EMA 平滑 — 减少噪音，更稳定判断因子是否有效
                prev_ic_ema = state.ic_ema
                current_ic = ic_result['ic']
                if prev_ic_ema is None:
                    state.ic_ema = current_ic
                else:
                    state.ic_ema = prev_ic_ema * 0.7 + current_ic * 0.3
                smoothed_ic = state.ic_ema
                logger.info(f"[IC反馈] IC={current_ic:.4f} (平滑={smoothed_ic:.4f}) | {ic_result.get('verdict','')} | n={ic_result['n']}")

                # v26: IC方向自适应 — 负IC时反转因子权重
                # IC持续<-0.05说明因子反向，应该反转选股方向
                if smoothed_ic < -0.05:
                    state.ic_inverted = True
                    if not state.ic_invert_logged:
                        logger.warning(f"🔄 IC持续为负({smoothed_ic:.4f}) → 因子方向反转，低分股优先")
                        state.ic_invert_logged = True
                elif smoothed_ic > 0.02:
                    if state.ic_inverted:
                        logger.info(f"🔄 IC回正({smoothed_ic:.4f}) → 恢复正常选股方向")
                    state.ic_inverted = False
                    state.ic_invert_logged = False

        # 存储本周期分数和价格，供下周期使用
        state.prev_scores = factor_result.get("scores", {}) if factor_result else {}
        state.prev_breakdown = factor_result.get("breakdown", {}) if factor_result else {}
        state.prev_prices = {
            sym: sig.get("price", 0)
            for sym, sig in signals.items() if sig.get("price", 0) > 0
        }
        state.prev_rsi = {
            sym: sig.get("rsi", 50)
            for sym, sig in signals.items()
        }
    except Exception as ic_err:
        logger.debug(f"IC反馈环跳过: {ic_err}")
