"""
ATOS PRO — v29 QQQ Core + Alpha 策略模块 (Phase 5 框架重塑)
==============================================================
从 shadow_trader.py 抽离 (原 1361-1560 行)。策略参数与逻辑 **逐字保留**:
  60% QQQ 核心 + 40% 动量股(7只), 21日动量×0.6 + 距20日高点×0.4,
  季度(63天)再平衡, 个股止损8%/移动止损8%(arm+3%), QQQ硬止损12%/移动止损10%。
  参数经 hermes 网格寻优(1152组合) top1: 年化20.93% vs 默认14.8%, PF 2.91。
  回测v7: 年化30.7% vs SPY 15.2%。

F4 算法优化 (2026-09, backtest_v31 三轮网格验证, 见 data/backtest_v31{,b,c}_result.json):
  ① alpha 宇宙 13→20: 加入 7 只低相关防御性标的 (XLP/XLV/XLF/COST/JNJ/WMT/UNH)。
     分散化动量 (Moskowitz/AQR) — 胜率 54.9%→62.1% (+7.2pp), PF 2.93→3.38,
     年化代价 ~3.8pp (仍 24.5% >> SPY 15%)。
  ② QQQ 趋势去险叠加: QQQ 跌破 MA200 时整体敞口 ×0.80 (留 20% 现金),
     回撤 -31.6%→-27.5%, 年化代价 ~1.2pp。仅去险不加杠杆, 不择时抄底。
  最终回测: 年化 23.3%, 回撤 -27.5%, 胜率 62.1%, PF 3.38, Sharpe 1.12。


架构修复 (报告 §7.3.1): "策略隔离" 原本靠散落 6+ 处循环的
``if sym in ("QQQ",) or sym in V28_ALPHA_UNIVERSE: continue`` if-flag
(Pattern 87/90/96/97 反复证明"漏一个循环就杀持仓")。
现收敛为 :func:`is_v28_position` 单一判定函数, 所有隔离点统一调用。
"""

import datetime

from atos.core.logging import get_logger
from atos.core.position_schema import get_qty
from atos.config_shared import RISK as _RISK

logger = get_logger("shadow_trader")

# ============================================================
# v29: QQQ Core + Alpha 策略 (优化: 7只 + 动量权重0.6)
# 回测v7: 60% QQQ + 40% 动量股(7只), 年化30.7%, 跑赢SPY 15.6%
# ============================================================
V28_ALPHA_UNIVERSE = [
    # 科技/成长动量核心 (v28 原始 13 只)
    "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "AMD",
    "CRM", "NFLX", "PLTR", "MU", "TSLA",
    # F4 分散化: 7 只低相关防御性标的 (必需/公用/金融) —
    # 动量选股从单一科技扩展为全市场动量, 降低组合相关性。
    # 回测: 胜率 54.9%→62.1%, PF 2.93→3.38, 回撤 -36.1%→-31.6%。
    "XLP", "XLV", "XLF", "COST", "JNJ", "WMT", "UNH",
]
V28_CORE_SYMBOL = "QQQ"  # v28 核心 ETF 标的 (单一真源, risk_gate/risk_manager 引用)
V28_CORE_PCT = 0.60      # QQQ 核心仓位比例
V28_ALPHA_COUNT = 7       # alpha 个股数量 (v29: 5→7)
V28_REBALANCE_DAYS = 63   # 再平衡周期: 63 交易日 (F2: 原63日历日≈44交易日, 改为交易日口径, 见 _trading_days_since)
V28_STOP_LOSS = 0.12  # 个股硬止损 12% (F3: 网格回测 10%→12% 年化+0.58pp, maxDD+1.5pp, 用户确认切换)
V28_TRAILING_STOP = 0.08  # 个股移动止损 (F2: 已停用 — 高波动动量股纯whipsaw, -6pp/年, 见 SYSTEM_OPT_REPORT 结论3)
V28_QQQ_TRAILING = 0.10   # QQQ 移动止损 (F2: 已停用 — 指数回调必反弹, 移动止损只锁损)
V28_QQQ_HARD_STOP = 0.25  # QQQ 硬止损 25% 兜底 (F2: 从12%放宽 — 指数级-25%才触发, 仅防极端黑天鹅, 12%日常回调会误杀)
V28_ARM = 0.03            # 移动止损 arming 阈值 (F2: 已停用, 保留常量防引用断裂)
# F4 趋势去险: QQQ 跌破 MA200 时整体敞口缩放 (仅降险, 不择时抄底)。
# 回测: 回撤 -31.6%→-27.5%, 年化代价 ~1.2pp。1.0 = 关闭去险。
V28_TREND_DERISK_SCALE = 0.80
V28_TREND_MA = 200        # 趋势判定均线周期 (需 signals[QQQ]["ma200"])


