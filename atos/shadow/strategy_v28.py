"""
ATOS PRO — v29 QQQ Core + Alpha 策略模块 (Phase 5 框架重塑)
==============================================================
从 shadow_trader.py 抽离 (原 1361-1560 行)。策略参数与逻辑 **逐字保留**:
  60% QQQ 核心 + 40% 动量股(7只), 21日动量×0.6 + 距20日高点×0.4,
  季度(63天)再平衡, 个股止损5%/移动止损8%, QQQ移动止损12%。
  回测v7: 年化30.7% vs SPY 15.2%。

架构修复 (报告 §7.3.1): "策略隔离" 原本靠散落 6+ 处循环的
``if sym in ("QQQ",) or sym in V28_ALPHA_UNIVERSE: continue`` if-flag
(Pattern 87/90/96/97 反复证明"漏一个循环就杀持仓")。
现收敛为 :func:`is_v28_position` 单一判定函数, 所有隔离点统一调用。
"""

import datetime

from atos.core.logging import get_logger
from atos.core.position_schema import get_qty

logger = get_logger("shadow_trader")

# ============================================================
# v29: QQQ Core + Alpha 策略 (优化: 7只 + 动量权重0.6)
# 回测v7: 60% QQQ + 40% 动量股(7只), 年化30.7%, 跑赢SPY 15.6%
# ============================================================
V28_ALPHA_UNIVERSE = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "AMD",
    "CRM", "NFLX", "PLTR", "MU", "TSLA",
]
V28_CORE_SYMBOL = "QQQ"  # v28 核心 ETF 标的 (单一真源, risk_gate/risk_manager 引用)
V28_CORE_PCT = 0.60      # QQQ 核心仓位比例
V28_ALPHA_COUNT = 7       # alpha 个股数量 (v29: 5→7)
V28_REBALANCE_DAYS = 63   # 每季度再平衡
V28_STOP_LOSS = 0.05      # 个股止损 5%
V28_TRAILING_STOP = 0.08  # 移动止损 8%
V28_QQQ_TRAILING = 0.12   # QQQ 移动止损 12%


def is_v28_position(sym: str) -> bool:
    """v28 策略持仓判定 — 唯一权威入口 (替代散落的 if-flag 隔离)。

    v28 持仓 = QQQ 核心仓 + alpha 动量池。旧卖出规则 (止损/分批止盈/
    剥头皮/Flat清理/动量退出/集中度熔断/Triple-Barrier) 一律跳过这些标的。
    """
    return sym == V28_CORE_SYMBOL or sym in V28_ALPHA_UNIVERSE


