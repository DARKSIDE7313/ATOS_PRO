"""
ATOS PRO v3 — Shadow Trader（影子交易，完全重写版）
=================================================
核心架构变更：
  1. 因子引擎 → 主决策层（统计评分决定买什么）
  2. AI → 否决权(VETO)层（只阻止高风险交易）
  3. 严格风控 → 每个标的独立决策（无传染）
  4. 冷却期全覆盖 → 任何卖出都触发冷却
  5. 闭市后只做风控，不开仓

Phase 5 框架重塑 (2026-08-22):
  本文件从 2059 行上帝对象拆分为 **纯编排层**，职责实现全部下沉到
  atos/shadow/ 子模块 (均可独立测试):
    account.py         — ShadowAccount 账户领域对象 (execute 强制过风控门)
    state_store.py     — shadow_state.json 平铺 schema 唯一构造/读写点
    market_data.py     — SPY/VIX 缓存 + SPY 趋势分级
    cycle_state.py     — CycleState 跨周期状态单例 (替代函数属性缓存)
    strategy_v28.py    — v29 QQQ Core+Alpha 策略 + is_v28_position 单一隔离判定
    risk_loop.py       — 风控阶段全部卖出规则集群
    decision_layers.py — 质量门控 / 情报简报 / AI v6 建议
    ic_feedback.py     — IC 反馈环
    equity_tracker.py  — 周期结算 / 绩效 / day_changes 数据桥
  本文件仅保留: run_shadow_cycle (周期编排) + _finalize_cycle (结算编排)
  + main (进程入口)。顶部 re-export 保持对旧导入路径的完全兼容。

交易流程：
  信号 → 因子排名 → 风控过滤 → AI否决 → 执行
                      ↓
             已有持仓 → 止损检查 → 追踪止损 → 持仓复核

使用方法：
  python3 -m atos.shadow.shadow_trader
"""

import os
import sys
import json
import time
import datetime
import queue
import math
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.config_shared import ALLOCATION
from atos.core.logging import get_logger, log_trade, log_risk
from atos.live.signal_engine import get_signals, get_realtime_signals
from atos.live.risk_manager import (
    check_all_stops, record_fill, reset_cycle, reset_daily, update_drawdown,
    get_state as get_risk_state, COOLDOWN_CYCLES,
)
from atos.market.regime.regime_engine import RegimeEngine
from atos.factors import batch_value_factors, batch_momentum_factors, batch_quality_factors, combine, get_top_picks
from atos.core.universe import ALL_SYMBOLS
from atos.shadow.reporter import generate_report
from atos.risk.professional import TrailingStop, triple_barrier, vol_target_position, kelly_after_drawdown
from atos.debugger.safety_net import (
    safe_price, is_duplicate_order, check_disk_space, full_health_check,
    atomic_write, is_safe_to_trade,
)
from atos.market.regime_gate import evaluate_regime_gate
from atos.longterm.serenity import get_chokepoint_candidates
from atos.scheduler import start_scheduler, stop_scheduler, signal_queue

# ── Phase 5: 拆分后的子模块 ──
from atos.shadow.account import ShadowAccount
from atos.shadow.state_store import (
    build_state_dict, save_account_state, save_emergency_state,
    load_saved_state, get_state_file_path,
)
from atos.shadow.market_data import get_market_data_cached, compute_spy_trend
from atos.shadow.cycle_state import CycleState
from atos.shadow.strategy_v28 import (
    V28_ALPHA_UNIVERSE, V28_CORE_PCT, V28_ALPHA_COUNT, V28_REBALANCE_DAYS,
    V28_STOP_LOSS, V28_TRAILING_STOP, V28_QQQ_TRAILING,
    is_v28_position, _v28_qqq_core_alpha,
)
from atos.shadow.risk_loop import run_risk_phase
from atos.shadow.decision_layers import (
    compute_quality_gate, fetch_intel_briefing, run_ai_counsel,
)
from atos.shadow.ic_feedback import run_ic_feedback
from atos.shadow.equity_tracker import (
    compute_cycle_return, record_cycle_equity, update_perf_tracker,
    record_daily_returns, write_day_changes,
)
from atos.core.position_schema import normalize_positions, get_qty

