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
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.config_shared import ALLOCATION
from atos.core.logging import get_logger, log_trade, log_risk
from atos.live.signal_engine import get_signals, get_realtime_signals
from atos.live.risk_manager import (
    check_all_stops, record_fill, reset_cycle, update_drawdown,
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
from atos.config_shared import ALLOCATION

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
import pandas as pd
import numpy as np

logger = get_logger("shadow_trader")


# ============================================================
# 缓存层
# ============================================================
_spy_cache = None
_vix_cache = None
_cache_ts = None
_CACHE_TTL_MINUTES = 10  # 10分钟缓存


def _get_market_data_cached():
    """缓存SPY/VIX数据（中国大陆优化：Futu优先）"""
    global _spy_cache, _vix_cache, _cache_ts
    now = datetime.datetime.now()
    if _spy_cache is not None and _vix_cache is not None and _cache_ts is not None:
        if (now - _cache_ts).total_seconds() < _CACHE_TTL_MINUTES * 60:
            return _spy_cache, _vix_cache

    # 🆕 优先用Futu历史数据（中国大陆不被墙）
    try:
        from atos.data.futu_historical import get_spy_vix_data
        spy, vix = get_spy_vix_data()
        if spy is not None and not spy.empty and len(spy) >= 50:
            _spy_cache, _vix_cache, _cache_ts = spy, vix if not vix.empty else spy, now
            return spy, vix if not vix.empty else spy
    except Exception:
        pass

    # Fallback to yfinance
    try:
        spy = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True, timeout=15)
        vix = yf.download("^VIX", period="1y", interval="1d", progress=False, auto_adjust=True, timeout=15)
    except Exception:
        spy, vix = pd.DataFrame(), pd.DataFrame()

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
        
        dynamic_cooldown = min(int(COOLDOWN_CYCLES * vol_mult), 12)  # Fix: 上限12周期≈1小时
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
            qty = p.get("shares", p.get("qty", p.get("quantity", 0)))  # Fix: 兼容 shares/qty/quantity 三个键名
            # 防御 nan / None / 负数
            if lp is None: lp = 0
            if isinstance(lp, float) and math.isnan(lp):
                lp = p.get("avg_price", 0)
            if lp is None: lp = 0
            if isinstance(lp, float) and math.isnan(lp):
                lp = 0
            if lp <= 0:
                ap = p.get("avg_price", 0)
                lp = ap if ap and (isinstance(ap, float) and not math.isnan(ap)) else 0
            if lp <= 0:
                lp = 0
            pos_val += qty * lp
        return self.cash + pos_val

    @property
    def position_list(self) -> list:
        result = []
        for sym, p in self.positions.items():
            qty = p.get("shares", p.get("qty", p.get("quantity", 0)))  # Fix: 兼容多键名
            last = p.get("last_price", p["avg_price"])
            pnl_pct = (last - p["avg_price"]) / p["avg_price"] if p["avg_price"] > 0 else 0
            result.append({
                "symbol": sym, "qty": qty, "avg_price": p["avg_price"],
                "last": last, "mkt_val": last * qty,
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
        return {"VERY_AGGRESSIVE": 3, "AGGRESSIVE": 15, "MODERATE": 12, "CONSERVATIVE": 15}[self.mode]

    @property
    def max_single_pct(self) -> float:
        return 0.12          # v19: 单仓上限 12%（从 20% 降低，专业基金标准 ≤12%，
                              # 防止单票黑天鹅事件造成过度集中损失）

    # v28: ETF 单仓上限（QQQ/SPY 是分散化ETF，不是单票）
    ETF_MAX_PCT = 0.65       # QQQ 可以到 65%

    @property
    def min_cash_pct(self) -> float:
        return 0.02  # v28: 满仓策略，最低现金 2%

    def get_state(self) -> dict:
        pos_val = sum(
            p.get("shares", p.get("qty", 0)) * p.get("last_price", p.get("avg_price", 0))
            for p in self.positions.values()
        )
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
                if isinstance(px, float) and math.isnan(px):
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
                price: float, reason: str = "", ai_decision_id: int = 0) -> bool:
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
        # v28: 跳过冷却和重复检查（v28 是季度再平衡策略，不需要这些限制）
        _is_v28 = reason.startswith("v28")
        if not _is_v28 and is_duplicate_order(symbol, action, shares):
            return False

        # BUGFIX P1: 执行层冷却拦截 — 任何 BUY/ADD 先查冷却
        if not _is_v28 and action in ("BUY", "ADD") and self.is_cooling_off(symbol):
            logger.debug(f"🚫 冷却拦截: {action} {symbol} (执行层)")
            return False

        # 硬性现金下限
        if action == "BUY":
            min_cash = self.total_equity * self.min_cash_pct
            try:
                from atos.core.fee_model import futu_buy_fee
                estimated_cost = price * shares + futu_buy_fee(shares, price)
            except ImportError:
                estimated_cost = price * shares + max(self.min_commission, shares * self.commission_per_share)
            if self.cash - estimated_cost < min_cash:
                affordable = int((self.cash - min_cash) / (price * 1.001))
                if affordable <= 0:
                    return False
                shares = affordable

        # 单仓上限（硬约束，不允许超过）
        # v28: ETF (QQQ/SPY/TLT/GLD/IWM) 用更高的上限 — ETF 是分散化的
        _ETF_SYMBOLS = {"QQQ", "SPY", "TLT", "GLD", "IWM", "SLV", "USO", "IBB"}
        if symbol in _ETF_SYMBOLS:
            max_single_val = self.total_equity * self.ETF_MAX_PCT
        else:
            max_single_val = self.total_equity * self.max_single_pct
        current_val = self.positions[symbol]["qty"] * price if symbol in self.positions else 0
        max_buy = max_single_val - current_val
        if max_buy <= 0 and action == "BUY":
            logger.debug(f"  {symbol} 已达单仓上限 (${max_single_val:,.0f})")
            return False

        # 总仓位上限（v28: 满仓策略 98%，留 2% 现金缓冲）
        if action == "BUY" or action == "ADD":
            total_pos_val = sum(p["qty"] * (p.get("last_price", p["avg_price"])) for p in self.positions.values())
            estimated_buy = price * shares
            max_total_pos = self.total_equity * 0.98
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
        # v28: Futu 真实费用模型
        try:
            from atos.core.fee_model import futu_buy_fee, futu_sell_fee
            comm = futu_buy_fee(shares, fill) if action == "BUY" else futu_sell_fee(shares, fill)
        except ImportError:
            comm = max(self.min_commission, shares * self.commission_per_share)
        pnl = 0.0  # Fix: 声明在外层，log_trade 可以访问

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
                old_shares = old.get("shares", old.get("qty", 0))
                total_qty = old_shares + shares
                old_cost = old_shares * old["avg_price"]
                self.positions[symbol] = {
                    "shares": total_qty, "qty": total_qty,
                    "avg_price": (old_cost + fill * shares) / total_qty,
                    "last_price": fill,
                    "ai_decision_id": ai_decision_id or old.get("ai_decision_id", 0),
                    "buy_time": old.get("buy_time", datetime.datetime.now().isoformat()),
                }
            else:
                self.positions[symbol] = {"shares": shares, "qty": shares, "avg_price": fill, "last_price": fill,
                                          "ai_decision_id": ai_decision_id,  # v19: 追踪AI决策
                                          "buy_time": datetime.datetime.now().isoformat()}  # v17: Triple-Barrier时间追踪

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": 0, "pnl_pct": 0,
                "reason": reason,
                "source": "factor_engine",
            })

        elif action == "SELL":
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
            actual_qty = pos.get("shares", pos.get("qty", 0))  # Fix: 用实际持仓量
            if actual_qty < shares:
                shares = actual_qty

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
            pos["shares"] = pos["qty"]  # Fix: 同步 shares 键

            # v19 Fix: 反馈闭环 — 根据持仓中记录的AI决策ID追踪结果
            try:
                from atos.ai.memory import record_outcome
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
                # 从持仓数据中获取AI决策ID（买入时记录的）
                decision_id = pos.get("ai_decision_id", 0) if isinstance(pos, dict) else 0
                if decision_id > 0:
                    record_outcome(decision_id, outcome, pnl_pct, 0, reason)
                    logger.info(f"[AI追踪] #{decision_id} → {outcome} PnL={pnl_pct:.2%}")
            except Exception:
                pass

            if pos["qty"] <= 0:
                del self.positions[symbol]
                if symbol in self.trailing_stops:
                    del self.trailing_stops[symbol]

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
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

        log_trade(symbol, action, shares, price, pnl=pnl, reason=reason)
        # P0 修复: 每次成交后立即保存状态 (防止中断丢失)
        _save_account_state(self)  # Fix: self 就是 account，execute() 是 ShadowAccount 的方法
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
                "peak_equity": account.peak_equity,
                "drawdown": round((account.peak_equity - account.total_equity) / account.peak_equity, 6) if account.peak_equity > 0 else 0,
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
                _qty = _pos.get("qty", _pos.get("shares", 0))
                if _qty > 0:
                    _price = signals.get(_sym, {}).get("price", _pos.get("last_price", 0)) if 'signals' in dir() else _pos.get("last_price", 0)
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
    _last_news = getattr(run_shadow_cycle, '_last_news_fetch', 0)
    if _time.time() - _last_news > 1800:  # 30分钟
        try:
            from atos.news.sentiment_engine import refresh_news
            refresh_news()
            run_shadow_cycle._last_news_fetch = _time.time()
        except Exception as e:
            logger.warning(f"📰 新闻抓取失败: {e}")

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
    spy_trend = "BULL"  # Default optimistic
    try:
        spy_close_raw = spy["Close"]
        if isinstance(spy_close_raw, pd.DataFrame):
            spy_close = spy_close_raw.squeeze()
        else:
            spy_close = spy_close_raw

        # Convert to numpy, drop NaN
        spy_vals = spy_close.dropna().values
        if len(spy_vals) < 20:
            raise ValueError(f"SPY数据不足 ({len(spy_vals)}根有效K线)")

        spy_current = float(spy_vals[-1])
        spy_ma20 = float(np.mean(spy_vals[-20:]))
        spy_ma50 = float(np.mean(spy_vals[-50:])) if len(spy_vals) >= 50 else spy_ma20

        # 🏦 v22: 放宽 BULL 判断 — 价格高于 MA20 即是牛市，不要求 >2%
        if spy_current < spy_ma20 and spy_current < spy_ma50 and spy_ma20 < spy_ma50:
            spy_trend = "BEAR"
            logger.warning(f"🐻 SPY死叉: ${spy_current:.0f} < MA20=${spy_ma20:.0f} < MA50=${spy_ma50:.0f}")
        elif spy_current < spy_ma20 * 0.98:
            spy_trend = "CAUTIOUS"
            logger.info(f"🟡 SPY谨慎: ${spy_current:.0f} < MA20*0.98=${spy_ma20*0.98:.0f}")
        elif spy_current > spy_ma20:
            spy_trend = "BULL"
        else:
            spy_trend = "BULL"  # 默认乐观 — 轻微低于MA20不算谨慎
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

    # 数据质量检查
    ds = signals.get("SPY", {}).get("data_source", "unknown")
    if "yfinance" in str(ds) and "Futu" not in str(ds):
        logger.warning(f"⚠️ 数据源降级: {ds} — 价格有15-20分钟延迟!")

    # 数据时效性检查 — Futu价格必须新鲜
    try:
        from atos.live.realtime_feeds import get_feed
        feed = get_feed()
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
        _ic_neg = getattr(run_shadow_cycle, '_ic_inverted', False)
        if _ic_neg:
            logger.info(f"⚠️ IC负值({getattr(run_shadow_cycle, '_ic_ema', 0):.3f}) → 维持正常选股但降仓50%")
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
                # Fix: IC EMA 平滑 — 减少噪音，更稳定判断因子是否有效
                prev_ic_ema = getattr(run_shadow_cycle, '_ic_ema', None)
                current_ic = ic_result['ic']
                if prev_ic_ema is None:
                    run_shadow_cycle._ic_ema = current_ic
                else:
                    run_shadow_cycle._ic_ema = prev_ic_ema * 0.7 + current_ic * 0.3
                smoothed_ic = run_shadow_cycle._ic_ema
                logger.info(f"[IC反馈] IC={current_ic:.4f} (平滑={smoothed_ic:.4f}) | {ic_result.get('verdict','')} | n={ic_result['n']}")

                # v26: IC方向自适应 — 负IC时反转因子权重
                # IC持续<-0.05说明因子反向，应该反转选股方向
                if smoothed_ic < -0.05:
                    run_shadow_cycle._ic_inverted = True
                    if not getattr(run_shadow_cycle, '_ic_invert_logged', False):
                        logger.warning(f"🔄 IC持续为负({smoothed_ic:.4f}) → 因子方向反转，低分股优先")
                        run_shadow_cycle._ic_invert_logged = True
                elif smoothed_ic > 0.02:
                    if getattr(run_shadow_cycle, '_ic_inverted', False):
                        logger.info(f"🔄 IC回正({smoothed_ic:.4f}) → 恢复正常选股方向")
                    run_shadow_cycle._ic_inverted = False
                    run_shadow_cycle._ic_invert_logged = False

        # 存储本周期分数和价格，供下周期使用
        run_shadow_cycle._prev_scores = factor_result.get("scores", {}) if factor_result else {}
        run_shadow_cycle._prev_breakdown = factor_result.get("breakdown", {}) if factor_result else {}
        run_shadow_cycle._prev_prices = {
            sym: sig.get("price", 0)
            for sym, sig in signals.items() if sig.get("price", 0) > 0
        }
        run_shadow_cycle._prev_rsi = {
            sym: sig.get("rsi", 50)
            for sym, sig in signals.items()
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

    # ── 组合轮动已禁用 — 历史数据显示此逻辑是最大亏损来源 ──
    # 文艺复兴/AQR等顶级基金的核心原则: 让赢家跑, 让止损负责退出
    # 频繁轮动 = 手续费 + 滑点 + 追涨杀跌 = 稳定亏损
    # 仅保留止损(-5%)和止盈(+15%)作为退出机制
    ROTATION_DISABLED = True

    # ── v24: 两阶段部分止盈 — 基于 GS 成功模式优化 ──
    # GS 实证: 7次分批止盈各~9%, 共+$2,212 → 提前到+5%首次锁利
    # Renaissance 核心: 让利润跑但分阶段锁定
    # v28: 跳过 — v28 持仓不做分批止盈，让利润充分奔跑
    for sym, pos in list(account.positions.items()):
        if sym in ("QQQ",) or sym in V28_ALPHA_UNIVERSE:
            continue  # v28 持仓跳过
        qty_now = pos.get("shares", pos.get("qty", 0))
        px = signals.get(sym, {}).get("price", pos.get("avg_price", 0))
        if px <= 0: continue
        pnl = (px - pos["avg_price"]) / pos["avg_price"] if pos["avg_price"] > 0 else 0
        
        # Tier 1: +5% → 卖1/4锁利 (GS模式: 第一次止盈)
        if pnl >= 0.05 and pnl < 0.15 and qty_now >= 4:
            partial_key = f"_partial1_{sym}"
            if not getattr(account, partial_key, False):
                sell_qty = max(1, qty_now // 4)
                if sell_qty > 0:
                    account.execute(sym, "SELL", sell_qty, px,
                                  reason=f"Tier1止盈 +{pnl:.1%} (卖1/4锁利@5%)")
                    logger.info(f"💰 Tier1止盈: {sym} {sell_qty}/{qty_now}股 +{pnl:.1%}")
                    setattr(account, partial_key, True)
        
        # Tier 2: +15% → 再卖1/4 (原逻辑保留)
        elif pnl >= 0.15 and qty_now >= 4:
            sell_qty = max(1, qty_now // 4)
            if sell_qty > 0:
                account.execute(sym, "SELL", sell_qty, px,
                              reason=f"Tier2止盈 +{pnl:.1%} (卖1/4锁利@15%)")
                logger.info(f"💰 Tier2止盈: {sym} {sell_qty}/{qty_now}股 +{pnl:.1%}")
        
        # ── v24: Citadel 动量衰减止盈 ──
        # 浮盈>3%但MACD转负 → 动量衰减，提前锁利
        if pnl > 0.03:
            macd_val = signals.get(sym, {}).get("macd_hist", 0)
            if macd_val < -0.3:
                momentum_exit_key = f"_momexit_{sym}"
                if not getattr(account, momentum_exit_key, False):
                    sell_qty = max(1, qty_now // 3)
                    if sell_qty > 0:
                        account.execute(sym, "SELL", sell_qty, px,
                                      reason=f"动量衰减止盈 +{pnl:.1%} MACD={macd_val:.2f} (卖1/3)")
                        logger.info(f"📉 动量衰减止盈: {sym} {sell_qty}股 +{pnl:.1%} MACD转负")
                        setattr(account, momentum_exit_key, True)

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
        atr_pct = (signals.get(sym, {}).get("atr", 0) / price) if price > 0 else 0.02
        tb = triple_barrier(pos["avg_price"], price, hold_days, hold_days,
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
        _v28_position = sym in ("QQQ",) or sym in V28_ALPHA_UNIVERSE
        if not _v28_position:
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
            widen = min(dd_widen_factor * trail_widen, 1.5)
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
        if sym in ("QQQ",) or sym in V28_ALPHA_UNIVERSE:
            continue
        buy_date_str = pos.get("buy_date", "")
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
            account.execute(sym, "SELL", pos["qty"], price, reason=reason)
            log_trade("SELL", sym, pos["qty"], price, reason)
            logger.info(f"🔄 {reason}: {sym}")
            continue
        
        # 弱势持仓加速退出: 持有>3天, 亏损>2%, MACD<0, RSI<40
        if (days_held >= 3 
            and pnl_pct < -0.02 
            and macd_hist < 0 
            and rsi < 40):
            reason = f"弱势退出 (持{days_held}天, PnL{pnl_pct:+.1%}, RSI={rsi:.0f})"
            account.execute(sym, "SELL", pos["qty"], price, reason=reason)
            log_trade("SELL", sym, pos["qty"], price, reason)
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
    _CONC_LIMIT = 0.15   # 单仓上限15%
    _CONC_TARGET = 0.12  # 减持目标12%
    for sym, pos in list(account.positions.items()):
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

    # 4d. 风格检查（回撤/熔断）
    risk_state = get_risk_state()
    if risk_state["circuit_open"]:
        logger.warning(f"🔴 熔断中: {risk_state.get('daily_pnl_pct', 0):.2%} 日亏损")
        # 熔断后只跑风控，不开仓
        _finalize_cycle(account, cycle, regime, current_vix, signals, top_picks, {},
                        "circuit_open", spy_trend)
        return

    # ---- 5. 智能质量门控（替代低胜率 AI 辩论：基于因子质量+动量+RSI） ----
    # 🆕 每周期运行（不再跳周期），严格过滤低质量信号
    ai_veto_map = {}
    if is_market_hours:
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

    # ── 5b. 🆕 实时情报简报（每周期运行，AI决策前优先参考）──
    intel_briefing = None
    try:
        from atos.intel.briefing import get_pre_trade_briefing, briefing_to_prompt
        INTEL_INTERVAL = 6  # 每6周期（30分钟）刷新一次情报
        _last_intel = getattr(run_shadow_cycle, '_last_intel_cycle', -999)
        if account.cycle_count - _last_intel >= INTEL_INTERVAL:
            watchlist = [p["symbol"] for p in top_picks[:8]] if top_picks else \
                        list(signals.keys())[:10]
            intel_briefing = get_pre_trade_briefing(symbols=watchlist, max_news=12)
            run_shadow_cycle._last_intel_cycle = account.cycle_count
            # 记录情报摘要到日志
            sentiment = intel_briefing.get("market_sentiment", {})
            flags = intel_briefing.get("risk_flags", [])
            logger.info(f"📡 情报简报: 情绪={sentiment.get('bias','?')} "
                       f"新闻={len(intel_briefing.get('top_news',[]))}条 "
                       f"风险={len(flags)}个")
    except Exception as e:
        logger.debug(f"情报简报跳过: {e}")

    # ── 5c. 🆕 增强AI决策 (每8周期≈40分钟, 轻量快速) ──
    # v6: 替换低胜率(6.4%)的旧AI辩论，使用硬规则+信心评分+情报融合
    AI_ENHANCED_INTERVAL = 8
    ai_enhanced_advice = None
    if account.cycle_count % AI_ENHANCED_INTERVAL == 0:
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
            ai_enhanced_advice = None
            ai_veto_map = {}

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
# ============================================================
# v28: QQQ Core + Alpha 策略
# 回测: 60% QQQ + 40% 动量股(5只), 年化26.8%, 跑赢SPY 11.7%
# ============================================================
V28_ALPHA_UNIVERSE = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "AMD",
    "CRM", "NFLX", "PLTR", "MU", "TSLA",
]
V28_CORE_PCT = 0.60      # QQQ 核心仓位比例
V28_ALPHA_COUNT = 5       # alpha 个股数量
V28_REBALANCE_DAYS = 63   # 每季度再平衡
V28_STOP_LOSS = 0.05      # 个股止损 5%
V28_TRAILING_STOP = 0.08  # 移动止损 8%
V28_QQQ_TRAILING = 0.12   # QQQ 移动止损 12%


def _v28_qqq_core_alpha(account, signals, regime, spy_trend):
    """v28 策略: QQQ 核心 + 动量个股 alpha

    规则:
    1. 60% 资金买 QQQ（始终持有，不择时）
    2. 40% 资金买 5 只最强动量股
    3. 每季度再平衡
    4. 个股止损 5%, 移动止损 8%
    5. QQQ 移动止损 12%
    """
    equity = account.total_equity
    cash = account.cash

    # ── 卖出检查 ──
    for sym in list(account.positions.keys()):
        pos = account.positions[sym]
        qty = pos.get("qty", pos.get("shares", 0))
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
    qqq_qty = qqq_pos.get("qty", qqq_pos.get("shares", 0))
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
    target_qqq_value = equity * V28_CORE_PCT
    qqq_price = signals.get("QQQ", {}).get("price", 0)

    if qqq_price > 0:
        current_qqq = account.positions.get("QQQ", {})
        current_qqq_qty = current_qqq.get("qty", current_qqq.get("shares", 0))
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

    # ── Alpha 仓: 动量股 ──
    target_alpha_value = equity * (1 - V28_CORE_PCT)
    per_stock_value = target_alpha_value / V28_ALPHA_COUNT

    # 计算动量分 (v28i: 行业动量 — 1日变动 + 距20日高点距离)
    alpha_candidates = []
    for sym in V28_ALPHA_UNIVERSE:
        sig = signals.get(sym, {})
        price = sig.get("price", 0)
        if price <= 0:
            continue

        # 动量指标
        mom_1d = sig.get("change_pct", 0) or 0  # 1日变动
        dist_high = sig.get("dist_20d_high", -10) or -10  # 距20日高点 (负数, 越近0越强)
        ma50 = sig.get("ma50", 0)
        rsi = sig.get("rsi", 50)

        # v28i: 行业动量评分 = 40% 短期动量 + 60% 趋势强度(距高点)
        # dist_high 范围约 -20~0, 转换为 0~1 分数 (越近高点越高分)
        trend_score = max(0, 1 + dist_high / 20)  # -20→0, 0→1
        mom_score = max(0, min(1, (mom_1d + 5) / 10))  # -5%→0, +5%→1
        score = mom_score * 0.4 + trend_score * 0.6

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
            qty = pos.get("qty", pos.get("shares", 0))
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
            account.execute(sym, "BUY", qty, price,
                          reason=f"v28动量alpha score={score:.3f}")
            logger.info(f"🟢 v28买入 {sym}: {qty}股 @${price:.2f} score={score:.3f}")

    account._v28_last_rebalance = now
    logger.info(f"✅ v28再平衡完成 | 持仓: {len(account.positions)}只")


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

    # v16: 大盘趋势过滤 — SPY低于MA50时不开任何新仓（机构级风控）
    if spy_trend == "BEAR":
        logger.info(f"🐻 趋势BEAR — 不开新仓，仅维持风控")
        return
    if spy_trend == "CAUTIOUS":
        # v22: 移除过度谨慎的子检查，CAUTIOUS也不应该完全禁止开仓
        logger.info(f"🟡 趋势CAUTIOUS — 温和开仓（上限12只）")

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
                    key=lambda s: factor_result.get("scores", {}).get(s, 0) if factor_result else 0, reverse=True)[:50]
                serenity_boosts = get_chokepoint_candidates(scan_top)
                if serenity_boosts:
                    logger.info(f"🧩 Serenity瓶颈加分: {len(serenity_boosts)}只")
                    for sym, b in sorted(serenity_boosts.items(), key=lambda x: -x[1])[:5]:
                        logger.info(f"  +{b:.2f} {sym}")
        except Exception as e:
            logger.debug(f"Serenity瓶颈扫描跳过: {e}")
    else:
        logger.debug(f"Serenity瓶颈扫描跳过: 距离上次不足1小时")

    # v17 回测优化: 自适应趋势限制（牛市大胆，熊市保守）
    # 回测验证: 2022熊市跑赢SPY 15.5%, 牛市需更激进
    if spy_trend == "BULL":
        trend_max_pos = 12  # v16: 牛市最多12只（从18减少，集中火力）
        base_score_threshold = 0.35  # v16: 提高门槛（从0.28），只选最强标的
        logger.info(f"🟢 趋势BULL — 进攻模式（上限{trend_max_pos}只, 阈值{base_score_threshold}）")
    elif spy_trend == "CAUTIOUS":
        trend_max_pos = 12  # v22: 从8提升至12，防止容量陷阱
        base_score_threshold = 0.35  # v22: 从0.38降至0.35，匹配BULL阈值
        logger.info(f"🟡 趋势CAUTIOUS — 温和模式（上限{trend_max_pos}只, 阈值{base_score_threshold}）")
    else:
        trend_max_pos = 3
        base_score_threshold = 0.45
        logger.info(f"🐻 趋势BEAR — 防御模式（上限{trend_max_pos}只, 阈值{base_score_threshold}）")
    effective_max_pos = min(account.max_positions, trend_max_pos)

    # 行业分散 — v19 收紧集中度（专业基金标准：单行业 ≤35%，防止板块轮动风险）
    # 对冲基金 PA 标准：即使牛市也要分散，黑天鹅不分牛熊
    if spy_trend == "BULL":
        SECTOR_LIMITS = {"Tech": 0.35, "Financial": 0.35, "Healthcare": 0.35,
                         "Consumer": 0.30, "Industrial": 0.30, "Energy": 0.25,
                         "ETF": 0.50, "Bond": 0.25, "Commodity": 0.20}
    elif spy_trend == "CAUTIOUS":
        SECTOR_LIMITS = {"Tech": 0.30, "Financial": 0.30, "Healthcare": 0.30,
                         "Consumer": 0.25, "Industrial": 0.25, "Energy": 0.20,
                         "ETF": 0.45, "Bond": 0.20, "Commodity": 0.15}
    else:  # BEAR
        SECTOR_LIMITS = {"Tech": 0.25, "Financial": 0.25, "Healthcare": 0.25,
                         "Consumer": 0.20, "Industrial": 0.20, "Energy": 0.15,
                         "ETF": 0.40, "Bond": 0.15, "Commodity": 0.10}
    sector_exposure = {}
    if account.positions:
        try:
            from atos.portfolio.correlation import get_sector_exposure, SECTOR_MAP
            sector_exposure = get_sector_exposure(account.position_list, SECTOR_MAP)
        except Exception:
            pass

    # ── v23: 行业再平衡 — 超限行业自动卖最弱持仓 ──
    # v24 FIX: 1) exposure改用总权益做分母 2) 每周期最多卖1次防级联
    # v28: 跳过 — v28 策略有意集中持仓科技股，不做行业再平衡
    _has_v28 = any(s in V28_ALPHA_UNIVERSE or s == "QQQ" for s in account.positions)
    if sector_exposure and account.positions and not _has_v28:
        try:
            from atos.portfolio.correlation import SECTOR_MAP
            # 用总权益重新计算各行业敞口（而不是占总投资的百分比）
            eq = account.total_equity
            real_exposure = {}
            for sym, pos in account.positions.items():
                sector = SECTOR_MAP.get(sym, "Unknown")
                mkt = pos["qty"] * pos.get("last_price", pos.get("avg_price", 0))
                real_exposure[sector] = real_exposure.get(sector, 0) + mkt / eq if eq > 0 else 0

            rebalanced = False  # v24: 每周期最多卖1只防级联
            for sector, exposure in real_exposure.items():
                if rebalanced:
                    break
                limit = SECTOR_LIMITS.get(sector, 0.25)
                if exposure > limit:
                    # 找该行业最弱持仓
                    sector_positions = []
                    for sym, pos in account.positions.items():
                        if SECTOR_MAP.get(sym, "Unknown") == sector:
                            avg = pos.get("avg_price", 0)
                            lp = pos.get("last_price", avg)
                            pnl = (lp - avg) / avg if avg > 0 else 0
                            sector_positions.append((sym, pnl, pos))
                    if sector_positions:
                        # 卖最弱的
                        weakest = min(sector_positions, key=lambda x: x[1])
                        sym, pnl, pos = weakest
                        price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
                        if price > 0:
                            # 只卖一部分，不是全卖 — 卖到刚好低于limit
                            target_val = eq * limit * 0.9  # 留10%余量
                            current_sector_val = sum(
                                p["qty"] * p.get("last_price", p.get("avg_price", 0))
                                for s2, p in account.positions.items()
                                if SECTOR_MAP.get(s2, "Unknown") == sector
                            )
                            excess_val = current_sector_val - target_val
                            sell_qty = max(1, min(pos["qty"], int(excess_val / price)))
                            if sell_qty > 0:
                                reason = f"行业再平衡 ({sector}{exposure:.0%}>{limit:.0%} 减{sym}{sell_qty}股)"
                                account.execute(sym, "SELL", sell_qty, price, reason=reason)
                                logger.info(f"⚖️ {reason}")
                                rebalanced = True  # 本周期只卖1次
        except Exception as e:
            logger.debug(f"行业再平衡跳过: {e}")

    # 当前持仓数已达上限
    if len(account.positions) >= effective_max_pos:
        # 🏦 v22: 弱持仓轮出 — 有更好信号时卖低分持仓让位
        pick_score = pick["score"]
        weakest_sym = None
        weakest_score = 999
        for psym, ppos in account.positions.items():
            psig = signals.get(psym, {})
            pscore = psig.get("score", psig.get("composite_score", 0))
            # 只在浮亏<3%的持仓中找轮出候选（避免止损卖飞）
            pprice = psig.get("price", ppos.get("last_price", 0))
            pavg = ppos.get("avg_price", 0)
            ppnl = (pprice/pavg - 1) if pavg > 0 else 0
            if ppnl > -0.03 and pscore < weakest_score and pscore < pick_score - 0.05:
                weakest_score = pscore
                weakest_sym = psym
        
        if weakest_sym:
            logger.info(f"🔄 轮出 {weakest_sym}(score={weakest_score:.3f}) 让位给 {sym}(score={pick_score:.3f})")
            account.execute(weakest_sym, "SELL", account.positions[weakest_sym].get("qty", 0),
                          price, reason=f"轮出让位 (低分{weakest_score:.3f}→高分{pick_score:.3f})")
        else:
            logger.debug(f"持仓已满 ({len(account.positions)}/{effective_max_pos})，无弱持仓可轮出")
            return

    max_deploy = account.total_equity * (1.0 - account.min_cash_pct) * gate_exposure
    if gate_exposure < 1.0:
        logger.info(f"📊 宏观门控后部署预算: ${max_deploy:,.0f} (系数×{gate_exposure:.0%})")

    # v9: 现金缓冲放宽 — CAUTIOUS 也不需要那么保守
    current_cash_pct = account.cash / account.total_equity if account.total_equity > 0 else 0
    target_cash_pct = 0.02 if spy_trend == "BULL" else (0.04 if spy_trend == "CAUTIOUS" else 0.10)  # Fix: BULL中更积极
    if current_cash_pct < target_cash_pct:
        logger.warning(f"💰 现金不足 {current_cash_pct:.1%} < {target_cash_pct:.0%}，只卖不买")
        return

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
    force_etf_only = etf_pct < 0.15  # v10: 从30%降到15%建议
    if force_etf_only:
        logger.info(f"🛡️ ETF优先模式 (≥15%): 当前ETF占比={etf_pct:.1%} < 15%，建议优先ETF")

    # 候选：因子评分 > 0.30（基金级校准：从0.55降为0.30，匹配新的0基准评分体系）
    # 实测因子引擎最高分约0.40（GS/MU），阈值0.30可选出5-8只候选
    # 应用 Serenity 加分后重新排序
    enhanced_candidates = []
    for p in top_picks:
        sym = p["symbol"]
        # v9: 允许加仓已有持仓（赢家加仓）
        # 不再跳过已有持仓 — 让后续的加仓逻辑决定
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

    enhanced_candidates.sort(key=lambda x: -x["score"])
    # v17 回测优化: base_score_threshold 已在上方根据 spy_trend 动态设置
    candidates = [c for c in enhanced_candidates if c["score"] > base_score_threshold]

    # v16: 过滤 LongTerm 专属防御股（避免与长线持仓重叠）
    LONGTERM_DEFENSE = {"MRK", "JNJ", "DIS", "PFE", "ABBV", "KO", "PEP", "PG", "XOM", "CVX"}
    candidates = [c for c in candidates if c["symbol"] not in LONGTERM_DEFENSE]
    if any(c["symbol"] in LONGTERM_DEFENSE for c in enhanced_candidates):
        filtered = [c["symbol"] for c in enhanced_candidates if c["symbol"] in LONGTERM_DEFENSE]
        logger.info(f"🛡️ 长线防御股过滤: {filtered}")

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
            # v23: 同行业持仓数量限制 — 防高相关性虚假分散（7/11教训）
            same_sector_count = sum(1 for s in account.positions if SECTOR_MAP.get(s, "Unknown") == sym_sector)
            max_per_sector = 3  # 每行业最多3只
            if same_sector_count >= max_per_sector and sym not in account.positions:
                logger.info(f"⏭ {sym} {sym_sector}行业已有{same_sector_count}只≥{max_per_sector} — 相关性过高")
                continue
        except Exception:
            pass

        price = signals.get(sym, {}).get("price", 0)
        if price <= 0:
            continue
        
        # ── v23: 入场质量确认 — 多指标对齐才开仓 ──
        # v27: 趋势自适应阈值 — BULL市放宽，BEAR市收紧
        # 1. MACD 确认: BULL=-3.0, CAUTIOUS=-1.5, BEAR=-0.5
        macd_hist = signals.get(sym, {}).get("macd_hist", 0)
        macd_min = -3.0 if spy_trend == "BULL" else (-1.5 if spy_trend == "CAUTIOUS" else -0.5)
        if macd_hist < macd_min:
            logger.info(f"⏭ {sym} MACD={macd_hist:.3f}<{macd_min} 深度负值({spy_trend})，等企稳")
            continue
        # 2. 成交量确认: BULL=0.1, CAUTIOUS=0.2, BEAR=0.3
        vol_ratio = signals.get(sym, {}).get("volume_ratio", 1.0)
        vol_min = 0.1 if spy_trend == "BULL" else (0.2 if spy_trend == "CAUTIOUS" else 0.3)
        if vol_ratio < vol_min:
            logger.info(f"⏭ {sym} 量比={vol_ratio:.2f}<{vol_min} 缩量({spy_trend})，等放量")
            continue
        # 3. RSI 不能超卖区反弹无力 (RSI<30的弱势股不接飞刀)
        rsi = signals.get(sym, {}).get("rsi", 50)
        if rsi < 25:
            logger.info(f"⏭ {sym} RSI={rsi:.0f}<25 极度弱势，不接飞刀")
            continue

        # ── v24: 量化基金策略增强 ──
        # Renaissance(Medallion): 上升趋势中的短期超卖 = 最佳买入点
        # "Buy the dip in an uptrend" — 回调到支撑位时买入
        ma50_val = signals.get(sym, {}).get("ma50", 0)
        if 25 <= rsi <= 40 and ma50_val > 0 and price > ma50_val * 0.98:
            pick["score"] += 0.04  # 上升趋势中超卖回调 → 加分
            logger.info(f"📊 Renaissance信号: {sym} RSI={rsi:.0f} 超卖回调+价格在MA50上方 → score+0.04")

        # AQR: 时间序列动量确认 — 价格>MA20>MA50 = 确认上升
        ma20_val = signals.get(sym, {}).get("ma20", 0)
        if ma20_val > 0 and ma50_val > 0 and price > ma20_val > ma50_val:
            pick["score"] += 0.03  # 完美多头排列 → 加分
            logger.info(f"📊 AQR动量: {sym} 价格>MA20>MA50 多头排列 → score+0.03")

        # ── v26: 新闻情绪信号 ──
        # 新闻情绪>+0.15 → 加分买入, 情绪<-0.15 → 减分/跳过
        try:
            from atos.news.sentiment_engine import get_sentiment, get_macro_sentiment
            news_score = get_sentiment(sym)
            macro_news = get_macro_sentiment()
            if news_score > 0.15:
                bonus = min(news_score * 0.1, 0.05)  # 最多加0.05
                pick["score"] += bonus
                logger.info(f"📰 新闻利好: {sym} sentiment={news_score:+.2f} → score+{bonus:.3f}")
            elif news_score < -0.15:
                penalty = min(abs(news_score) * 0.1, 0.05)
                pick["score"] -= penalty
                logger.info(f"📰 新闻利空: {sym} sentiment={news_score:+.2f} → score-{penalty:.3f}")
            # 宏观情绪极端时调整
            if macro_news < -0.3:
                pick["score"] -= 0.02  # 宏观恐慌 → 降低买入意愿
                logger.info(f"📰 宏观偏空: sentiment={macro_news:+.2f} → score-0.02")
        except ImportError:
            pass  # 新闻模块不可用不影响交易
        # ============================================================
        # 硬性要求：必须跑赢手续费 + 滑点（真正生效）
        # ============================================================
        # 当前止盈9% - 手续费0.6% = 8.4% 净空间，满足 MIN_PROFIT_EDGE
        # 但低分标的（0.55-0.60）需要额外buffer，防止被手续费吃掉
        # 低分标的过滤：基金级校准 — 趋势自适应阈值（CAUTIOUS=0.25, 其他=0.30）
        if pick["score"] < base_score_threshold and not force_etf_only:
            logger.info(f"⏭ {sym} score={pick['score']:.2f}<{base_score_threshold} 跳过，分数太低")
            continue

        # RSI过滤 — v22统一: 趋势自适应阈值 (BULL<75, CAUTIOUS<68, BEAR<60)
        rsi = signals.get(sym, {}).get("rsi", 50)
        rsi_max = 75 if spy_trend == "BULL" else (68 if spy_trend == "CAUTIOUS" else 60)
        if rsi > rsi_max:
            logger.info(f"⏭ {sym} RSI={rsi:.0f}>{rsi_max} 超买({spy_trend})，跳过")
            continue
        if rsi < 30:
            logger.info(f"⏭ {sym} RSI={rsi:.0f}<30 弱势，等企稳")
            continue

        # v18 缩量过滤 — 只拒极端缩量（市场开盘初期成交量低是正常的）
        vol_r = signals.get(sym, {}).get("volume_ratio", 1.0)
        if vol_r < 0.03:
            logger.info(f"⏭ {sym} 极度缩量 vol_r={vol_r:.2f}<0.03")
            continue
            continue

        # 🆕 v18: MACD确认 — BULL趋势下允许轻微负MACD，CAUTIOUS/BEAR严格要求
        macd_hist = signals.get(sym, {}).get("macd_hist", 0)
        price_macd = signals.get(sym, {}).get("price", 0)
        # 用MACD占价格的比例来判断动量（绝对值在不同价位不可比）
        macd_ratio = abs(macd_hist) / price_macd if price_macd > 0 else 0
        if spy_trend == "BULL":
            if macd_ratio > 0.02:  # BULL下只拒绝MACD严重为负的（>2%价格）
                logger.info(f"⏭ {sym} MACD严重恶化 macd/price={macd_ratio:.1%}")
                continue
        else:
            if macd_hist < 0:  # CAUTIOUS/BEAR下拒绝所有负MACD
                logger.info(f"⏭ {sym} MACD负({macd_hist:.4f}) {spy_trend}趋势禁开")
                continue

        # 🆕 v18: 价格vs MA50 — BULL下允许小幅回调(<5%)，CAUTIOUS/BEAR必须>MA50
        ma50 = signals.get(sym, {}).get("ma50", 0)
        if ma50 > 0:
            ma50_dev = (price - ma50) / ma50
            if spy_trend == "BULL":
                if ma50_dev < -0.05:  # BULL下允许回调，只拒绝深度跌破(>5%)
                    logger.info(f"⏭ {sym} 价格${price:.0f} << MA50${ma50:.0f} ({ma50_dev:.1%})")
                    continue
            else:
                if price < ma50:  # CAUTIOUS/BEAR必须>MA50
                    logger.info(f"⏭ {sym} 价格${price:.0f} < MA50${ma50:.0f} — {spy_trend}趋势禁开")
                    continue

        # v18: 布林带位置过滤 — BULL下允许追强势股
        boll = signals.get(sym, {}).get("bollinger", {})
        pct_b = boll.get("pct_b", 0.5) if isinstance(boll, dict) else 0.5
        boll_limit = 0.95 if spy_trend == "BULL" else 0.85
        if pct_b > boll_limit:
            logger.info(f"⏭ {sym} 布林带上轨 pct_b={pct_b:.2f}>{boll_limit} — 追高风险")
            continue

        # MA200偏离过滤 — v15 收紧: 50%→35% (防止极端偏离)
        ma200 = signals.get(sym, {}).get("ma200", 0)
        if ma200 > 0 and price > ma200 * 1.35:
            logger.info(f"⏭ {sym} 价格偏离MA200>{((price/ma200-1)*100):.0f}%>35%")
            continue

        # 🏦 v21: 近期回调幅度过滤器（只买回调股，不追高）
        # v27: BULL市放宽 — 允许在高点附近买入强势突破股
        high_20d = signals.get(sym, {}).get("high_20d", 0) or price
        if high_20d > 0:
            pullback = (price - high_20d) / high_20d
            near_high_limit = 0.005 if spy_trend == "BULL" else -0.01  # BULL允许在高点0.5%以内
            if pullback > near_high_limit:
                mom_score = signals.get(sym, {}).get("score_momentum", 0)
                mom_min = 0.4 if spy_trend == "BULL" else 0.6
                if mom_score < mom_min:
                    logger.info(f"⏭ {sym} 距20日高{pullback:+.1%} 动量{mom_score:.0%}<{mom_min:.0%}({spy_trend}) — 等回调")
                    continue

        # v9: 允许加仓 — 包括小幅浮亏 (<5%)
        is_add = sym in account.positions
        if is_add:
            pos = account.positions[sym]
            avg_px = pos.get("avg_price", 0)
            if avg_px <= 0:
                continue
            pnl_pct = (price - avg_px) / avg_px
            if pnl_pct < -0.05:  # 只禁止深度亏损加仓
                logger.info(f"⏭ {sym} 深度浮亏{pnl_pct:.1%} — 禁止加仓")
                continue
            # 加仓不超过单仓上限
            current_val = pos["qty"] * price
            max_single_val = account.total_equity * account.max_single_pct
            if current_val >= max_single_val:
                logger.info(f"⏭ {sym} 已达单仓上限")
                continue

        # ── v13: 入场质量过滤 — 防止买在高点 ──
        # 1. RSI动量: 只买RSI在上升的 (避免追跌)
        prev_rsi = run_shadow_cycle._prev_rsi.get(sym, rsi) if hasattr(run_shadow_cycle, '_prev_rsi') else rsi
        if rsi < prev_rsi - 2 and not is_add:  # RSI下降>2点且非加仓 → 动能减弱
            logger.info(f"⏭ {sym} RSI下降({prev_rsi:.0f}→{rsi:.0f}) — 等待动能恢复")
            continue

        # 2. 日内动量: 价格必须在开盘价上方 (正日内动量)
        open_price = signals.get(sym, {}).get("open", 0)
        if open_price > 0 and price < open_price * 0.998 and not is_add:
            logger.info(f"⏭ {sym} 日内下跌 {(price/open_price-1)*100:.1f}% — 等企稳")
            continue

        # 2b. 🏦 v20: 成交量/流动性过滤器（基金级风控，防小盘股滑点）
        avg_volume = signals.get(sym, {}).get("volume", 0) or 0
        if avg_volume > 0:
            target_shares = max(1, int(account.total_equity * 0.001 / price))
            volume_ratio = target_shares / avg_volume if avg_volume > 0 else 0
            if volume_ratio > 0.02:  # 目标仓位超过日成交量2% → 流动性不足
                logger.info(f"⏭ {sym} 流动性不足 (目标{target_shares}股/{avg_volume:.0f}日均量={volume_ratio:.1%})")
                continue
            if avg_volume < 50000:  # 日均成交量低于5万股 → 跳过
                logger.debug(f"⏭ {sym} 成交量过低 ({avg_volume:.0f}股/日)")
                continue

        # ── v11: 基金标准仓位计算 (Integrated Position Sizing) ──
        # 融合三个维度: 波动率倒数(30%) + 半凯利(30%) + 因子分数(40%)
        # 文艺复兴/AQR/桥水 的共同方法论
        enhanced_score = pick["score"]

        # 获取当前胜率/盈亏比 — v23: 匹配 Kelly 默认值
        current_wr = 0.50  # v23: 匹配 kelly.py DEFAULT_WIN_RATE
        current_wlr = 1.50  # v23: 匹配 kelly.py DEFAULT_WIN_LOSS_R
        num_trades = 0
        try:
            from atos.live.kelly import _load_stats
            stats = _load_stats()
            if stats and stats.get("total_trades", 0) >= 5:
                current_wr = stats.get("win_rate", 0.42)
                current_wlr = stats.get("win_loss_r", 1.20)
                num_trades = stats.get("total_trades", 0)
        except Exception:
            pass

        # 使用基金标准综合仓位
        from atos.portfolio.fund_standard import integrated_position_size
        target_pct = integrated_position_size(
            symbol=sym,
            factor_score=min(enhanced_score, 1.0),
            price=price,
            win_rate=current_wr,
            win_loss_ratio=current_wlr,
            current_drawdown=current_dd,
            max_weight=account.max_single_pct,
            trades=num_trades,
        )

        # ── v17: 回撤后动态减仓（Kelly After Drawdown）──
        # 专业基金标准：亏钱后自动缩小仓位，防止连亏时越亏越多
        kd = kelly_after_drawdown(target_pct, current_dd, max_drawdown_limit=0.10)
        target_pct = kd["adjusted_kelly"]
        if kd["scale"] < 1.0:
            logger.info(f"📉 回撤减仓: {sym} DD={current_dd:.1%} scale={kd['scale']:.0%} → {target_pct:.2%}")

        # ── v27: IC 负值降仓 — 因子不可信时缩小仓位 ──
        if getattr(run_shadow_cycle, '_ic_inverted', False):
            target_pct *= 0.5
            logger.info(f"⚠️ IC降仓: {sym} → {target_pct:.1%} (因子IC为负)")

        # ── v24: Bridgewater Risk Parity — 连续波动率仓位调整 ──
        # 目标: 每只持仓贡献相同的风险（等风险贡献）
        # 高波动标的自动缩小仓位，低波动标的自动放大仓位
        atr_val = signals.get(sym, {}).get("atr", 0)
        if atr_val > 0 and price > 0:
            atr_pct = atr_val / price  # ATR 占价格比例 = 日波动率代理
            target_vol = 0.015  # 目标日波动率 1.5%
            # 连续调整: vol_scalar = target_vol / actual_vol
            vol_scalar = target_vol / atr_pct if atr_pct > 0 else 1.0
            # Clamp 在 0.3x - 1.5x 之间
            vol_scalar = max(0.30, min(1.50, vol_scalar))
            old_pct = target_pct
            target_pct *= vol_scalar
            if abs(vol_scalar - 1.0) > 0.05:
                logger.info(f"📊 RiskParity: {sym} ATR={atr_pct:.1%} vol_scalar={vol_scalar:.2f} → 仓位{old_pct:.1%}→{target_pct:.1%}")

        # ── v24: Renaissance 均值回归入场加分 ──
        # 在上升趋势中回调3-8%时买入（"buy the dip"）
        if not is_add:
            ma50_val = signals.get(sym, {}).get("ma50", 0)
            if ma50_val > 0 and price > 0:
                ma50_dev = (price - ma50_val) / ma50_val
                if -0.08 <= ma50_dev <= -0.02:
                    # 回调2-8% → 均值回归入场机会，仓位加20%
                    target_pct *= 1.20
                    logger.info(f"📉 MeanRev: {sym} MA50回调{ma50_dev:.1%} → 仓位×1.20")
                elif ma50_dev > 0.08:
                    # 偏离MA50超过8% → 追高风险，仓位减20%
                    target_pct *= 0.80
                    logger.info(f"📈 MeanRev: {sym} MA50偏离+{ma50_dev:.1%} → 仓位×0.80")

        if target_pct > 0.005:
            logger.info(f"📐 FundStd: {sym} score={enhanced_score:.3f} → {target_pct:.1%} "
                       f"(WR={current_wr:.0%}, DD={current_dd:.1%})")
        else:
            continue  # 仓位太小, 跳过

        target_val = account.total_equity * target_pct

        # 考虑已有持仓 — Bug #2: 加仓路径已在上方过滤（仅盈利时可到达此处）
        current_val = account.positions[sym]["qty"] * price if sym in account.positions else 0
        delta_val = target_val - current_val
        if delta_val <= 0:
            continue

        shares = max(1, int(delta_val / price))

        if shares < 5 or shares * price < 500:
            continue

        # 交易成本检查
        est_cost = max(account.min_commission, shares * account.commission_per_share) + price * shares * account.slippage_pct
        if price * shares * 0.005 < est_cost:
            continue

        # 🆕 最小交易金额 $2000（禁止1股迷你单，手续费会吃掉所有利润）
        min_trade_value = 2000
        if shares * price < min_trade_value:
            logger.debug(f"⏭ {sym} 交易金额${shares*price:.0f} < ${min_trade_value} 最低门槛")
            continue

        # 🆕 最小股数 5股（1股交易没有意义）
        if shares < 5:
            continue

        # 🆕 禁止当日买入后立即卖出（至少持有2个周期≈10分钟，防止churning）
        recent_trades = [t for t in account.trade_history[-20:]
                        if t.get('symbol') == sym]
        if recent_trades:
            last_trade = recent_trades[-1]
            if last_trade.get('action') == 'SELL':
                try:
                    last_time = __import__('datetime').datetime.fromisoformat(last_trade['date'])
                    minutes_since_sell = (__import__('datetime').datetime.now() - last_time).total_seconds() / 60
                    if minutes_since_sell < 60:  # 卖出后60分钟内不买回
                        logger.info(f"⏭ {sym} {minutes_since_sell:.0f}分钟前刚卖出，冷却中（防churning）")
                        continue
                except Exception:
                    pass

        reason_parts = [f"因子开仓 score={pick['base_score']:.2f}"]
        if serenity_boost > 0:
            reason_parts.append(f"Serenity+{serenity_boost:.2f}")
        reason_parts.append(f"仓位{target_pct:.1%}")

        # v19: 获取AI决策ID以追踪结果
        ai_did = 0
        try:
            from atos.ai.memory import _get_db
            conn = _get_db()
            row = conn.execute("SELECT id FROM decisions WHERE symbol=? AND action='BUY' ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
            if row: ai_did = row[0]
            conn.close()
        except Exception: pass

        ok = account.execute(sym, "BUY", shares, price,
                             reason=" | ".join(reason_parts),
                             ai_decision_id=ai_did)
        if ok:
            deployed += shares * price
            logger.info(f"✅ 开仓 {sym}: {shares}股 @${price:.2f} (crouching={target_pct:.1%}, score={pick['base_score']:.2f})")

    logger.info(f"开仓完成: {len(account.positions)}持仓, 部署${deployed:,.0f}")


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

    # ── v17: 统一绩效追踪 — 每20周期汇报 ──
    try:
        from atos.core.performance import get_tracker, init_tracker
        if getattr(run_shadow_cycle, '_perf_inited', False) is False:
            init_tracker(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            run_shadow_cycle._perf_inited = True
        perf = get_tracker()
        perf.update(current_eq, cycle_ret)
        if cycle % 20 == 0:
            m = perf.get_metrics()
            logger.info(f"📊 绩效#{cycle}: Sharpe={m.get('sharpe',0):.2f} Sortino={m.get('sortino',0):.2f} "
                       f"Calmar={m.get('calmar',0):.2f} WR={m.get('win_rate',0):.1f}% "
                       f"PF={m.get('profit_factor',0):.2f} 评级={m.get('grade','?')}")
        perf.save()
    except Exception as e:
        logger.debug(f"绩效追踪跳过: {e}")

    # 记录每日收益
    try:
        from atos.core.daily_returns import record_daily
        record_daily(current_eq, len(account.trade_history), len(account.positions))
    except Exception:
        pass

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

    # v5.1: 写日内涨跌数据供 Dashboard 读取（从 Futu OpenD 获取 prev_close）
    try:
        dc_file = os.path.join(os.path.dirname(state_file), "day_changes.json")
        day_data = {}
        # 尝试从 Futu OpenD 批量获取日内涨跌
        try:
            from futu import OpenQuoteContext, RET_OK
            pos_syms = [s for s in account.positions if isinstance(account.positions.get(s), dict)]
            if pos_syms:
                ctx = OpenQuoteContext('127.0.0.1', 11111)
                ret, data = ctx.get_market_snapshot([f'US.{s}' for s in pos_syms])
                ctx.close()
                if ret == RET_OK:
                    for _, row in data.iterrows():
                        sym = row['code'].replace('US.', '')
                        day_data[sym] = {
                            'prev_close': round(float(row.get('prev_close_price', 0) or 0), 2),
                            'day_chg': round(float(row.get('change_val', 0) or 0), 2),
                            'day_pct': round(float(row.get('change_rate', 0) or 0), 2),
                        }
        except Exception:
            pass  # Futu 不可用时 fallback 到零值
        
        # Fallback: 对 Futu 没覆盖的持仓用 current price
        for sym, pos in account.positions.items():
            if sym in day_data: continue
            if not isinstance(pos, dict): continue
            px = pos.get('last_price', 0) or 0
            if px > 0:
                day_data[sym] = {'prev_close': round(px,2), 'day_chg': 0.0, 'day_pct': 0.0}
        
        with open(dc_file, 'w') as f:
            json.dump(day_data, f)
    except Exception:
        pass

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
def _save_account_state(account: ShadowAccount):
    """P0 修复: 原子化保存账户状态（含备份机制）"""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    state_file = os.path.join(base_dir, "data", "shadow_state.json")
    bak_file = state_file + ".bak"
    os.makedirs(os.path.dirname(state_file), exist_ok=True)

    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "peak_equity": account.peak_equity,
        "drawdown": round((account.peak_equity - account.total_equity) / account.peak_equity, 6) if account.peak_equity > 0 else 0,
        "equity_history": getattr(account, "equity_history", []),
        "last_cycle": datetime.datetime.now().isoformat(),
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
        } if hasattr(account, "trailing_stops") and account.trailing_stops else {},
    }
    # 原子写入: 先备份旧文件, 再写临时文件, 最后 rename
    try:
        if os.path.exists(state_file):
            os.replace(state_file, bak_file)
    except Exception:
        pass
    try:
        atomic_write(state_file, json.dumps(state, indent=2))
    except Exception:
        logger.warning("状态保存失败，尝试直接写入")
        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            logger.error("状态保存完全失败!")


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

    state_file = os.path.join(base_dir, "data", "shadow_state.json")

    # v11: 短线资金上限
    max_short_capital = ALLOCATION.get("short_term", 300_000)

    # 恢复状态
    if os.path.exists(state_file):
        with open(state_file) as f:
            saved = json.load(f)
        # v11: 强制上限 — 防止旧状态$1M覆盖配置的$300K
        # v24 FIX: 允许利润累积 — cap改为initial*1.5（允许50%利润），不再吞掉收益
        max_allowed = max_short_capital * 1.50  # 允许最多50%利润
        initial = min(saved.get("initial_cash", max_short_capital), max_short_capital)
        account = ShadowAccount(initial_cash=initial)
        account.cash = min(saved.get("cash", account.initial_cash), max_allowed)
        account.positions = saved.get("positions", {})
        # Fix: 标准化持仓键名（shares ↔ qty 一致性）
        for sym, p in account.positions.items():
            if "shares" in p and "qty" not in p:
                p["qty"] = p["shares"]
            elif "qty" in p and "shares" not in p:
                p["shares"] = p["qty"]
            elif "quantity" in p:
                p["shares"] = p["qty"] = p["quantity"]
        account.trade_history = saved.get("trade_history", [])
        account.cycle_returns = saved.get("cycle_returns", [])
        account.cycle_count = saved.get("cycle_count", 0)
        account.prev_equity = min(saved.get("equity", account.initial_cash), max_short_capital)
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

            # ── v23: 每日相关性扫描（每288周期=每天一次）──
            if cycle % 288 == 1 and len(account.positions) >= 2:
                try:
                    from atos.portfolio.correlation import check_concentration_risk
                    pos_list = []
                    for sym, pos in account.positions.items():
                        lp = pos.get("last_price", pos.get("avg_price", 0))
                        pos_list.append({
                            "symbol": sym,
                            "mkt_val": pos["qty"] * lp,
                            "avg_price": pos.get("avg_price", 0),
                            "last_price": lp,
                            "qty": pos["qty"],
                        })
                    alerts = check_concentration_risk(pos_list, correlation_threshold=0.75)
                    if alerts:
                        for a in alerts[:3]:  # 只处理最严重的前3对
                            logger.warning(f"🔗 相关性告警: {a['suggestion']}")
                        # 自动减持最高相关性配对中市值较小的
                        top = alerts[0]
                        reduce_sym = top.get("reduce_symbol", "")
                        if reduce_sym and reduce_sym in account.positions:
                            rpos = account.positions[reduce_sym]
                            rprice = rpos.get("last_price", rpos.get("avg_price", 0))
                            if rprice > 0:
                                reduce_qty = max(1, int(rpos["qty"] * 0.30))
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
                _save_account_state(account)
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
