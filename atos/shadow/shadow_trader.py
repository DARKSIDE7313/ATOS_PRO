"""
ATOS PRO v3 — Shadow Trader（影子交易，完全重写版）
=================================================
核心架构变更：
  1. 因子引擎 → 主决策层（统计评分决定买什么）
  2. AI → 否决权(VETO)层（只阻止高风险交易）
  3. 严格风控 → 每个标的独立决策（无传染）
  4. 冷却期全覆盖 → 任何卖出都触发冷却
  5. 闭市后只做风控，不开仓

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.config_shared import ALLOCATION
from atos.core.logging import get_logger, log_trade, log_risk
from atos.core.metrics import format_report
from atos.live.signal_engine import get_signals, get_realtime_signals
from atos.live.risk_manager import (
    check_all_stops, check_daily_limits, record_fill,
    filter_orders, reset_cycle, reset_daily, update_drawdown, get_state as get_risk_state,
    COOLDOWN_CYCLES,
)
from atos.market.regime.regime_engine import RegimeEngine
from atos.factors import batch_value_factors, batch_momentum_factors, batch_quality_factors, combine, get_top_picks
from atos.core.universe import ALL_SYMBOLS, UNIVERSE_FULL
from atos.portfolio import compute_cash_buffer, compute_target_positions, should_rebalance, check_concentration_risk
from atos.shadow.reporter import generate_report
from atos.risk.advanced import (
    liquidity_check, filter_liquid_universe, calc_betas,
    hedge_suggestion, check_strategy_decay, detect_anomalies,
)
from atos.risk.professional import (
    triple_barrier, vol_target_position, TrailingStop, kelly_after_drawdown,
)
from atos.debugger.safety_net import (
    validate_market_data, safe_price, safe_divide, clamp, money_round,
    is_duplicate_order, check_disk_space, full_health_check,
    atomic_write, safe_load_json, is_safe_to_trade,
)
from atos.factors.advanced_signals import get_all_advanced_signals, intermarket_signals
from atos.market.regime_gate import evaluate_regime_gate, adjust_exposure_for_regime_gate  # 🆕 v4 宏观门控
from atos.live.kelly import kelly_fraction, kelly_qty, crouching_allocation  # 🆕 Crouching 仓位方法
from atos.longterm.serenity import get_chokepoint_candidates  # 🆕 Serenity 瓶颈扫描集成
from atos.scheduler import start_scheduler, stop_scheduler, signal_queue  # 🆕 Vibe-Trading 调度器

# ── Vibe Bridge 安全导入（atos/vibe_bridge.py 已删除，用 layers 替代）──
def is_vibe_alive() -> bool:
    try:
        from atos.layers.vibe_bridge import VibeBridge
        import asyncio
        return asyncio.get_event_loop().run_until_complete(VibeBridge().healthcheck())
    except Exception:
        return False

def run_swarm_research(symbols: list, goal: str = "") -> dict | None:
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
# 缓存层
# ============================================================
_spy_cache = None
_vix_cache = None
_cache_ts = None
_CACHE_TTL_MINUTES = 10  # 10分钟缓存


def _get_market_data_cached():
    """缓存SPY/VIX数据"""
    global _spy_cache, _vix_cache, _cache_ts
    now = datetime.datetime.now()
    if _spy_cache is not None and _vix_cache is not None and _cache_ts is not None:
        if (now - _cache_ts).total_seconds() < _CACHE_TTL_MINUTES * 60:
            return _spy_cache, _vix_cache
    spy = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="1y", interval="1d", progress=False, auto_adjust=True)
    _spy_cache, _vix_cache, _cache_ts = spy, vix, now
    return spy, vix


# ============================================================
# 模拟账户
# ============================================================
class ShadowAccount:
    """本地模拟账户"""

    def __init__(self, initial_cash: float = 1000000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}            # {symbol: {qty, avg_price, last_price, decision_id}}
        self.trade_history = []
        self.cycle_returns = []
        self.trailing_stops = {}       # {symbol: TrailingStop}
        self.cycle_count = 0
        self.prev_equity = initial_cash
        self.commission_per_share = 0.005
        self.min_commission = 1.0
        self.slippage_pct = 0.001      # 0.1%
        self.stop_loss_blacklist = {}  # {symbol: sell_cycle} — 任何卖出都加入
        self.strategy_decay_factor = 1.0
        self.peak_equity = initial_cash
        self.equity_history = []
        self._last_ai_cycle = 0        # 上次AI运行的周期

    # ---- 冷却期 ----
    def is_cooling_off(self, symbol: str) -> bool:
        """检查冷却期（任何卖出都会触发，不仅仅是止损）。
        
        BUGFIX P2 2026-06-12: 使用真实的动态冷却长度判断。
        """
        if symbol in self.stop_loss_blacklist:
            entry = self.stop_loss_blacklist[symbol]
            if isinstance(entry, dict):
                sold_cycle = entry.get("sold_cycle", 0)
                cooldown = entry.get("cooldown", COOLDOWN_CYCLES)
            else:
                # 兼容旧格式（纯整数）
                sold_cycle = entry
                cooldown = COOLDOWN_CYCLES
                # 升级为新格式
                self.stop_loss_blacklist[symbol] = {
                    "sold_cycle": sold_cycle,
                    "cooldown": cooldown,
                }
            if self.cycle_count - sold_cycle < cooldown:
                return True
            else:
                del self.stop_loss_blacklist[symbol]
        return False

    def add_to_blacklist(self, symbol: str):
        """任何卖出都加入冷却黑名单。
        
        BUGFIX P2 2026-06-12: 存储 sold_cycle + 真实 cooldown 长度。
        之前只存了 cycle_count，比较时仍然用的固定 COOLDOWN_CYCLES，
        导致动态冷却实际没生效。
        """
        vol_mult = 1.0
        try:
            if hasattr(self, 'positions') and symbol in self.positions:
                pos = self.positions[symbol]
                lp = pos.get("last_price", pos.get("avg_price", 0))
                atr_val = pos.get("atr", 0)
                if atr_val > 0 and lp > 0:
                    daily_vol = atr_val / lp
                    if daily_vol > 0.03:
                        vol_mult = 1.5
                    elif daily_vol < 0.01:
                        vol_mult = 0.7
        except Exception:
            pass
        
        dynamic_cooldown = int(COOLDOWN_CYCLES * vol_mult)
        self.stop_loss_blacklist[symbol] = {
            "sold_cycle": self.cycle_count,
            "cooldown": dynamic_cooldown,
        }
        logger.info(f"🔒 冷却: {symbol} → 禁止买入至周期#{self.cycle_count + dynamic_cooldown} (波动率系数×{vol_mult:.1f})")

    def clean_blacklist(self):
        """清理过期条目（按真实 cooldown 判断）"""
        expired = []
        for s, entry in self.stop_loss_blacklist.items():
            if isinstance(entry, dict):
                sold = entry.get("sold_cycle", 0)
                cd = entry.get("cooldown", COOLDOWN_CYCLES)
            else:
                sold = entry
                cd = COOLDOWN_CYCLES
            if self.cycle_count - sold >= cd:
                expired.append(s)
        for s in expired:
            del self.stop_loss_blacklist[s]

    # ---- 属性 ----
    @property
    def total_equity(self) -> float:
        """计算总权益 — 防御 NaN 污染"""
        pos_val = 0.0
        for p in self.positions.values():
            lp = p.get("last_price", p.get("avg_price", 0))
            qty = p.get("qty", 0)
            # 防御 nan / None / 负数
            if lp is None: lp = 0
            if isinstance(lp, float) and math.isnan(lp):
                lp = p.get("avg_price", 0)
            if lp is None: lp = 0
            if isinstance(lp, float) and str(lp) == "nan":
                lp = 0
            if lp <= 0:
                ap = p.get("avg_price", 0)
                lp = ap if ap and str(ap) != "nan" else 0
            if lp <= 0:
                lp = 0
            pos_val += qty * lp
        return self.cash + pos_val

    @property
    def position_list(self) -> list:
        result = []
        for sym, p in self.positions.items():
            last = p.get("last_price", p["avg_price"])
            pnl_pct = (last - p["avg_price"]) / p["avg_price"] if p["avg_price"] > 0 else 0
            result.append({
                "symbol": sym, "qty": p["qty"], "avg_price": p["avg_price"],
                "last": last, "mkt_val": last * p["qty"],
                "pnl_pct": round(pnl_pct, 4),
            })
        return result

    @property
    def mode(self) -> str:
        t = self.total_equity
        if t < 50000: return "VERY_AGGRESSIVE"
        elif t < 200000: return "AGGRESSIVE"
        elif t < 500000: return "MODERATE"
        return "CONSERVATIVE"

    # Bug #7 注释: mode 名称反映风险偏好而非仓位数量。
    # VERY_AGGRESSIVE(3仓)=资金极少时只能集中火力, AGGRESSIVE(15仓)=有资金后可以分散,
    # MODERATE(8仓)=适中, CONSERVATIVE(10仓)=大资金但适度分散保持流动性。
    @property
    def max_positions(self) -> int:
        return {"VERY_AGGRESSIVE": 3, "AGGRESSIVE": 15, "MODERATE": 8, "CONSERVATIVE": 10}[self.mode]

    @property
    def max_single_pct(self) -> float:
        # 统一15%硬上限（不管什么模式，避免BAC 26%的情况）
        return 0.10          # v6 进攻性单仓上限 10%

    @property
    def min_cash_pct(self) -> float:
        return {"VERY_AGGRESSIVE": 0.03, "AGGRESSIVE": 0.03, "MODERATE": 0.10, "CONSERVATIVE": 0.03}[self.mode]

    def get_state(self) -> dict:
        pos_val = sum(p["qty"] * p.get("last_price", p["avg_price"]) for p in self.positions.values())
        return {
            "total": self.total_equity,
            "cash": self.cash,
            "mkt_val": pos_val,
            "mode": self.mode,
            "max_positions": self.max_positions,
            "alloc": {"short_pct": 0.2, "long_pct": 0.7, "cash_pct": 0.1},
            "positions": self.position_list,
            "constraints": {
                "max_single_pct": self.max_single_pct,
                "short_budget": self.total_equity * 0.2,
                "long_budget": self.total_equity * 0.7,
                "min_cash": self.total_equity * self.min_cash_pct,
            },
        }

    def update_prices(self, signals: dict):
        """更新持仓价格 — 强制防御 NaN"""
        for sym, p in self.positions.items():
            if sym in signals:
                px = signals[sym].get("price", None)
                # 防御 nan：px 必须是 > 0 的数字
                if px is None or not isinstance(px, (int, float)):
                    continue
                if isinstance(px, float) and str(px) == "nan":
                    continue
                if px <= 0:
                    continue
                p["last_price"] = px
                # Fix #10: 存储 ATR 供滑点计算
                atr = signals[sym].get("atr", 0)
                if atr > 0:
                    p["atr"] = atr

    # ---- 执行 ----
    def execute(self, symbol: str, action: str, shares: int,
                price: float, reason: str = "") -> bool:
        """执行交易（带完整安全检查 + 风控记录）
        
        BUGFIX 2026-06-12: 执行层冷却拦截
        任何 BUY / ADD 都先检查冷却期，上层策略分支绕不过。
        """
        if shares <= 0 or not symbol or not isinstance(symbol, str):
            return False
        if safe_price(price) is None:
            return False
        if shares > 100000:
            logger.error(f"数量异常: {shares}股")
            return False
        if is_duplicate_order(symbol, action, shares):
            return False

        # BUGFIX P1: 执行层冷却拦截 — 任何 BUY/ADD 先查冷却
        if action in ("BUY", "ADD") and self.is_cooling_off(symbol):
            logger.debug(f"🚫 冷却拦截: {action} {symbol} (执行层)")
            return False

        # 硬性现金下限
        if action == "BUY":
            min_cash = self.total_equity * self.min_cash_pct
            estimated_cost = price * shares + max(self.min_commission, shares * self.commission_per_share)
            if self.cash - estimated_cost < min_cash:
                affordable = int((self.cash - min_cash) / (price * 1.001))
                if affordable <= 0:
                    return False
                shares = affordable

        # 单仓上限（硬约束，不允许超过）
        max_single_val = self.total_equity * self.max_single_pct
        current_val = self.positions[symbol]["qty"] * price if symbol in self.positions else 0
        max_buy = max_single_val - current_val
        if max_buy <= 0 and action == "BUY":
            logger.debug(f"  {symbol} 已达单仓上限 (${max_single_val:,.0f})")
            return False

        # 总仓位上限（防止所有仓位加起来超过85%总资产）
        if action == "BUY" or action == "ADD":
            total_pos_val = sum(p["qty"] * (p.get("last_price", p["avg_price"])) for p in self.positions.values())
            estimated_buy = price * shares
            max_total_pos = self.total_equity * 0.85
            if total_pos_val + estimated_buy > max_total_pos:
                available = max_total_pos - total_pos_val
                if available <= 0:
                    logger.debug(f"  总仓位已满 (${total_pos_val:,.0f}/${max_total_pos:,.0f})")
                    return False
                estimated_shares = max(1, int(available / price))
                if estimated_shares < shares:
                    shares = estimated_shares

        # 滑点 — Fix #10: 动态滑点，基于波动率
        daily_vol = 0.005
        if symbol in self.positions:
            atr_val = self.positions[symbol].get("atr", 0)
            if atr_val > 0 and price > 0:
                daily_vol = atr_val / price
        dynamic_slip = max(0.0005, min(0.005, daily_vol * 0.25))
        slip = price * dynamic_slip
        fill = price + slip if action == "BUY" else price - slip
        comm = max(self.min_commission, shares * self.commission_per_share)

        if action == "BUY":
            buy_val = fill * shares
            max_buy_val = max_single_val - current_val
            if buy_val > max_buy_val:
                shares = max(1, int(max_buy_val / fill))
            if shares <= 0:
                return False

            cost = fill * shares + comm
            if cost > self.cash:
                affordable = max(1, int((self.cash - self.min_commission) / fill))
                if affordable <= 0:
                    return False
                shares = affordable
                cost = fill * shares + comm

            self.cash -= cost
            if symbol in self.positions:
                old = self.positions[symbol]
                total_qty = old["qty"] + shares
                old_cost = old["qty"] * old["avg_price"]
                self.positions[symbol] = {
                    "qty": total_qty,
                    "avg_price": (old_cost + fill * shares) / total_qty,
                    "last_price": fill,
                }
            else:
                self.positions[symbol] = {"qty": shares, "avg_price": fill, "last_price": fill}

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": 0,
                "reason": reason,
                "source": "factor_engine",  # Fix #8: 策略归因
            })

        elif action == "SELL":
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
            if pos["qty"] < shares:
                shares = pos["qty"]

            pnl = (fill - pos["avg_price"]) * shares
            pnl_pct = (fill - pos["avg_price"]) / pos["avg_price"] if pos["avg_price"] > 0 else 0
            self.cash += fill * shares - comm

            # 记录风控
            record_fill(pnl, self.total_equity)

            # 保存到 trade_stats 供 Kelly 学习
            try:
                from atos.live.kelly import save_trade
                result = save_trade(pnl_pct)
                logger.info(f"[Kelly] 交易记录: {symbol} PnL={pnl_pct:.2%} total_trades={result.get('total_trades',0)} WR={result.get('win_rate',0):.1%}")
            except Exception as e:
                logger.warning(f"[Kelly] save_trade failed: {e}")

            pos["qty"] -= shares
            if pos["qty"] <= 0:
                del self.positions[symbol]
                if symbol in self.trailing_stops:
                    del self.trailing_stops[symbol]

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": round(pnl, 2),
                "reason": reason,
            })

            # v3: 任何卖出都触发冷却期
            self.add_to_blacklist(symbol)

        elif action == "ADD":
            # 加仓：按比例增持，但不超过单仓上限
            target_val = self.total_equity * 0.03  # 每次加仓3%
            add_val = min(target_val, max_buy, self.cash - self.total_equity * self.min_cash_pct)
            if add_val < price * 1.001:
                return False
            add_shares = max(1, int(add_val / fill))
            if add_shares <= 0:
                return False
            return self.execute(symbol, "BUY", add_shares, price, reason)

        log_trade(symbol, action, shares, price, reason=reason)
        return True


# ============================================================
# 主交易循环
# ============================================================
def run_shadow_cycle(account: ShadowAccount, cycle: int = 0):
    """影子交易周期（重写版）"""
    account.cycle_count += 1
    reset_cycle()
    logger.info(f"Cycle {cycle} (#{account.cycle_count}) | "
                f"Equity=${account.total_equity:,.0f} | "
                f"Cash=${account.cash:,.0f} | "
                f"Positions={len(account.positions)}")

    # 周期级安全检查
    check_disk_space(min_free_mb=50)
    full_health_check(account.get_state())

    # 紧急停止开关: 存在 /tmp/atos_EMERGENCY_STOP 时跳过所有交易并退出
    if os.path.exists("/tmp/atos_EMERGENCY_STOP"):
        logger.critical("🚨 紧急停止文件 /tmp/atos_EMERGENCY_STOP 存在 — 跳过所有交易并退出")
        # 保存最终状态
        try:
            save_state_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "..", "data", "shadow_state.json")
            state = {
                "initial_cash": account.initial_cash,
                "cash": account.cash,
                "positions": account.positions,
                "cycle_count": account.cycle_count,
                "equity": account.total_equity,
                "stopped_at": datetime.datetime.now().isoformat(),
                "reason": "EMERGENCY_STOP",
            }
            os.makedirs(os.path.dirname(save_state_path), exist_ok=True)
            with open(save_state_path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass
        sys.exit(0)

    # 市场时间检查
    market_ok, market_reason = is_safe_to_trade()
    is_market_hours = market_ok  # 仅在交易时段开新仓

    # ---- 1. 市场状态 ----
    spy, vix = _get_market_data_cached()
    # RegimeEngine 持久化实例（避免每次重建导致学习数据丢失）
    if not hasattr(run_shadow_cycle, '_regime_engine'):
        run_shadow_cycle._regime_engine = RegimeEngine()
    engine = run_shadow_cycle._regime_engine
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
    spy_trend = "BULL"
    try:
        spy_close = spy["Close"].squeeze()
        if spy_close.empty or len(spy_close) < 2:
            raise ValueError("SPY数据为空")
        spy_ma20 = spy_close.rolling(20).mean().iloc[-1] if len(spy_close) >= 20 else float('nan')
        spy_ma50 = spy_close.rolling(50).mean().iloc[-1] if len(spy_close) >= 50 else float('nan')
        spy_current = spy_close.iloc[-1]
        if math.isnan(spy_ma20) or math.isnan(spy_ma50):
            spy_trend = "UNKNOWN"
            logger.warning(f"⚠️ SPY趋势数据不足({len(spy_close)}根K线) → 降级UNKNOWN")
        elif spy_current < spy_ma20 and spy_current < spy_ma50:
            spy_trend = "BEAR"
            logger.warning(f"🐻 SPY趋势看空: ${spy_current:.0f} < MA20=${spy_ma20:.0f} < MA50=${spy_ma50:.0f}")
        elif spy_current < spy_ma20:
            spy_trend = "CAUTIOUS"
            logger.info(f"🟡 SPY趋势谨慎: ${spy_current:.0f} < MA20=${spy_ma20:.0f}")
    except Exception as e:
        spy_trend = "UNKNOWN"
        logger.warning(f"SPY趋势分析失败 → 降级UNKNOWN: {e}")

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
    except Exception as e:
        logger.error(f"因子失败: {e}")

    # 🆕 IC 反馈环：根据实盘收益评估因子预测能力
    # ============================================================
    # Vibe-Trading 火力全开：每小时触发一次 Swarm 多代理研究
    # ============================================================
    _last_vibe = getattr(run_shadow_cycle, "_last_vibe", 0)
    now = __import__("time").time()
    if now - _last_vibe > 1800 and is_vibe_alive():  # 30分钟更频繁触发 Vibe Swarm
        try:
            top_syms = [p["symbol"] for p in top_picks[:8]] if top_picks else []
            if len(top_syms) >= 3:
                swarm_result = run_swarm_research(top_syms, goal="find supply chain chokepoints and high conviction ideas")
                if swarm_result:
                    logger.info(f'[Vibe] Swarm 已触发: {swarm_result.get("run_id")}')
                    run_shadow_cycle._last_vibe = now
        except Exception as ve:
            logger.debug(f"[Vibe] Swarm 跳过: {ve}")

    try:
        from atos.factors.engine import ic_analysis
        # 用函数属性存储上周期分数（跨周期持久化）
        prev_scores = getattr(run_shadow_cycle, '_prev_scores', {})
        prev_breakdown = getattr(run_shadow_cycle, '_prev_breakdown', {})

        # 计算本周期实际收益（%）
        if prev_scores and factor_result:
            current_returns = {}
            for sym in prev_scores:
                sig = signals.get(sym, {})
                price_now = sig.get("price", 0)
                prev_price = getattr(run_shadow_cycle, '_prev_prices', {}).get(sym, 0)
                if price_now > 0 and prev_price > 0:
                    current_returns[sym] = (price_now - prev_price) / prev_price

            if len(current_returns) >= 10:
                ic_result = ic_analysis(prev_scores, current_returns,
                                        regime["regime"], prev_breakdown)
                logger.info(f"[IC反馈] IC={ic_result['ic']:.4f} | {ic_result.get('verdict','')} | n={ic_result['n']}")

        # 存储本周期分数和价格，供下周期使用
        run_shadow_cycle._prev_scores = factor_result.get("scores", {}) if factor_result else {}
        run_shadow_cycle._prev_breakdown = factor_result.get("breakdown", {}) if factor_result else {}
        run_shadow_cycle._prev_prices = {
            sym: sig.get("price", 0)
            for sym, sig in signals.items() if sig.get("price", 0) > 0
        }
    except Exception as ic_err:
        logger.debug(f"IC反馈环跳过: {ic_err}")

    # 更新价格
    account.update_prices(signals)

    # ---- 4. 风控阶段（硬止损/追踪止损/止盈）— 每个标的独立！ ----
    # 4a. 硬止损 + 硬止盈（统一检查）
    # Fix #9: 相关性崩盘熔断
    stp_count = 0
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

    # 4b. 追踪止损（每个标的独立判断）
    # BUGFIX 2026-06-11: 
    #   - BEAR/CAUTIOUS 趋势下完全关闭追踪止损（不是只是不创建新的）
    #   - 确认次数从5提升到8（40分钟过滤盘中假突破）
    #   - 日累计亏损超过3%时加宽止损幅度
    #   - 追踪止损本身已经有 confirm_cycles 保护，但确认完才触发
    
    # 检查日亏损状态：如果今天已经亏得多，加宽止损容忍度
    from atos.live.risk_manager import get_state as get_rm_state
    rm_state = get_rm_state()
    daily_pnl_pct = abs(rm_state.get("daily_pnl_pct", 0))
    dd_widen_factor = 1.0
    if daily_pnl_pct > 0.03:
        dd_widen_factor = 1.5  # 日亏>3%：加宽50%止损线，减少进一步触发
        logger.info(f"📉 日亏损{daily_pnl_pct:.2%}>3% — 加宽追踪止损 {dd_widen_factor:.0%}")
    elif daily_pnl_pct > 0.02:
        dd_widen_factor = 1.3  # 日亏>2%：加宽30%
    
    # 趋势分级止损策略：
    #   BEAR     = 全关（持有等反弹）
    #   CAUTIOUS = 保留追踪止损但加宽1.5倍止损线
    #   BULL     = 正常追踪止损
    if spy_trend == "BEAR":
        use_trailing = False
        trail_widen = 1.0
        if account.trailing_stops:
            account.trailing_stops.clear()
            logger.info("🐻 BEAR趋势: 关闭所有追踪止损，持有等反弹")
    elif spy_trend == "CAUTIOUS":
        use_trailing = True
        trail_widen = 1.5
        logger.info("🟡 CAUTIOUS趋势: 保留追踪止损但加宽%.0f倍" % trail_widen)
    else:
        use_trailing = True
        trail_widen = 1.0
    
    for sym, pos in list(account.positions.items()):
        price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
        if price <= 0:
            continue
        if sym not in account.trailing_stops:
            if not use_trailing:
                continue
            # 波动率追踪止损（确认周期8次=40分钟，有效过滤假突破）
            atr_val = signals.get(sym, {}).get("atr", 0)
            trail = max(0.04, min(0.12, (atr_val / price) * 4)) if atr_val > 0 else 0.05
            # 趋势分级加宽止损线（CAUTIOUS模式下给更大容忍度）
            trail = trail * dd_widen_factor * trail_widen
            ts = TrailingStop(trail_pct=trail, confirm_cycles=8)
            ts.init(pos["avg_price"])
            account.trailing_stops[sym] = ts
            continue

        result = account.trailing_stops[sym].update(price)
        if result["triggered"]:
            pnl_pct = result.get("unrealized_pnl", 0)
            # BUGFIX: 检查是否已经是正收益的追踪止盈，或者亏损很小才触发
            # 如果 PnL 在 -3%~+3% 内，给一次额外机会（可能是噪音）
            if -0.03 <= pnl_pct <= 0.03:
                # 重置确认次数，再观察
                account.trailing_stops[sym]._breach_count = 0
                logger.debug(f"⏳ {sym} 追踪接近阈值但PnL={pnl_pct:+.2%}很小，跳过此次触发")
                continue
            account.execute(sym, "SELL", pos["qty"], price, reason="追踪止损")
            log_risk("TRAILING_STOP", f"{sym}: {result['reason']}")
            logger.info(f"🎯 追踪止损: {sym} PnL={pnl_pct:+.2%}")
            continue

    # 4c. 回撤更新
    account.peak_equity = max(account.peak_equity, account.total_equity)
    update_drawdown(account.total_equity, account.peak_equity)
    current_dd = (account.peak_equity - account.total_equity) / account.peak_equity
    if current_dd > 0.05:
        logger.info(f"📉 当前回撤: {current_dd:.2%} (峰值${account.peak_equity:,.0f})")

    # 4d. 风格检查（回撤/熔断）
    risk_state = get_risk_state()
    if risk_state["circuit_open"]:
        logger.warning(f"🔴 熔断中: {risk_state.get('daily_pnl_pct', 0):.2%} 日亏损")
        # 熔断后只跑风控，不开仓
        _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks, {},
                        "circuit_open", spy_trend)
        return

    # ---- 5. AI 否决审查（进攻版: 每12周期，闭市时也检查持仓紧急情况） ----
    # 进攻模式:
    #   - 每12周期运行一次（从24提频）
    #   - 交易时段: 跑全部候选审查
    #   - 闭市时段: 只跑持仓检查（紧急情况）
    AI_CYCLE_INTERVAL = 6  # 每6周期≈30分钟（从12提频，更多AI决策）
    ai_veto_map = {}
    if account.cycle_count % AI_CYCLE_INTERVAL == 0:
        try:
            from atos.ai.engine_v4 import veto_candidates
            
            # 构建前3候选的简化 veto 数据
            veto_candidate_list = []
            for pick in (top_picks or [])[:3]:
                sym = pick["symbol"]
                sig = signals.get(sym, {})
                bd = pick.get("breakdown", {})
                factor_score = pick.get("score", 0.5)
                # 计算评分理由摘要
                score_parts = []
                for k in ["value", "momentum", "quality", "technical"]:
                    v = bd.get(k, 0.5)
                    if v > 0.6:
                        score_parts.append(f"{k}={v:.2f}")
                score_reason = "+".join(score_parts) if score_parts else f"总分={factor_score:.2f}"
                
                veto_candidate_list.append({
                    "symbol": sym,
                    "price": sig.get("price", 0),
                    "factor_score": round(factor_score, 2),
                    "reason": score_reason,
                    "rsi": sig.get("rsi", 50),
                    "spy_price": spy_c[-1] if spy_c else 0,
                    "spy_trend": spy_trend,
                    "vix": round(current_vix, 1),
                    "regime": regime.get("regime", "UNKNOWN") if isinstance(regime, dict) else "UNKNOWN",
                })
            
            if veto_candidate_list:
                raw_veto_map = veto_candidates(veto_candidate_list)
                ai_veto_map = raw_veto_map
                vetoed_count = sum(1 for v in ai_veto_map.values() if v)
                if vetoed_count:
                    logger.info(f"🧠 AI否决: {vetoed_count}/{len(veto_candidate_list)} 被阻止")
                else:
                    logger.info(f"🧠 AI否决: 全部批准 ({len(veto_candidate_list)}候选)")

                # Fix #5: 记录 AI 决策到记忆库
                try:
                    from atos.ai.memory import record_decision
                    for c in veto_candidate_list:
                        vetoed = bool(ai_veto_map.get(c["symbol"], False))
                        record_decision(
                            symbol=c["symbol"],
                            action="VETO" if vetoed else "APPROVE",
                            confidence=0.7,
                            factor_score=c.get("factor_score", 0.5),
                            reasons={"vetoed": vetoed, "regime": regime.get("regime", "UNKNOWN")},
                            debate_summary=f"AI veto {'blocked' if vetoed else 'approved'} {c['symbol']}",
                            market_regime=regime.get("regime", "UNKNOWN") if isinstance(regime, dict) else "UNKNOWN",
                        )
                except Exception as mem_err:
                    logger.debug(f"AI记忆写入失败: {mem_err}")
        except Exception as e:
            logger.error(f"AI否决审查失败: {e}")
            ai_veto_map = {}
    else:
        ai_veto_map = {}

    # ---- 6. 新开仓（因子引擎主决策，AI否决已预先过滤） ----
    if is_market_hours:
        # 传递 ai_veto_map 给因子开仓
        _factor_based_buying(account, signals, top_picks, factor_result, regime, spy_trend, 
                            gate_exposure, ai_veto_map)
    else:
        logger.info("🏁 闭市时段: 不开新仓，仅维持风控")

    # ---- 7. 最终结算 ----
    _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks,
                    ai_veto_map, "normal", spy_trend)


# ============================================================
# 因子开仓（v3新函数）
# ============================================================
def _factor_based_buying(account, signals, top_picks, factor_result, regime, spy_trend, gate_exposure=1.0, ai_veto_map=None):
    """基于因子评分开仓。AI只有否决权，没有开仓权。

    v5 (Crouching) 修改:
      - 集成 Serenity 瓶颈扫描：当天瓶颈候选+0.15因子加分
      - 使用 Crouching 方法计算仓位（比 Kelly 更激进但有回撤保护）
      - 做空比例>10%的标的额外加分
      - 保留波动率目标作为下限保护

    Args:
        gate_exposure: RGVH风格宏观门控暴露系数（0.0-1.0）
        ai_veto_map: {symbol: {"veto": True/False}} — AI否决映射
    """
    if ai_veto_map is None:
        ai_veto_map = {}
    if not top_picks:
        logger.info("无高分候选标的")
        return

    # v6: 进攻性改动 — CAUTIOUS 允许小开仓（最多 3 只），BEAR 仍不开新仓
    if spy_trend == "BEAR":
        logger.info(f"🐻 趋势BEAR — 不开新仓，仅维持风控")
        return
    if spy_trend == "CAUTIOUS":
        logger.info(f"🟡 趋势CAUTIOUS — 允许小仓位进攻（上限5只）")
        trend_max_pos = 5   # 从3扩到5，匹配当前实际持仓水平

    # 🆕 v5: 运行 Serenity 瓶颈扫描，获取候选加分（缓存版，每小时最多一次）
    serenity_boosts = {}
    _last_serenity_scan = getattr(account, '_last_serenity_scan', None)
    _now = datetime.datetime.now()
    if _last_serenity_scan is None or (_now - _last_serenity_scan).total_seconds() > 3600:
        account._last_serenity_scan = _now
        try:
            scan_symbols = list(signals.keys()) if signals else None
            if scan_symbols and len(scan_symbols) > 5:
                scan_top = sorted(scan_symbols,
                    key=lambda s: signals[s].get("score", 0), reverse=True)[:50]
                serenity_boosts = get_chokepoint_candidates(scan_top)
                if serenity_boosts:
                    logger.info(f"🧩 Serenity瓶颈加分: {len(serenity_boosts)}只")
                    for sym, b in sorted(serenity_boosts.items(), key=lambda x: -x[1])[:5]:
                        logger.info(f"  +{b:.2f} {sym}")
        except Exception as e:
            logger.debug(f"Serenity瓶颈扫描跳过: {e}")
    else:
        logger.debug(f"Serenity瓶颈扫描跳过: 距离上次不足1小时")

    # 趋势限制（放宽: BULL 5→10, CAUTIOUS 3→5, BEAR 2→3, 配合5%现金下限可部署8个仓位）
    trend_max_pos = {"BULL": 10, "CAUTIOUS": 5, "BEAR": 3}.get(spy_trend, 10)
    effective_max_pos = min(account.max_positions, trend_max_pos)

    # 行业分散（扩展为所有行业都限制，不仅仅是 Tech）
    SECTOR_LIMITS = {"Tech": 0.30, "Financial": 0.25, "Healthcare": 0.25,
                     "Consumer": 0.25, "Industrial": 0.25, "Energy": 0.20,
                     "ETF": 0.35, "Bond": 0.20, "Commodity": 0.15}
    sector_exposure = {}
    if account.positions:
        try:
            from atos.portfolio.correlation import get_sector_exposure, SECTOR_MAP
            sector_exposure = get_sector_exposure(account.position_list, SECTOR_MAP)
        except Exception:
            pass

    # 当前持仓数已达上限
    if len(account.positions) >= effective_max_pos:
        logger.debug(f"持仓已满 ({len(account.positions)}/{effective_max_pos})，不开新仓")
        return

    max_deploy = account.total_equity * (1.0 - account.min_cash_pct) * gate_exposure
    if gate_exposure < 1.0:
        logger.info(f"📊 宏观门控后部署预算: ${max_deploy:,.0f} (系数×{gate_exposure:.0%})")

    # Fix #2: 组合优化检查 — 现金不足时强制筹资
    current_cash_pct = account.cash / account.total_equity if account.total_equity > 0 else 0
    target_cash_pct = 0.05 if spy_trend == "BULL" else (0.10 if spy_trend == "CAUTIOUS" else 0.15)
    if current_cash_pct < target_cash_pct:
        logger.warning(f"💰 现金不足 {current_cash_pct:.1%} < {target_cash_pct:.0%}，只卖不买")
        return  # 不开新仓，等现有仓位止盈/止损释放现金

    # 🆕 v5: 当前回撤（用于 Crouching 方法）
    current_dd = 0.0
    try:
        from atos.live.risk_manager import get_state as get_risk_state
        rs = get_risk_state()
        current_dd = rs.get("current_drawdown", 0.0)
        if current_dd is None:
            current_dd = 0.0
    except Exception:
        current_dd = 0.0

    deployed = 0

    # ============================================================
    # 硬性要求：至少50%仓位必须是ETF（被动、低费、跑赢大盘基础）
    # ============================================================
    ETF_UNIVERSE = {"SPY", "QQQ", "IWM", "VTI", "VOO", "IVV", "SPY", "DIA", "XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC"}
    
    # 计算当前ETF持仓占比
    etf_value = 0.0
    total_pos_value = 0.0
    for sym, pos in account.positions.items():
        val = pos.get("qty", 0) * pos.get("last_price", pos.get("avg_price", 0))
        total_pos_value += val
        if sym in ETF_UNIVERSE:
            etf_value += val
    
    etf_pct = etf_value / total_pos_value if total_pos_value > 0 else 0.0
    force_etf_only = etf_pct < 0.50
    if force_etf_only:
        logger.info(f"🛡️ ETF强制模式 (≥50%): 当前ETF占比={etf_pct:.1%} < 50%，新开仓只允许ETF")

    # 候选：因子评分 > 0.30（基金级校准：从0.55降为0.30，匹配新的0基准评分体系）
    # 实测因子引擎最高分约0.40（GS/MU），阈值0.30可选出5-8只候选
    # 应用 Serenity 加分后重新排序
    enhanced_candidates = []
    for p in top_picks:
        sym = p["symbol"]
        if sym in account.positions:
            continue
        base_score = p.get("score", 0)
        # 应用 Serenity 瓶颈加分
        serenity_boost = serenity_boosts.get(sym, 0.0) * 1.6   # 基金级加强
        enhanced_score = base_score + serenity_boost
        enhanced_candidates.append({
            "symbol": sym,
            "score": enhanced_score,
            "base_score": base_score,
            "serenity_boost": serenity_boost,
            "original": p,
        })

    # 按增强后的评分排序
    enhanced_candidates.sort(key=lambda x: -x["score"])
    candidates = [c for c in enhanced_candidates if c["score"] > 0.30]

    for pick in candidates:
        sym = pick["symbol"]
        if deployed >= max_deploy:
            break
        if len(account.positions) >= effective_max_pos and sym not in account.positions:
            break

        # 冷却期检查
        if account.is_cooling_off(sym):
            logger.info(f"⏭ {sym} 冷却期未过，跳过")
            continue

        # AI否决检查（新架构核心）
        if sym in ai_veto_map:
            veto_info = ai_veto_map[sym]
            if isinstance(veto_info, dict) and veto_info.get("veto", False):
                logger.info(f"🧠 AI否决跳过 {sym}: {veto_info.get('reason', '')[:50]}")
                continue
            elif isinstance(veto_info, bool) and veto_info:
                logger.info(f"🧠 AI否决跳过 {sym}")
                continue

        # 行业敞口检查
        try:
            from atos.portfolio.correlation import SECTOR_MAP
            sym_sector = SECTOR_MAP.get(sym, "Unknown")
            sector_limit = SECTOR_LIMITS.get(sym_sector, 0.20)
            if sector_exposure.get(sym_sector, 0) >= sector_limit:
                logger.info(f"⏭ {sym} 行业{sym_sector}已超限 (容忍度最高{sector_limit:.0%})")
                continue
        except Exception:
            pass

        price = signals.get(sym, {}).get("price", 0)
        if price <= 0:
            continue
        
        # ============================================================
        # 硬性要求：必须跑赢手续费 + 滑点（真正生效）
        # ============================================================
        # 当前止盈9% - 手续费0.6% = 8.4% 净空间，满足 MIN_PROFIT_EDGE
        # 但低分标的（0.55-0.60）需要额外buffer，防止被手续费吃掉
        # 低分标的过滤：基金级校准 — 0.30以下跳过（匹配新评分体系，最高分仅0.40）
        if pick["score"] < 0.30 and not force_etf_only:
            logger.info(f"⏭ {sym} score={pick['score']:.2f}<0.30 跳过，分数太低")
            continue

        # RSI过滤 — 基金级校准：从 68 放宽到 72。当前大盘回调期，强势股 RSI 在 60-70 之间
        rsi = signals.get(sym, {}).get("rsi", 50)
        if rsi > 72:
            logger.info(f"⏭ {sym} RSI={rsi:.0f}>72 超买")
            continue

        # MA200偏离过滤 — 基金级校准：放宽到 25%，回调市场个股可能从底部反弹很远
        ma200 = signals.get(sym, {}).get("ma200", 0)
        if ma200 > 0 and price > ma200 * 1.25:
            logger.info(f"⏭ {sym} 价格偏离MA200>{((price/ma200-1)*100):.0f}%>25%")
            continue

        # Bug #2: 修复死代码 — 已有持仓允许加仓（仅盈利时）
        is_add = sym in account.positions
        if is_add:
            pos = account.positions[sym]
            avg_px = pos.get("avg_price", 0)
            if avg_px <= 0:
                continue
            pnl_pct = (price - avg_px) / avg_px
            if pnl_pct < 0:
                logger.debug(f"⏭ {sym} 浮亏{pnl_pct:.1%} — 禁止加仓")
                continue
            # 加仓不超过单仓上限的50%
            current_val = pos["qty"] * price
            max_single_val = account.total_equity * account.max_single_pct
            if current_val >= max_single_val * 0.5:
                logger.debug(f"⏭ {sym} 已达加仓上限")
                continue

        # Crouching 方法计算仓位
        enhanced_score = pick["score"]
        serenity_boost = pick["serenity_boost"]
        has_catalyst = (serenity_boost >= 0.10)  # STRONG_CHOKEPOINT = has catalyst

        crouching_pct = crouching_allocation(
            score=min(enhanced_score, 1.0),
            drawdown=current_dd,
            has_news_catalyst=has_catalyst,
        )

        # 波动率目标仓位（作为下限保护）
        atr_val = signals.get(sym, {}).get("atr", 0)
        daily_vol = atr_val / price if atr_val > 0 else 0.02
        per_symbol_budget = max_deploy / max(effective_max_pos - len(account.positions), 1)

        vol_result = vol_target_position(
            capital=min(per_symbol_budget, account.total_equity * account.max_single_pct),
            price=price, volatility=daily_vol,
            target_annual_vol=0.15, max_position_pct=account.max_single_pct,
        )

        # BUGFIX P4 2026-06-12: 从 max() 改保守融合
        # 不再取两者中较大值（推高建仓尺寸），改用加权平均 + clamp。
        # crouching_pct 更激进，vol_pct 更保守，取 0.4×crouching + 0.6×vol
        vol_pct = vol_result.get("weight", 0) if vol_result else 0
        if crouching_pct > 0 and vol_pct > 0:
            target_pct = 0.4 * crouching_pct + 0.6 * vol_pct
        else:
            target_pct = max(crouching_pct, vol_pct)  # 只有一个有值时取那个
        target_pct = min(target_pct, account.max_single_pct)
        # 再加一道回撤折扣：回撤>3%时总仓位×0.85
        if current_dd > 0.03:
            target_pct *= 0.85
        target_val = account.total_equity * target_pct

        # 考虑已有持仓 — Bug #2: 加仓路径已在上方过滤（仅盈利时可到达此处）
        current_val = account.positions[sym]["qty"] * price if sym in account.positions else 0
        delta_val = target_val - current_val
        if delta_val <= 0:
            continue

        shares = max(1, int(delta_val / price))

        if shares < 1 or shares * price < 100:
            continue

        # 交易成本检查
        est_cost = max(account.min_commission, shares * account.commission_per_share) + price * shares * account.slippage_pct
        if price * shares * 0.005 < est_cost:
            continue

        reason_parts = [f"因子开仓 score={pick['base_score']:.2f}"]
        if serenity_boost > 0:
            reason_parts.append(f"Serenity+{serenity_boost:.2f}")
        reason_parts.append(f"仓位{target_pct:.1%}")

        ok = account.execute(sym, "BUY", shares, price,
                             reason=" | ".join(reason_parts))
        if ok:
            deployed += shares * price
            logger.info(f"✅ 开仓 {sym}: {shares}股 @${price:.2f} (crouching={target_pct:.1%}, score={pick['base_score']:.2f})")

    logger.info(f"开仓完成: {len(account.positions)}持仓, 部署${deployed:,.0f}")


# ============================================================
# AI 否决审查（低频）
# ============================================================
def _run_ai_review(account, signals, top_picks, regime, spy_trend):
    """运行AI否决审查，仅返回否决映射。"""
    try:
        # 构建快照
        import datetime
        snapshot = {
            "mode": account.mode,
            "total_equity": account.total_equity,
            "cash": account.cash,
            "positions": account.position_list,
            "market_regime": regime,
            "factor_rankings": [
                {"symbol": p["symbol"], "score": p["score"], "breakdown": p.get("breakdown", {})}
                for p in top_picks[:10]
            ] if top_picks else [],
            "vix": round(regime.get("vix", 18) if isinstance(regime, dict) else 18, 1),
            "constraints": account.get_state()["constraints"],
            "universe": [{"symbol": s, **signals[s]} for s in list(signals.keys())[:10] if s in signals],
        }

        from atos.ai.engine_v4 import get_advice_v2 as get_advice_fallback
        advice = get_advice_fallback(snapshot)
        veto_map = advice.get("veto_map", {})

        # 检查否决的标的，如果已经在持仓中则跳过
        for sym, veto in veto_map.items():
            if veto.get("veto", False) and sym not in account.positions:
                logger.info(f"🧠 AI否决阻止买入 {sym}: {veto.get('reason', '')[:60]}")

        cio_note = advice.get("cio_market_read", "")[:100]
        if cio_note:
            logger.info(f"CIO: {cio_note}")

        return veto_map

    except Exception as e:
        logger.error(f"AI否决审查失败: {e}")
        return {}


# ============================================================
# 周期结束
# ============================================================
def _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks,
                    ai_veto_map, mode, spy_trend):
    """每个周期结束前的最终处理"""
    # 记录周期收益 — 从 equity_history 精确计算
    current_eq = round(account.total_equity, 2)

    # 找前一个有效 equity 值
    prev_eq = None
    # 从 equity_history 最近的非当前条目回溯
    for e in reversed(account.equity_history):
        eq_val = e.get("equity") if isinstance(e, dict) else e
        if isinstance(eq_val, (int, float)) and eq_val > 0:
            prev_eq = eq_val
            break
    
    if prev_eq is None:
        prev_eq = account.initial_cash
    
    # 精确计算 cycle return
    cycle_ret = (current_eq - prev_eq) / prev_eq if prev_eq > 0 else 0
    # 防御 nan
    if isinstance(cycle_ret, float) and str(cycle_ret) in ("nan", "inf", "-inf"):
        cycle_ret = 0.0
    
    account.cycle_returns.append(round(cycle_ret, 6))
    # 只保留最近 1000 个周期收益
    if len(account.cycle_returns) > 1000:
        account.cycle_returns = account.cycle_returns[-1000:]
    
    account.equity_history.append({
        "time": datetime.datetime.now().isoformat(),
        "equity": current_eq,
    })
    # 只保留最近 500 个历史点
    if len(account.equity_history) > 500:
        account.equity_history = account.equity_history[-500:]
    
    account.prev_equity = current_eq

    # 更新峰值
    account.peak_equity = max(account.peak_equity, current_eq)
    # 更新回撤
    current_dd = (account.peak_equity - current_eq) / account.peak_equity if account.peak_equity > 0 else 0
    from atos.live.risk_manager import update_drawdown
    update_drawdown(current_eq, account.peak_equity)

    logger.info(f"Cycle {cycle} done | Equity=${current_eq:,.0f} | "
                f"Ret={cycle_ret:+.4%} | Mode={mode} | "
                f"DD={current_dd:.2%} | Peak=${account.peak_equity:,.0f}")

    # 保存状态
    state_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "shadow_state.json"
    )
    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "peak_equity": account.peak_equity,
        "equity_history": account.equity_history,
        "last_cycle": datetime.datetime.now().isoformat(),
        "stop_loss_blacklist": account.stop_loss_blacklist,
        "strategy_decay_factor": account.strategy_decay_factor,
        # 序列化后的追踪止损（Dashboard 显示用）
        "trailing_stops": {
            sym: {
                "trail_pct": round(ts.trail_pct, 4),
                "highest_price": round(ts.highest_price, 2),
                "stop_price": round(ts.stop_price, 2),
                "entry_price": round(ts.entry_price, 2),
                "breach_count": ts._breach_count,
                "confirm_cycles": ts.confirm_cycles,
            }
            for sym, ts in account.trailing_stops.items()
        } if hasattr(account, "trailing_stops") else {},
    }
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    atomic_write(state_file, json.dumps(state, indent=2))

    # 生成透明报告
    try:
        generate_report(
            account=account, cycle=cycle, regime=regime, vix=current_vix,
            factor_rankings=[{"symbol": p["symbol"], "score": p["score"]} for p in top_picks] if top_picks else [],
            trades=account.trade_history[-20:],
            ai_risks=f"AI vetoes: {sum(1 for v in ai_veto_map.values() if v.get('veto'))}/{len(ai_veto_map)}",
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

    # 恢复持久化风险状态
    from atos.live.risk_manager import load_risk_state
    load_risk_state()

    state_file = os.path.join(base_dir, "data", "shadow_state.json")

    # 恢复状态
    if os.path.exists(state_file):
        with open(state_file) as f:
            saved = json.load(f)
        account = ShadowAccount(initial_cash=saved.get("initial_cash", ALLOCATION["short_term"]))
        account.cash = saved.get("cash", account.initial_cash)
        account.positions = saved.get("positions", {})
        account.trade_history = saved.get("trade_history", [])
        account.cycle_returns = saved.get("cycle_returns", [])
        account.cycle_count = saved.get("cycle_count", 0)
        account.prev_equity = saved.get("equity", account.initial_cash)
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
                state = {
                    "initial_cash": account.initial_cash,
                    "cash": account.cash,
                    "positions": account.positions,
                    "trade_history": account.trade_history,
                    "cycle_returns": account.cycle_returns,
                    "cycle_count": account.cycle_count,
                    "equity": account.total_equity,
                    "peak_equity": account.peak_equity,
                    "equity_history": getattr(account, "equity_history", []),
                    "stopped_at": datetime.datetime.now().isoformat(),
                    "stop_loss_blacklist": account.stop_loss_blacklist,
                    "strategy_decay_factor": account.strategy_decay_factor,
                    "trailing_stops": {
                        sym: {
                            "trail_pct": round(ts.trail_pct, 4),
                            "highest_price": round(ts.highest_price, 2),
                            "stop_price": round(ts.stop_price, 2),
                            "entry_price": round(ts.entry_price, 2),
                            "breach_count": ts._breach_count,
                            "confirm_cycles": ts.confirm_cycles,
                        }
                        for sym, ts in account.trailing_stops.items()
                    } if hasattr(account, "trailing_stops") else {},
                }
                os.makedirs(os.path.dirname(state_file), exist_ok=True)
                try:
                    atomic_write(state_file, json.dumps(state, indent=2))
                except Exception:
                    pass
                sys.exit(0)

            cycle += 1
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
                logger.critical(f"💀 永久性错误，系统退出: {err_type}: {err}")
                os.remove(lock_file) if os.path.exists(lock_file) else None
                sys.exit(1)
            elif "402" in err or "Payment Required" in err or "insufficient_quota" in err:
                logger.warning("⚠️ DeepSeek API 余额不足！降频到30分钟。")
                time.sleep(30 * 60)
            elif is_transient:
                backoff = min(300, 30 * (1 + (cycle % 5)))
                logger.warning(f"⏳ 瞬时错误，{backoff}s后重试: {err_type}: {err[:80]}")
                time.sleep(backoff)
            else:
                logger.error(f"⚠️ 未知错误，60s后继续: {err_type}: {err[:80]}")
                time.sleep(60)

    # 保存最终状态
    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "peak_equity": account.peak_equity,
        "equity_history": getattr(account, "equity_history", []),
        "stopped_at": datetime.datetime.now().isoformat(),
        "stop_loss_blacklist": account.stop_loss_blacklist,
        "strategy_decay_factor": account.strategy_decay_factor,
    }
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass
    logger.info(f"最终权益: ${account.total_equity:,.0f} | 交易数: {len(account.trade_history)}")


if __name__ == "__main__":
    main()