# ── 兼容旧导入路径的别名 (外部代码零改动) ──
_save_account_state = save_account_state
_get_market_data_cached = get_market_data_cached

# ── Vibe Bridge 安全导入（atos/vibe_bridge.py 已删除，用 layers 替代）──
def is_vibe_alive() -> bool:
    try:
        from atos.layers.vibe_bridge import VibeBridge
        import asyncio
        return asyncio.get_event_loop().run_until_complete(VibeBridge().healthcheck())
    except Exception:
        return False

def run_swarm_research(symbols: list, goal: str = ""):
    try:
        from atos.layers.vibe_bridge import VibeBridge
        import asyncio
        bridge = VibeBridge()
        return asyncio.get_event_loop().run_until_complete(
            bridge.morning_scan(symbols, focus=goal, horizon="1-5 days"))
    except Exception as e:
        from atos.core.logging import get_logger
        get_logger("shadow_trader").warning(f"Vibe swarm 跳过: {e}")
        return None

# ============================================================
# 全局交易成本参数（必须跑赢大盘 + 手续费的核心）
# ============================================================
import yfinance as yf

logger = get_logger("shadow_trader")


# ============================================================
# P0-1: 日级风控重置 — 活跃循环跨天边界调用 risk_manager.reset_daily()
# 修复 B1: reset_daily() 原本只在死代码 live_trader.py 调用，活跃循环从不调用，
# 导致 _daily_pnl_pct / _orders_this_day 跨天无限累积 (追踪止损永久加宽)。
# reset_daily() 内部保留 consecutive_losses / current_drawdown 等跨日指标。
# ============================================================


def _get_market_date_safe():
    """获取美东市场日期（单一真源 get_market_date），异常时回退本地日期。"""
    try:
        from atos.core.market_clock import get_market_date
        return get_market_date()
    except Exception:
        return datetime.date.today()


# C2: 初始化即记录当前市场日期 — 避免日中断重启后首周期误触发 reset_daily()
# 清掉从 risk_state.json 恢复的当日累计 _daily_pnl。
_last_daily_reset_date = _get_market_date_safe()


def _run_daily_reset_if_new_day():
    """在每日边界（美东市场日期跨天）调用 reset_daily()。"""
    global _last_daily_reset_date
    _today = _get_market_date_safe()
    if _last_daily_reset_date != _today:
        reset_daily()
        _last_daily_reset_date = _today
        logger.info(f"🌅 日级风控状态已重置（新交易日 {_today}）")


