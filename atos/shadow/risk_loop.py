"""
ATOS PRO — Shadow 风控阶段 (Phase 5 框架重塑)
================================================
从 shadow_trader.py run_shadow_cycle 的 "section 4 风控阶段" 抽离
(原 841-1207 行)。包含全部卖出规则集群:
  4a. 硬止损/止盈统一检查 (check_all_stops) + 相关性崩盘熔断
  -   v24 两阶段部分止盈 (+5%/+15%) + Citadel 动量衰减止盈
  4b. 追踪止损 (趋势分级: BEAR关/CAUTIOUS加宽/BULL正常)
  -   v17 Triple-Barrier 时间/波动率退出
  -   v25 快速剥头皮 / v23 保本+分批止盈 / 自适应止盈 / Citadel超买止盈
  -   v28 ATR动态止损 / v22 Flat清理
  4c. 动量退出 + 弱势退出
  4d. 回撤更新 + Citadel 单仓集中度熔断
  4e. legacy 熔断检查 (circuit_open → halt)

所有策略阈值与逻辑 **逐字保留**, 零数值改动。
v28 持仓隔离统一走 is_v28_position() (原散落的 if-flag 收敛, 报告 §7.3.1)。

接口:
    run_risk_phase(account, signals, spy_trend) -> (is_market_hours, halt_mode)
      is_market_hours: 风控可能强制关仓 (相关性崩盘 → False)
      halt_mode: None=继续主流程; "circuit_open"=调用方 finalize 后 return
"""

import datetime
from typing import Optional

from atos.core.logging import get_logger, log_trade, log_risk
from atos.live.risk_manager import check_all_stops, update_drawdown
from atos.risk.professional import TrailingStop, triple_barrier
from atos.shadow.strategy_v28 import is_v28_position

logger = get_logger("shadow_trader")