def _v28_qqq_core_alpha(account, signals, regime, spy_trend):
    """v29 策略: QQQ 核心 + 动量个股 alpha (优化: 7只 + 动量权重0.6)

    规则:
    1. 60% 资金买 QQQ（始终持有，不择时）
    2. 40% 资金买 7 只最强动量股 (21日动量 + 距20日高点)
    3. 每季度再平衡
    4. 个股止损 5%, 移动止损 8%
    5. QQQ 移动止损 12%
    """
    # P0-3: 读取风险敞口缩放系数（安全层×宏观门控合并值），clamp 到 [0,1]
    scale = max(0.0, min(1.0, getattr(account, '_risk_exposure_scale', 1.0)))

    equity = account.total_equity
    cash = account.cash

    # ── 卖出检查 ──
    for sym in list(account.positions.keys()):
        pos = account.positions[sym]
        qty = get_qty(pos)
        if qty <= 0:
            continue
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0:
            continue

        price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
        if price <= 0:
            continue

        pnl_pct = (price - avg_price) / avg_price

        # 更新峰值
        peak = pos.get("peak_price", avg_price)
        if price > peak:
            pos["peak_price"] = price
            peak = price

        sell_reason = None

        if sym == "QQQ":
            # QQQ: 移动止损 12%
            if peak > avg_price * 1.05:
                ts_drop = (peak - price) / peak
                if ts_drop >= V28_QQQ_TRAILING:
                    sell_reason = f"QQQ移动止损{ts_drop:.1%}"
        else:
            # 个股: 止损 5%
            if pnl_pct <= -V28_STOP_LOSS:
                sell_reason = f"止损{pnl_pct:.1%}"
            # 移动止损 8%
            elif peak > avg_price * 1.03:
                ts_drop = (peak - price) / peak
                if ts_drop >= V28_TRAILING_STOP:
                    sell_reason = f"移动止损{ts_drop:.1%}"

        if sell_reason:
            account.execute(sym, "SELL", qty, price, reason=sell_reason)
            logger.info(f"🔴 v28卖出 {sym}: {sell_reason} PnL={pnl_pct:.1%}")

    # ── 再平衡检查 ──
    last_rebal = getattr(account, '_v28_last_rebalance', None)
    now = datetime.datetime.now()
    days_since = (now - last_rebal).days if last_rebal else 999

    # v28c: 如果 QQQ 配比远低于目标，每天都再平衡直到到位
    qqq_pos = account.positions.get("QQQ", {})
    qqq_qty = get_qty(qqq_pos)
    qqq_px = signals.get("QQQ", {}).get("price", 0)
    qqq_val = qqq_qty * qqq_px if qqq_px > 0 else 0
    qqq_pct = qqq_val / equity if equity > 0 else 0

    if qqq_pct < V28_CORE_PCT * 0.80:
        should_rebalance = True  # QQQ 严重不足，立即再平衡
        if days_since > 0:
            logger.info(f"📊 v28 QQQ配比{qqq_pct:.0%} << 目标{V28_CORE_PCT:.0%} — 加速再平衡")
    else:
        should_rebalance = days_since >= V28_REBALANCE_DAYS

    if not should_rebalance:
        return

    logger.info(f"📊 v28 季度再平衡 | Equity=${equity:,.0f}")

    # ── 核心仓: QQQ ──
    target_qqq_value = equity * V28_CORE_PCT * scale
    qqq_price = signals.get("QQQ", {}).get("price", 0)

    if qqq_price > 0:
        current_qqq = account.positions.get("QQQ", {})
        current_qqq_qty = get_qty(current_qqq)
        current_qqq_value = current_qqq_qty * qqq_price

        if current_qqq_value < target_qqq_value * 0.90:
            # 需要加仓 QQQ — 允许多批次买入直到达到目标
            buy_value = target_qqq_value - current_qqq_value
            # 刷新现金（可能刚卖了其他持仓）
            cash = account.cash
            max_affordable = int(cash * 0.98 / qqq_price)
            buy_qty = max(1, min(int(buy_value / qqq_price), max_affordable))
            if buy_qty > 0 and buy_qty * qqq_price < cash * 0.98:
                ok = account.execute("QQQ", "BUY", buy_qty, qqq_price,
                              reason=f"v28核心仓 目标${target_qqq_value:,.0f}")
                if ok:
                    logger.info(f"🟢 v28买入 QQQ: {buy_qty}股 @${qqq_price:.2f} (现有{current_qqq_qty}股)")
                else:
                    logger.warning(f"⚠️ v28 QQQ买入被拒绝: {buy_qty}股 @${qqq_price:.2f} — 检查单仓/总仓上限")
        elif current_qqq_value > target_qqq_value * 1.10:
            # H10: 对称减持 — QQQ 超配 >10% 时卖回目标仓位
            sell_value = current_qqq_value - target_qqq_value
            sell_qty = int(sell_value / qqq_price)
            if sell_qty > 0:
                sell_qty = min(sell_qty, current_qqq_qty)
                ok = account.execute("QQQ", "SELL", sell_qty, qqq_price,
                              reason=f"v28核心仓减持 目标${target_qqq_value:,.0f}")
                if ok:
                    logger.info(f"🟢 v28减持 QQQ: {sell_qty}股 @${qqq_price:.2f} (现有{current_qqq_qty}股)")
                else:
                    logger.warning(f"⚠️ v28 QQQ减持被拒绝: {sell_qty}股 @${qqq_price:.2f}")

    # ── Alpha 仓: 动量股 ──
    target_alpha_value = equity * (1 - V28_CORE_PCT) * scale
    per_stock_value = target_alpha_value / V28_ALPHA_COUNT

    # 计算动量分 (v28i: 行业动量 — 1日变动 + 距20日高点距离)
    alpha_candidates = []
    for sym in V28_ALPHA_UNIVERSE:
        sig = signals.get(sym, {})
        price = sig.get("price", 0)
        if price <= 0:
            continue

        # 动量指标 (v29: 用真实字段 mom_21 + dist_20d_high，修复 phantom-field bug)
        mom_21 = sig.get("mom_21", 0) or 0        # 21日动量 (%)
        dist_high = sig.get("dist_20d_high", -10) or -10  # 距20日高点 (%)
        ma50 = sig.get("ma50", 0)
        rsi = sig.get("rsi", 50)

        # v29: 行业动量评分 = 60% 21日动量 + 40% 趋势强度(距高点)
        # 回测v7: 0.6/0.4 权重最优 (30.7% vs 27.4% baseline)
        trend_score = max(0, 1 + dist_high / 20)  # -20→0, 0→1
        mom_score = max(0, min(1, (mom_21 + 5) / 10))  # -5%→0, +5%→1
        score = mom_score * 0.6 + trend_score * 0.4

        # 过滤
        if rsi > 78:  # 超买
            continue
        if ma50 > 0 and price < ma50 * 0.92:  # 远低于MA50
            continue

        alpha_candidates.append((sym, score, price))

    # 排序选 top N
    alpha_candidates.sort(key=lambda x: -x[1])

    # 当前 alpha 持仓
    current_alpha = [s for s in account.positions if s != "QQQ"]

    # 卖出不在 top N 的持仓
    top_syms = {c[0] for c in alpha_candidates[:V28_ALPHA_COUNT]}
    for sym in current_alpha:
        if sym not in top_syms:
            pos = account.positions[sym]
            qty = get_qty(pos)
            price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
            if qty > 0 and price > 0:
                account.execute(sym, "SELL", qty, price,
                              reason=f"v28再平衡换仓")
                logger.info(f"🔄 v28换仓卖出 {sym}")

    # 买入新候选
    cash = account.cash  # 刷新
    for sym, score, price in alpha_candidates[:V28_ALPHA_COUNT]:
        if sym in account.positions:
            continue  # 已持有
        qty = max(1, int(per_stock_value / price))
        if qty * price < cash * 0.85:
            ok = account.execute(sym, "BUY", qty, price,
                          reason=f"v28动量alpha score={score:.3f}")
            if ok:
                logger.info(f"🟢 v28买入 {sym}: {qty}股 @${price:.2f} score={score:.3f}")
            else:
                logger.warning(f"⚠️ v28买入被拒绝: {sym} {qty}股 @${price:.2f} — 检查单仓/总仓/现金/冷却上限")

    account._v28_last_rebalance = now
    logger.info(f"✅ v28再平衡完成 | 持仓: {len(account.positions)}只")