# ============================================================
# 主交易循环 — 周期编排
# ============================================================
def run_shadow_cycle(account: ShadowAccount, cycle: int = 0):
    """影子交易周期（重写版）— Phase 5: 纯编排, 实现下沉至子模块"""
    state = CycleState.get()
    account.cycle_count += 1
    reset_cycle()
    _run_daily_reset_if_new_day()  # P0-1: 跨天日级重置 (保留跨日指标)
    logger.info(f"Cycle {cycle} (#{account.cycle_count}) | "
                f"Equity=${account.total_equity:,.0f} | "
                f"Cash=${account.cash:,.0f} | "
                f"Positions={len(account.positions)}")

    # 周期级安全检查
    check_disk_space(min_free_mb=50)
    full_health_check(account.get_state())

    # ═══ v29: Kill Switch 检查 (规格书 §10.3) ═══
    try:
        from atos.core.kill_switch import get_kill_switch
        if get_kill_switch().check(account):
            logger.critical("🔴 KILL SWITCH ACTIVE — 本周期停止所有交易")
            _finalize_cycle(account, cycle, "KILLED", 0, {}, [], {}, "kill_switch", "N/A")
            return
    except Exception as e:
        logger.error(f"Kill switch 检查异常 (fail closed): {e}")
        return

    # 紧急停止开关: 存在 /tmp/atos_EMERGENCY_STOP 时跳过所有交易并退出
    if os.path.exists("/tmp/atos_EMERGENCY_STOP"):
        logger.critical("🚨 紧急停止文件 /tmp/atos_EMERGENCY_STOP 存在 — 跳过所有交易并退出")
        # 保存最终状态
        try:
            save_emergency_state(account, reason="EMERGENCY_STOP")
        except Exception:
            pass
        sys.exit(0)

    # 市场时间检查
    market_ok, market_reason = is_safe_to_trade()
    is_market_hours = market_ok  # 仅在交易时段开新仓

    # ---- v28: 多层安全检查 ----
    try:
        from atos.core.safety_layer import full_safety_check
        _vix = None
        try:
            import yfinance as yf
            _vix = yf.Ticker("^VIX").history(period="1d")["Close"].iloc[-1]
        except Exception:
            pass
        _safety = full_safety_check(
            equity=account.total_equity,
            peak_equity=account.peak_equity,
            positions=account.positions,
            cash=account.cash,
            vix_level=_vix,
            spy_above_ma50=True,  # 简化，下面 regime 会精确判断
            daily_returns=getattr(account, '_daily_returns', None),
        )
        if _safety['action'] == 'LIQUIDATE':
            logger.critical(f"🚨 安全层清仓: {_safety['reasons']}")
            # 卖光所有持仓
            for _sym in list(account.positions.keys()):
                _pos = account.positions[_sym]
                _qty = get_qty(_pos)
                if _qty > 0:
                    _price = _pos.get("last_price", 0)
                    if _price > 0:
                        account.execute(_sym, "SELL", _qty, _price, reason=f"安全层清仓: {_safety['reasons'][0]}")
            return
        elif _safety['action'] == 'HALT':
            logger.warning(f"🛑 安全层暂停: {_safety['reasons']}")
            is_market_hours = False  # 禁止开新仓
        elif _safety['exposure'] < 1.0:
            logger.warning(f"⚠️ 安全层减仓: {_safety['reasons']} exposure={_safety['exposure']:.0%}")
    except Exception as e:
        logger.debug(f"安全层检查跳过: {e}")

    # ---- v26: 定时抓取新闻情绪（每30分钟一次）----
    import time as _time
    if _time.time() - state.last_news_fetch > 1800:  # 30分钟
        try:
            from atos.news.sentiment_engine import refresh_news
            refresh_news()
            state.last_news_fetch = _time.time()
        except Exception as e:
            logger.warning(f"📰 新闻抓取失败: {e}")

    # ---- 1. 市场状态 ----
    spy, vix = get_market_data_cached()
    # RegimeEngine 持久化实例（避免每次重建导致学习数据丢失）
    if state.regime_engine is None:
        state.regime_engine = RegimeEngine()
    engine = state.regime_engine
    # 先清除旧数据再用新数据填充（确保数据是最新的）
    # 基础级: 保留最近 500 个点的滚动窗口，不丢失学习数据
    spy_c = spy["Close"].squeeze().tolist()
    vix_c = vix["Close"].squeeze().tolist()
    for i in range(min(len(spy_c), len(vix_c))):
        engine.update(float(spy_c[i]), float(vix_c[i]))
    # 修剪到最近 500 个点，防止内存泄漏
    if len(engine.spy_prices) > 500:
        engine.spy_prices = engine.spy_prices[-500:]
    if len(engine.vix_prices) > 500:
        engine.vix_prices = engine.vix_prices[-500:]
    regime = engine.get_regime()
    current_vix = float(vix_c[-1]) if vix_c else 18.0
    logger.info(f"Regime={regime['regime']} | VIX={current_vix:.1f} | "
                f"{'📈 交易时段' if is_market_hours else '🏁 闭市'}")

    # SPY趋势过滤
    spy_trend = compute_spy_trend(spy)

    # 🆕 v4: RGVH 风格宏观门控（3独立过滤器）
    try:
        gate_result = evaluate_regime_gate()
        gate_exposure = gate_result["exposure"]
        if gate_exposure < 1.0:
            logger.info(f"📊 宏观门控: {gate_result['description']} → 暴露系数×{gate_exposure:.0%}")
    except Exception as e:
        logger.warning(f"宏观门控失败: {e}")
        gate_exposure = 1.0

    # ---- 2. 信号 ----
    use_realtime = getattr(account, '_use_realtime', True)
    signals = get_realtime_signals() if use_realtime else get_signals()
    if not signals:
        logger.warning("[Shadow] 空信号 — 跳过本周期，保留上周期状态")
        _finalize_cycle(account, cycle, regime, current_vix, {}, [], {},
                        "no_signals", spy_trend)
        return

    # 数据质量检查
    ds = signals.get("SPY", {}).get("data_source", "unknown")
    if "yfinance" in str(ds) and "Futu" not in str(ds):
        logger.warning(f"⚠️ 数据源降级: {ds} — 价格有15-20分钟延迟!")

    # 数据时效性检查 — Futu价格必须新鲜
    try:
        from atos.live.realtime_feeds import get_feed
        feed = get_feed()
        # v28i: FutuOpenD 不可用时跳过重连（避免阻塞）
        if feed._fallback:
            pass  # 已降级到 yfinance，不重连
        else:
            cache_stats = feed.cache.stats
            max_age = cache_stats.get("max_age_sec", 0)
            if max_age > 60:
                logger.warning(f"⏰ Futu数据过期: 最旧{max_age:.0f}秒 — 强制重连!")
                feed.reconnect()
            elif max_age > 3:
                logger.debug(f"Futu数据年龄: 最旧{max_age:.1f}秒")
    except Exception:
        pass

    # ---- 3. 因子 ----
    symbols = sorted(signals.keys())[:66]  # 全部信号标的都跑因子
    top_picks = []
    factor_result = {}
    try:
        v = batch_value_factors(symbols)
        m = batch_momentum_factors(symbols)
        q = batch_quality_factors(symbols)
        factor_result = combine(signals, v, m, q, regime["regime"], use_v3_signals=True)
        top_picks = get_top_picks(factor_result, n=10)
        # v27: IC方向自适应 — 负IC时不反转选股(避免选到垃圾股)，而是标记降仓
        if state.ic_inverted:
            logger.info(f"⚠️ IC负值({state.ic_ema or 0:.3f}) → 维持正常选股但降仓50%")
    except Exception as e:
        logger.error(f"因子失败: {e}")

    # 🆕 IC 反馈环：根据实盘收益评估因子预测能力
    # ============================================================
    # Vibe-Trading 火力全开：每小时触发一次 Swarm 多代理研究
    # ============================================================
    now = __import__("time").time()
    if now - state.last_vibe > 1800 and is_vibe_alive():  # 30分钟更频繁触发 Vibe Swarm
        try:
            top_syms = [p["symbol"] for p in top_picks[:8]] if top_picks else []
            if len(top_syms) >= 3:
                swarm_result = run_swarm_research(top_syms, goal="find supply chain chokepoints and high conviction ideas")
                if swarm_result:
                    logger.info(f'[Vibe] Swarm 已触发: {swarm_result.get("run_id")}')
                    state.last_vibe = now
        except Exception as ve:
            logger.debug(f"[Vibe] Swarm 跳过: {ve}")

    # 🆕 IC 反馈环（子模块）
    run_ic_feedback(signals, factor_result, regime)

    # 更新价格
    account.update_prices(signals)

    # ---- 4. 风控阶段（硬止损/追踪止损/止盈）— 每个标的独立！ ----
    risk_hours, halt_mode = run_risk_phase(account, signals, spy_trend)
    is_market_hours = is_market_hours and risk_hours
    if halt_mode:
        # 熔断后只跑风控，不开仓
        _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks, {},
                        halt_mode, spy_trend)
        return

    # ---- 5. 智能质量门控（替代低胜率 AI 辩论：基于因子质量+动量+RSI） ----
    # 🆕 每周期运行（不再跳周期），严格过滤低质量信号
    ai_veto_map = {}
    if is_market_hours:
        ai_veto_map = compute_quality_gate(top_picks, signals, spy_trend)

    # ── 5b. 🆕 实时情报简报（每周期运行，AI决策前优先参考）──
    intel_briefing = fetch_intel_briefing(top_picks, signals, account.cycle_count)

    # ── 5c. 🆕 增强AI决策 (每8周期≈40分钟, 轻量快速) ──
    # v6: 替换低胜率(6.4%)的旧AI辩论，使用硬规则+信心评分+情报融合
    counsel_map, counsel_hours = run_ai_counsel(
        account, top_picks, signals, spy_c, current_vix,
        regime, spy_trend, intel_briefing)
    if counsel_map is not None:
        ai_veto_map = counsel_map
    is_market_hours = is_market_hours and counsel_hours

    # ---- 6. v28: QQQ Core + Alpha 策略开仓 ----
    # 回测验证: 60% QQQ + 40% 动量股(5只), 年化26.8%, 跑赢SPY 11.7%
    if is_market_hours:
        _v28_qqq_core_alpha(account, signals, regime, spy_trend)
    else:
        logger.info("🏁 闭市时段: 仅维持风控，不开新仓")

    # ---- 7. 最终结算 ----
    _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks,
                    ai_veto_map, "normal", spy_trend)