def is_v28_position(sym: str) -> bool:
    """v28 策略持仓判定 — 唯一权威入口 (替代散落的 if-flag 隔离)。

    v28 持仓 = QQQ 核心仓 + alpha 动量池。旧卖出规则 (止损/分批止盈/
    剥头皮/Flat清理/动量退出/集中度熔断/Triple-Barrier) 一律跳过这些标的。
    """
    return sym == V28_CORE_SYMBOL or sym in V28_ALPHA_UNIVERSE


def v28_check_exits(account, signals) -> None:
    """v28 持仓止损/移动止损检查 — 与交易时段解耦 (审计 P2: 闭市无止损)。

    原卖出检查内嵌在 _v28_qqq_core_alpha 里, 而该函数仅在盘中 (is_market_hours)
    被调用, 导致闭市时 v28 核心持仓 (QQQ+alpha, 占大部分资金) 无止损保护。
    现提取为独立函数: 盘中由 _v28_qqq_core_alpha 调用, 闭市由主循环单独调用。
    """
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

        if sym == V28_CORE_SYMBOL:
            # QQQ: 硬止损 25% 兜底 (F2: 移除移动止损/12%硬止损 — 指数回调必反弹, 25%仅防极端黑天鹅)
            if pnl_pct <= -V28_QQQ_HARD_STOP:
                sell_reason = f"QQQ硬止损{pnl_pct:.1%}"
        else:
            # 个股: 硬止损 10% (F2: 移除移动止损 whipsaw — 高波动动量股涨3%武装→回调8%卖→反弹, 纯损耗)
            if pnl_pct <= -V28_STOP_LOSS:
                sell_reason = f"止损{pnl_pct:.1%}"

        if sell_reason:
            account.execute(sym, "SELL", qty, price, reason=sell_reason)
            logger.info(f"🔴 v28卖出 {sym}: {sell_reason} PnL={pnl_pct:.1%}")


def _trading_days_since(last_rebal):
    """计算自上次再平衡以来经历的美股交易日数（剔除周末与 2026 假日）。

    F2 (SYSTEM_OPT_REPORT 改动4/P4): 再平衡 63 日历日 → 63 交易日。
    63 日历日≈44 交易日，等价于把再平衡从最优 63d 偷偷提到 42d 档，
    少赚约 1.7pp/年。此函数用 market_clock 的交易日口径重算。
    """
    if last_rebal is None:
        return 999
    try:
        from atos.core import market_clock
    except Exception:
        return (datetime.datetime.now() - last_rebal).days
    now = datetime.datetime.now()
    trading_days = 0
    d = last_rebal.date()
    while d < now.date():
        d += datetime.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if d in market_clock.US_HOLIDAYS_2026:
            continue
        trading_days += 1
    return trading_days


def _trend_derisk_scale(signals) -> float:
    """F4: QQQ 趋势去险系数 — QQQ 跌破 MA200 返回 V28_TREND_DERISK_SCALE, 否则 1.0。

    仅降险 (减少部署/增加现金), 不做抄底或加杠杆。signals 缺价/缺 MA 时安全返回 1.0。
    """
    q = signals.get(V28_CORE_SYMBOL, {}) or {}
    price = q.get("price", 0) or 0
    ma = q.get("ma200", 0) or 0
    if price > 0 and ma > 0 and price < ma:
        return V28_TREND_DERISK_SCALE
    return 1.0


def _v28_qqq_core_alpha(account, signals, regime, spy_trend):
    """v29 策略: QQQ 核心 + 动量个股 alpha (F2 优化: 7只 + 动量权重0.6)

    F2 规则 (hermes 系统级寻优, SYSTEM_OPT_REPORT):
    1. 60% 资金买 QQQ（始终持有，不择时）— 无 QQQ 硬止损(25% 极端兜底)/移动止损
    2. 40% 资金买 7 只最强动量股 (21日动量 + 距20日高点)
    3. 63 交易日再平衡 (原63日历日≈44交易日, 改交易日口径 +1.7pp)
    4. 个股硬止损 10% (原8%), 移动止损已移除 (whipsaw -6pp)
    """
    # P0-3: 读取风险敞口缩放系数（安全层×宏观门控合并值），clamp 到 [0,1]
    scale = max(0.0, min(1.0, getattr(account, '_risk_exposure_scale', 1.0)))

    # F4: QQQ 趋势去险叠加 — QQQ 跌破 MA200 时整体敞口 ×0.80 (降险/增现金)
    _tds = _trend_derisk_scale(signals)
    if _tds < 1.0:
        scale *= _tds
        logger.info(f"🛡️ v28趋势去险: QQQ 跌破 MA{V28_TREND_MA} → 敞口×{_tds:.2f}")

    equity = account.total_equity
    cash = account.cash

    # ── 卖出检查 (提取自 v28_check_exits, 盘中照常执行) ──
    v28_check_exits(account, signals)

    # ── 再平衡检查 (F2: 63 日历日 → 63 交易日) ──
    last_rebal = getattr(account, '_v28_last_rebalance', None)
    now = datetime.datetime.now()
    days_since = _trading_days_since(last_rebal)

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