def run_risk_phase(account, signals, spy_trend) -> tuple:
    """风控阶段 (卖出为主)。逐字迁移自原 run_shadow_cycle section 4。"""

    # ---- 4. 风控阶段（硬止损/追踪止损/止盈）— 每个标的独立！ ----
    # 4a. 硬止损 + 硬止盈（统一检查）
    # Fix #9: 相关性崩盘熔断
    stp_count = 0
    is_market_hours = True  # 本阶段返回值: 相关性崩盘时强制 False
    for order in check_all_stops(account.position_list, signals):
        sym = order["symbol"]
        px = signals.get(sym, {}).get("price", 0)
        if px <= 0:
            continue
        qty = min(order["qty"], account.positions.get(sym, {}).get("qty", 0))
        if qty <= 0:
            continue
        account.execute(sym, "SELL", qty, px, reason=order["reason"])
        logger.info(f"🚨 {order['exit_type']}: {sym} {qty}股 {order['reason']}")
        stp_count += 1

    # Fix #9: 相关性崩盘检测 — 单周期多止损 → 熔断新开仓
    if stp_count >= 3:
        logger.critical(f"🚨 相关性崩盘: {stp_count}只持仓触发止损 — 本周期暂停新开仓")
        is_market_hours = False  # 强制跳过新开仓

    # ── 组合轮动已禁用 — 历史数据显示此逻辑是最大亏损来源 ──
    # 文艺复兴/AQR等顶级基金的核心原则: 让赢家跑, 让止损负责退出
    # 频繁轮动 = 手续费 + 滑点 + 追涨杀跌 = 稳定亏损
    # 仅保留止损(-5%)和止盈(+15%)作为退出机制
    ROTATION_DISABLED = True

    # ── P0-2: 循环1 两阶段部分止盈(+5%/+15%) 已合并入下方 4b 的单一非-v28 止盈状态机 ──
    # 原循环1 的 Tier1/Tier2 (+5%/+15%, _partial1_ 旗标) 与 4b 的 Tier1/Tier2/Tier3
    # (+3%/+5%/+8%, recent_partials) 是两套独立状态机，同仓会双倍部分止盈。
    # 现统一为 4b 的单一三档止盈表 (单一 recent_partials 来源)。
    # Citadel 动量衰减止盈 (MACD<0) 亦并入 4b (见下方)。

    # 4b. 追踪止损（每个标的独立判断）
    # BUGFIX 2026-06-11:
    #   - BEAR/CAUTIOUS 趋势下完全关闭追踪止损（不是只是不创建新的）
    #   - 确认次数从5提升到8（40分钟过滤盘中假突破）
    #   - 日累计亏损超过3%时加宽止损幅度
    #   - 追踪止损本身已经有 confirm_cycles 保护，但确认完才触发

    # 检查日亏损状态：如果今天已经亏得多，加宽止损容忍度
    # P0-1: dd_widen_factor 改用日级权益基准。主源 = kill_switch 的日级 _day_start_equity
    # (mark-to-market 日内亏损)；兜底 = 复位后的 legacy _daily_pnl_pct
    # (已由 shadow_trader 跨天 reset_daily() 保证日级，不再是跨天累积值)。
    from atos.live.risk_manager import get_state as get_rm_state
    from atos.core.kill_switch import get_kill_switch
    rm_state = get_rm_state()
    _ks = get_kill_switch()
    _day_start_eq = _ks.get_day_start_equity()
    if _day_start_eq and _day_start_eq > 0:
        daily_pnl_pct = abs((account.total_equity - _day_start_eq) / _day_start_eq)
    else:
        daily_pnl_pct = abs(rm_state.get("daily_pnl_pct", 0))
    dd_widen_factor = 1.0
    if daily_pnl_pct > 0.035:  # v10: 日亏>3.5%才加宽 (原2.5%太敏感)
        dd_widen_factor = 1.4
        logger.info(f"📉 日亏损{daily_pnl_pct:.2%}>3.5% — 加宽追踪止损 {dd_widen_factor:.0%}")
    elif daily_pnl_pct > 0.025:
        dd_widen_factor = 1.2  # 日亏>2.5%：加宽20%

    # 趋势分级止损策略（v8 收紧版）：
    #   BEAR     = 全关（持有等反弹）
    #   CAUTIOUS = 保留追踪止损但加宽1.3倍
    #   BULL     = 正常追踪止损
    if spy_trend == "BEAR":
        use_trailing = False
        trail_widen = 1.0
        if account.trailing_stops:
            account.trailing_stops.clear()
            logger.info("🐻 BEAR趋势: 关闭所有追踪止损，持有等反弹")
    elif spy_trend == "CAUTIOUS":
        use_trailing = True
        trail_widen = 1.15   # v10: 从 1.3 降低 — 别太宽，追踪止损才有意义
        logger.info("🟡 CAUTIOUS趋势: 保留追踪止损但加宽%.0f倍" % trail_widen)
    else:
        use_trailing = True
        trail_widen = 1.0

    for sym, pos in list(account.positions.items()):
        price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
        if price <= 0:
            continue
        pnl_pct = (price - pos["avg_price"]) / pos["avg_price"] if pos["avg_price"] > 0 else 0

        # ── v17: Triple-Barrier 时间退出检查（专业级）──
        # 持仓超过20天 → 即使盈亏不大也退出，释放资金到更好的机会
        # v29 FIX: v28 持仓跳过 — v28 季度(63天)再平衡,20天强制退出会破坏策略
        _is_v28_pos = is_v28_position(sym)
        if _is_v28_pos:
            hold_days = 0  # v28 持仓不用 Triple-Barrier
        else:
            hold_days = 0
            if sym in account.positions:
                buy_time = account.positions[sym].get("buy_time", None)
            if buy_time:
                try:
                    from datetime import datetime as _dt
                    bought = _dt.fromisoformat(str(buy_time)) if isinstance(buy_time, str) else buy_time
                    hold_days = (_dt.now() - bought).total_seconds() / 86400
                except Exception:
                    pass
        # Triple-Barrier: 波动率自适应退出
        # v29: v28 持仓完全跳过 Triple-Barrier (时间和波动率)
        if not _is_v28_pos:
            atr_pct = (signals.get(sym, {}).get("atr", 0) / price) if price > 0 else 0.02
            tb = triple_barrier(pos["avg_price"], price, 0.0, hold_days,
                               volatility=max(0.01, atr_pct), max_hold_days=20)
            if tb["exit"] and tb["barrier"] == "time":
                account.execute(sym, "SELL", pos["qty"], price,
                              reason=f"时间到期 {hold_days:.0f}天 (Triple-Barrier)")
                logger.info(f"⏰ Triple-Barrier: {sym} 持仓{hold_days:.0f}天 到期退出")
                continue
            if tb["exit"] and tb["barrier"] == "stop":
                account.execute(sym, "SELL", pos["qty"], price,
                              reason=f"TB止损 (vol={atr_pct:.1%})")
                logger.info(f"🛑 Triple-Barrier止损: {sym} PnL={pnl_pct:+.2%}")
                continue

        # ── v28: 跳过旧止盈/保本/剥头皮规则 — v28 有自己的卖出逻辑 ──
        # v28: 只用硬止损(5%) + 移动止损(8%) + 季度再平衡，不做分批止盈
        _v28_position = is_v28_position(sym)
        if not _v28_position:
            # ── v24: Citadel 动量衰减止盈 (自循环1合并 — P0-2) ──
            # 浮盈>3%但MACD转负 → 动量衰减，提前锁利
            if pnl_pct > 0.03:
                macd_val = signals.get(sym, {}).get("macd_hist", 0)
                if macd_val < -0.3:
                    momentum_exit_key = f"_momexit_{sym}"
                    if not getattr(account, momentum_exit_key, False):
                        sell_qty = max(1, pos["qty"] // 3)
                        if sell_qty > 0:
                            account.execute(sym, "SELL", sell_qty, price,
                                          reason=f"动量衰减止盈 +{pnl_pct:.1%} MACD={macd_val:.2f} (卖1/3)")
                            logger.info(f"📉 动量衰减止盈: {sym} {sell_qty}股 +{pnl_pct:.1%} MACD转负")
                            setattr(account, momentum_exit_key, True)
                            continue

            # ── v23: 利润保护 — 更早保本 + 分批止盈 ──
            # 0. v25: Renaissance 快速剥头皮 — 持仓<1天且盈利>2% → 快速锁利
            buy_time_str = pos.get("buy_time", "")
            if buy_time_str:
                try:
                    bought_dt = datetime.datetime.fromisoformat(str(buy_time_str))
                    hours_held = (datetime.datetime.now() - bought_dt).total_seconds() / 3600
                    if hours_held < 24 and pnl_pct >= 0.02:
                        account.execute(sym, "SELL", pos["qty"], price, reason=f"快速剥头皮 +{pnl_pct:.1%} ({hours_held:.0f}h)")
                        logger.info(f"⚡ 剥头皮: {sym} +{pnl_pct:.1%} {hours_held:.0f}h → 全卖")
                        continue
                except (ValueError, TypeError):
                    pass

            # 1. +3%: 止损提到成本价（保本）
            if pnl_pct >= 0.03 and sym in account.trailing_stops:
                ts = account.trailing_stops[sym]
                if ts.activation_price is None or ts.activation_price < pos["avg_price"] * 1.001:
                    ts.activation_price = pos["avg_price"] * 1.001
            # 2. 分批止盈
            recent_partials = sum(
                1 for t in account.trade_history[-10:]
                if t.get("symbol") == sym and "止盈" in t.get("reason", "")
            )
            if pnl_pct >= 0.03 and recent_partials == 0:
                quarter = max(1, pos["qty"] // 4)
                account.execute(sym, "SELL", quarter, price, reason=f"Tier1止盈 +{pnl_pct:.1%} (卖1/4锁利@3%)")
                logger.info(f"💰 Tier1止盈: {sym} +{pnl_pct:.1%} 卖{quarter}股")
                continue
            if pnl_pct >= 0.05 and recent_partials == 1:
                quarter = max(1, pos["qty"] // 4)
                account.execute(sym, "SELL", quarter, price, reason=f"Tier2止盈 +{pnl_pct:.1%} (卖1/4锁利@5%)")
                logger.info(f"💰 Tier2止盈: {sym} +{pnl_pct:.1%} 卖{quarter}股")
                continue
            if pnl_pct >= 0.08 and recent_partials == 2:
                quarter = max(1, pos["qty"] // 4)
                account.execute(sym, "SELL", quarter, price, reason=f"Tier3止盈 +{pnl_pct:.1%} (卖1/4锁利@8%)")
                logger.info(f"💰 Tier3止盈: {sym} +{pnl_pct:.1%} 卖{quarter}股")
                continue
            # 3. 自适应止盈
            tp_level = 0.22 if spy_trend == "BULL" else (0.18 if spy_trend == "CAUTIOUS" else 0.12)
            if pnl_pct >= tp_level:
                account.execute(sym, "SELL", pos["qty"], price, reason=f"止盈 +{pnl_pct:.1%}")
                logger.info(f"💰 止盈: {sym} +{pnl_pct:.1%}")
                continue
            # 3b. Citadel超买主动止盈
            rsi_sell = signals.get(sym, {}).get("rsi", 50)
            if rsi_sell > 80 and pnl_pct > 0.02:
                half = max(1, pos["qty"] // 2)
                account.execute(sym, "SELL", half, price, reason=f"超买止盈 RSI={rsi_sell:.0f} PnL={pnl_pct:+.1%}")
                logger.info(f"📈 Citadel超买止盈: {sym} RSI={rsi_sell:.0f} PnL={pnl_pct:+.1%} 卖{half}股")
                continue
        # 4. 🏦 v28: ATR动态止损 — 与 v28 策略对齐
        # H12: v28 持仓(QQQ+alpha)跳过 section-4 ATR 硬止损，交给 _v28_qqq_core_alpha 自己的止损逻辑
        if _v28_position:
            continue
        atr = signals.get(sym, {}).get("atr", 0)
        if atr > 0 and price > 0:
            atr_pct_stop = atr / price
            if spy_trend == "BULL":
                sl_mult = 3.0
            elif spy_trend == "CAUTIOUS":
                sl_mult = 2.5
            else:
                sl_mult = 2.0
            sl_atr = sl_mult * atr_pct_stop
            # v28: QQQ 用 12% 止损，个股用 5%
            if sym == "QQQ":
                sl_level = max(0.08, min(0.12, sl_atr))
            else:
                sl_level = max(0.04, min(0.05, sl_atr))
        else:
            sl_level = 0.12 if sym == "QQQ" else 0.05
        if pnl_pct <= -sl_level:
            account.execute(sym, "SELL", pos["qty"], price, reason=f"硬止损 {pnl_pct:.1%} (上限{sl_level:.0%})")
            logger.info(f"🛑 止损: {sym} {pnl_pct:.1%} (上限{sl_level:.0%})")
            continue

        # 🏦 v22: Flat 持仓清理 — 持有7天以上且不涨不跌(-2%~+2%) → 卖出释放资金
        # v28: 跳过 — v28 持仓按季度再平衡，不做Flat清理
        if not _v28_position and hold_days >= 7 and abs(pnl_pct) < 0.02:
            # 但如果有高因子分数(>0.6)或强MACD，则保留
            score = signals.get(sym, {}).get("score", 0)
            macd_h = signals.get(sym, {}).get("macd_hist", 0)
            if score < 0.55 and macd_h <= 0:
                account.execute(sym, "SELL", pos["qty"], price,
                              reason=f"Flat清理 {hold_days:.0f}天 PnL={pnl_pct:+.1%}")
                logger.info(f"🗑 Flat清理: {sym} 持有{hold_days:.0f}天不涨 释放资金")
                continue

        # v28: 不设置 trailing stop — v28 有自己的卖出逻辑
        if _v28_position:
            continue

        if sym not in account.trailing_stops:
            if not use_trailing:
                continue
            # 🆕 v17 回测优化 — 自适应追踪止损
            # BULL: 宽止损 14% 让赢家奔跑
            # CAUTIOUS: 中等 11%
            # BEAR: 紧止损 7%（防守优先）
            atr_val = signals.get(sym, {}).get("atr", 0)
            if atr_val > 0 and price > 0:
                daily_vol = atr_val / price
                if spy_trend == "BULL":
                    trail = max(0.08, min(0.18, daily_vol * 3.0))
                elif spy_trend == "CAUTIOUS":
                    trail = max(0.06, min(0.14, daily_vol * 2.5))
                else:
                    trail = max(0.05, min(0.10, daily_vol * 2.0))
            else:
                trail = 0.14 if spy_trend == "BULL" else (0.10 if spy_trend == "CAUTIOUS" else 0.07)
            widen = min(max(dd_widen_factor, trail_widen), 1.5)
            trail = min(trail * widen, 0.20 if spy_trend == "BULL" else 0.14)
            confirm = 2 if spy_trend == "BULL" else (3 if spy_trend == "CAUTIOUS" else 4)
            ts = TrailingStop(trail_pct=trail, confirm_cycles=confirm)
            ts.init(pos["avg_price"])
            act_pct = 1.05 if spy_trend == "BULL" else (1.04 if spy_trend == "CAUTIOUS" else 1.02)
            ts.activation_price = pos["avg_price"] * act_pct
            account.trailing_stops[sym] = ts
            continue

        result = account.trailing_stops[sym].update(price)
        if result["triggered"]:
            # v8: 不再给"额外机会"——触发就卖
            account.execute(sym, "SELL", pos["qty"], price, reason=f"追踪止损 (确认{result['breach_count']}/{result['confirm_cycles']})")
            log_risk("TRAILING_STOP", f"{sym}: {result['reason']}")
            logger.info(f"🎯 追踪止损: {sym} PnL={pnl_pct:+.2%}")
            continue

    # 4c. 动量退出 — 持仓不涨不跌超过阈值 → 释放资金给更强信号
    # v23: 解决"卡死"问题 — 持仓横盘但占用资金,系统无法开新仓
    _MOMENTUM_EXIT_DAYS = 5       # 5天不涨不跌就走
    _MOMENTUM_EXIT_THRESHOLD = 0.015  # 1.5%以内算"不涨不跌"
    for sym, pos in list(account.positions.items()):
        if sym not in account.positions:
            continue
        # v28: 跳过动量退出 — v28 持仓按季度再平衡
        if is_v28_position(sym):
            continue
        buy_date_str = pos.get("buy_date", pos.get("buy_time", ""))
        if not buy_date_str:
            continue
        try:
            buy_date = datetime.fromisoformat(buy_date_str.replace("Z", "+00:00"))
            days_held = (datetime.now(buy_date.tzinfo) - buy_date).days if buy_date.tzinfo else (datetime.now() - buy_date).days
        except Exception:
            continue

        avg = pos["avg_price"]
        lp = pos.get("last_price", avg)
        pnl_pct = (lp - avg) / avg if avg > 0 else 0

        # 不涨不跌判定: 持有>5天, |pnl|<1.5%, 且MACD不强势
        macd_hist = signals.get(sym, {}).get("macd_hist", 0)
        rsi = signals.get(sym, {}).get("rsi", 50)

        if (days_held >= _MOMENTUM_EXIT_DAYS
            and abs(pnl_pct) < _MOMENTUM_EXIT_THRESHOLD
            and macd_hist < 0.05
            and rsi < 55):
            reason = f"动量退出 (持{days_held}天, PnL{pnl_pct:+.1%}, MACD={macd_hist:.3f})"
            account.execute(sym, "SELL", pos["qty"], lp, reason=reason)
            log_trade("SELL", sym, pos["qty"], lp, reason)
            logger.info(f"🔄 {reason}: {sym}")
            continue

        # 弱势持仓加速退出: 持有>3天, 亏损>2%, MACD<0, RSI<40
        if (days_held >= 3
            and pnl_pct < -0.02
            and macd_hist < 0
            and rsi < 40):
            reason = f"弱势退出 (持{days_held}天, PnL{pnl_pct:+.1%}, RSI={rsi:.0f})"
            account.execute(sym, "SELL", pos["qty"], lp, reason=reason)
            log_trade("SELL", sym, pos["qty"], lp, reason)
            logger.info(f"🔄 {reason}: {sym}")
            continue

    # 4d. 回撤更新
    account.peak_equity = max(account.peak_equity, account.total_equity)
    update_drawdown(account.total_equity, account.peak_equity)
    current_dd = (account.peak_equity - account.total_equity) / account.peak_equity if account.peak_equity > 0 else 0
    if current_dd > 0.05:
        logger.info(f"📉 当前回撤: {current_dd:.2%} (峰值${account.peak_equity:,.0f})")

    # ── v24: Citadel 单仓集中度熔断 — 单仓>15%自动减持到12% ──
    # 防止单一持仓过大导致黑天鹅风险
    # v29 FIX: 跳过 v28 持仓 — QQQ 目标60%,alpha股目标~8-12%,熔断会死循环卖出
    _CONC_LIMIT = 0.15   # 单仓上限15%
    _CONC_TARGET = 0.12  # 减持目标12%
    for sym, pos in list(account.positions.items()):
        # v29: v28 持仓完全跳过集中度熔断 (QQQ目标60%是策略设计,不是风险)
        if is_v28_position(sym):
            continue
        lp = pos.get("last_price", pos.get("avg_price", 0))
        mkt_val = pos["qty"] * lp
        weight = mkt_val / account.total_equity if account.total_equity > 0 else 0
        if weight > _CONC_LIMIT and lp > 0:
            # 计算需要卖多少股才能回到12%
            target_val = account.total_equity * _CONC_TARGET
            excess_val = mkt_val - target_val
            sell_qty = max(1, int(excess_val / lp))
            if sell_qty < pos["qty"]:
                account.execute(sym, "SELL", sell_qty, lp,
                              reason=f"集中度熔断 {weight:.0%}>{_CONC_LIMIT:.0%} → 减至{_CONC_TARGET:.0%}")
                logger.info(f"🛡️ 集中度熔断: {sym} {weight:.1%}>{_CONC_LIMIT:.0%} 卖{sell_qty}股")

    # 4e. 风格检查（回撤/熔断）
    from atos.live.risk_manager import get_state as get_risk_state
    risk_state = get_risk_state()
    if risk_state["circuit_open"]:
        logger.warning(f"🔴 熔断中: {risk_state.get('daily_pnl_pct', 0):.2%} 日亏损")
        # 熔断后只跑风控，不开仓
        return is_market_hours, "circuit_open"

    return is_market_hours, None