# ============================================================
# 周期结束 — 结算编排 (实现下沉至 equity_tracker / state_store)
# ============================================================
def _count_ai_vetoes(ai_veto_map) -> int:
    """归一化 ai_veto_map 值形状并统计否决数。

    生产者 (compute_quality_gate / run_ai_counsel) 输出 {sym: bool}，
    但下游报告读取曾假设 {sym: {'veto': bool}} 的形状，值形状漂移
    触发 AttributeError 被 except 吞掉 → 整个透明报告静默跳过。
    此处防御性归一化两种形状，确保报告不再因值形状漂移而跳过。
    """
    if not ai_veto_map:
        return 0
    n = 0
    for v in ai_veto_map.values():
        if isinstance(v, dict):
            n += 1 if v.get("veto") else 0
        else:
            n += 1 if v else 0
    return n


def _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks,
                    ai_veto_map, mode, spy_trend):
    """每个周期结束前的最终处理"""
    # 记录周期收益 — 从 equity_history 精确计算
    current_eq = round(account.total_equity, 2)
    cycle_ret = compute_cycle_return(account, current_eq)
    record_cycle_equity(account, current_eq, cycle_ret)

    # 更新回撤
    current_dd = (account.peak_equity - current_eq) / account.peak_equity if account.peak_equity > 0 else 0
    update_drawdown(current_eq, account.peak_equity)

    logger.info(f"Cycle {cycle} done | Equity=${current_eq:,.0f} | "
                f"Ret={cycle_ret:+.4%} | Mode={mode} | "
                f"DD={current_dd:.2%} | Peak=${account.peak_equity:,.0f}")

    # ── v17: 统一绩效追踪 — 每20周期汇报 ──
    update_perf_tracker(current_eq, cycle_ret, cycle)

    # 记录每日收益
    record_daily_returns(current_eq, account)

    # 保存状态 (单一 schema 构造点: state_store.build_state_dict)
    state_file = get_state_file_path()
    state = build_state_dict(account)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    atomic_write(state_file, json.dumps(state, indent=2))

    # v5.1: 写日内涨跌数据供 Dashboard 读取（从 Futu OpenD 获取 prev_close）
    write_day_changes(account)

    # 生成透明报告
    try:
        generate_report(
            account=account, cycle=cycle, regime=regime, vix=current_vix,
            factor_rankings=[{"symbol": p["symbol"], "score": p["score"]} for p in top_picks] if top_picks else [],
            trades=account.trade_history[-20:],
            ai_risks=f"AI vetoes: {_count_ai_vetoes(ai_veto_map)}/{len(ai_veto_map)}",
        )
    except Exception as e:
        logger.debug(f"报告跳过: {e}")


# ============================================================
# 主入口
# ============================================================
def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    lock_file = os.path.join(base_dir, "data", ".shadow_trader.lock")

    # 进程锁（改进版：使用 socket 端口独占 + 文件锁双重机制）
    import socket
    _LOCK_PORT = 19999
    sock_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock_lock.bind(("127.0.0.1", _LOCK_PORT))
        sock_lock.listen(1)
    except OSError:
        # 进程已运行（端口被占用），静默退出
        return

    # 兼容旧版文件锁检测
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    logger.info("🚀 ATOS PRO v3 Shadow Trader 启动 (PAPER TRADING)")

    # 🆕 全局线程异常处理器 — 防止后台线程崩溃拖死主进程
    import threading as _threading
    _original_excepthook = _threading.excepthook
    def _safe_thread_excepthook(args):
        logger.critical(f"💥 后台线程异常: {args.exc_type.__name__}: {args.exc_value}")
        # Don't crash the main process
    _threading.excepthook = _safe_thread_excepthook

    # 恢复持久化风险状态
    from atos.live.risk_manager import load_risk_state
    load_risk_state()

    state_file = get_state_file_path()

    # v11: 短线资金上限
    max_short_capital = ALLOCATION.get("short_term", 1_000_000)

    # 恢复状态
    saved = load_saved_state()
    if saved is not None:
        # v11: 强制上限 — 防止旧状态覆盖当前 $1M 配置
        # v24 FIX: 允许利润累积 — cap改为initial*1.5（允许50%利润），不再吞掉收益
        max_allowed = max_short_capital * 1.50  # 允许最多50%利润
        initial = min(saved.get("initial_cash", max_short_capital), max_short_capital)
        account = ShadowAccount(initial_cash=initial)
        account.cash = min(saved.get("cash", account.initial_cash), max_allowed)
        account.positions = saved.get("positions", {})
        # Fix: 标准化持仓键名（shares ↔ qty 一致性）— 单一实现 position_schema
        normalize_positions(account.positions)
        account.trade_history = saved.get("trade_history", [])
        account.cycle_returns = saved.get("cycle_returns", [])
        account.cycle_count = saved.get("cycle_count", 0)
        account.prev_equity = min(saved.get("equity", account.initial_cash), max_allowed)
        account.equity_history = saved.get("equity_history", [])
        if not isinstance(account.equity_history, list):
            account.equity_history = []
        account.stop_loss_blacklist = saved.get("stop_loss_blacklist", {})
        account.strategy_decay_factor = saved.get("strategy_decay_factor", 1.0)
        account.peak_equity = saved.get("peak_equity", account.initial_cash)
        account.clean_blacklist()
        logger.info(f"恢复: 现金${account.cash:,.0f} | 持仓{len(account.positions)}只 | "
                    f"周期#{account.cycle_count} | 冷却{len(account.stop_loss_blacklist)}只")
    else:
        account = ShadowAccount(initial_cash=ALLOCATION["short_term"])

    # 实时数据源
    account._use_realtime = True
    try:
        from atos.live.realtime_feeds import get_feed
        feed = get_feed()
        if feed.is_connected():
            logger.info(f"✅ 实时数据源连接成功")
        else:
            account._use_realtime = False
    except Exception as e:
        logger.warning(f"实时数据源不可用: {e}")
        account._use_realtime = False

    # 🆕 启动 Vibe-Trading 调度器（后台线程）
    try:
        start_scheduler()
        logger.info("✅ Vibe-Trading 调度器已启动")
    except Exception as e:
        logger.warning(f"⚠️ Vibe-Trading 调度器启动失败（非阻塞）: {e}")

    # 🆕 启动 AutoPilot 自动诊断监控（后台线程）
    try:
        from atos.autopilot.monitor import get_monitor
        import threading
        _autopilot = get_monitor()
        _ap_thread = threading.Thread(target=_autopilot.run, daemon=True, name="autopilot")
        _ap_thread.start()
        logger.info("✅ AutoPilot AI 诊断监控已启动")
    except Exception as e:
        logger.warning(f"⚠️ AutoPilot 启动失败（非阻塞）: {e}")

    logger.info("Press Ctrl+C to stop")

    cycle = 0
    # Fix #9: 相关性崩盘熔断追踪器
    _crash_tracker = {"stop_count": 0, "window_start": time.time(), "halted": False}
    _CRASH_WINDOW_SEC = 300  # 5分钟窗口
    _CRASH_THRESHOLD = 3     # 窗口内3次止损 → 熔断

    while True:
        try:
            # 🆕 消费 Vibe 信號（非阻塞）
            try:
                while True:
                    vibe_signal = signal_queue.get_nowait()
                    logger.info(
                        f"[Vibe] 信號: {vibe_signal['ticker']} "
                        f"{vibe_signal['direction']} "
                        f"conf={vibe_signal['confidence']:.2f} "
                        f"size={vibe_signal['position_size']:.4f}"
                    )
            except queue.Empty:
                pass

            # 🔴 External kill-switch: if /tmp/atos_EMERGENCY_STOP exists, halt immediately
            if os.path.exists("/tmp/atos_EMERGENCY_STOP"):
                logger.critical("🔴 EMERGENCY STOP detected — halting all trading")
                stop_scheduler()  # 🆕 停止调度器
                # Save final state before exiting
                save_emergency_state(account, reason="EMERGENCY_STOP")
                sys.exit(0)

            cycle += 1

            # ── v23: 每日相关性扫描（每288周期=每天一次）──
            if cycle % 288 == 1 and len(account.positions) >= 2:
                try:
                    from atos.portfolio.correlation import check_concentration_risk
                    pos_list = []
                    for sym, pos in account.positions.items():
                        lp = pos.get("last_price", pos.get("avg_price", 0))
                        pos_list.append({
                            "symbol": sym,
                            "mkt_val": get_qty(pos) * lp,
                            "avg_price": pos.get("avg_price", 0),
                            "last_price": lp,
                            "qty": get_qty(pos),
                        })
                    alerts = check_concentration_risk(pos_list, correlation_threshold=0.75)
                    if alerts:
                        for a in alerts[:3]:  # 只处理最严重的前3对
                            logger.warning(f"🔗 相关性告警: {a['suggestion']}")
                        # 自动减持最高相关性配对中市值较小的
                        top = alerts[0]
                        reduce_sym = top.get("reduce_symbol", "")
                        # v29: 跳过 v28 持仓 — v28 策略有意持有高相关科技股组合
                        if reduce_sym and reduce_sym in account.positions and not is_v28_position(reduce_sym):
                            rpos = account.positions[reduce_sym]
                            rprice = rpos.get("last_price", rpos.get("avg_price", 0))
                            if rprice > 0:
                                reduce_qty = max(1, int(get_qty(rpos) * 0.30))
                                reason = f"相关性减持 ({top['pair'][0]}-{top['pair'][1]} corr={top['correlation']:.0%})"
                                account.execute(reduce_sym, "SELL", reduce_qty, rprice, reason=reason)
                                logger.info(f"🔗 {reason} — 卖{reduce_sym} {reduce_qty}股")
                except Exception as e:
                    logger.debug(f"相关性扫描跳过: {e}")

            run_shadow_cycle(account, cycle)
            time.sleep(5 * 60)  # 5分钟周期
        except KeyboardInterrupt:
            logger.info("手动停止")
            stop_scheduler()  # 🆕 停止调度器
            os.remove(lock_file) if os.path.exists(lock_file) else None
            break
        except Exception as e:
            err = str(e)[:200]
            # Fix #7: 区分瞬时错误 vs 永久性错误
            TRANSIENT_PATTERNS = [
                "timeout", "Connection", "Timed out", "Too Many Requests",
                "429", "503", "502", "temporarily", "SSLError", "reset by peer",
                "ConnectionError", "RemoteDisconnected", "ReadTimeout",
            ]
            PERMANENT_PATTERNS = [
                "ImportError", "ModuleNotFoundError", "SyntaxError",
                "NameError", "AttributeError", "KeyError: 'long_term'",
                "No module named", "cannot import",
            ]
            err_type = type(e).__name__
            is_transient = any(p.lower() in err.lower() for p in TRANSIENT_PATTERNS) or \
                          (err_type in ("TimeoutError", "ConnectionError", "HTTPError"))
            is_permanent = any(p.lower() in err.lower() for p in PERMANENT_PATTERNS) or \
                          err_type in ("ImportError", "ModuleNotFoundError", "SyntaxError")

            if is_permanent:
                logger.critical(f"💀 永久性错误: {err_type}: {err}")
                save_account_state(account)
                # Don't exit — just sleep and retry. LaunchAgent will restart if needed.
                logger.info("⏸ 等待 5 分钟后重试...")
                time.sleep(300)
            elif "402" in err or "Payment Required" in err or "insufficient_quota" in err:
                logger.warning("⚠️ DeepSeek API 余额不足！降频到30分钟。")
                time.sleep(30 * 60)
            elif is_transient:
                backoff = min(300, 30 * (1 + (cycle % 5)))
                logger.warning(f"⏳ 瞬时错误，{backoff}s后重试: {err_type}: {err[:80]}")
                time.sleep(backoff)
            else:
                logger.error(f"⚠️ 未知错误，60s后继续: {err_type}: {err[:80]}")
                import traceback as _tb
                logger.debug(f"完整回溯:\n{_tb.format_exc()}")
                time.sleep(60)

    # 保存最终状态
    save_emergency_state(account, reason="STOPPED")
    logger.info(f"最终权益: ${account.total_equity:,.0f} | 交易数: {len(account.trade_history)}")


if __name__ == "__main__":
    main()
