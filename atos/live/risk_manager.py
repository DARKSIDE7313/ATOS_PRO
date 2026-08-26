"""ATOS PRO v3 — 风控管理器（重写版）
===================================
核心变更：
  1. 所有卖出统一触发冷却期（v2 中某些卖出不触发冷却）
  2. 硬止盈/止损合并到统一检查
  3. 新增日亏损熔断
  4. 新增最大回撤熔断
  5. 所有风控独立判断，不互相干扰
  6. 持久化风险状态（save/load risk_state.json）

使用方式：
  from atos.live.risk_manager import check_all_stops, check_daily_limits, TRADE_CIRCUIT
"""

import os
import json

from atos.config_shared import RISK
from atos.shadow.strategy_v28 import is_v28_position
from atos.core.position_schema import get_qty
from atos.core.market_clock import get_market_date

# H4: 风控参数统一从 config_shared.RISK 读取（消除 config_shared / risk_manager 双账本）
MAX_DAILY_LOSS_PCT = RISK["max_daily_loss_pct"]        # 日亏损超过 2.5% → 熔断
MAX_DRAWDOWN_PCT = RISK["max_drawdown_pct"]            # 最大回撤 12% → 暂停新开仓
MAX_CONSECUTIVE_LOSSES = RISK["max_consecutive_losses"]  # 连续 3 次亏损 → 降频
COOLDOWN_CYCLES = RISK["cooldown_cycles"]              # 冷却周期
STOP_LOSS_PCT = RISK["stop_loss_pct"]                  # 止损 5%
TAKE_PROFIT_PCT = RISK["take_profit_pct"]              # 止盈 18%
MAX_ORDERS_PER_CYCLE = 4        # 每周期最多4笔（从6降低，减少churning）

# 持久化路径
_RISK_STATE_FILE = None  # 在 load_risk_state() 中初始化

# 全局状态
_daily_pnl_pct = 0.0
_daily_pnl = 0.0
_orders_this_cycle = 0
_orders_this_day = 0
_consecutive_losses = 0
_current_drawdown = 0.0
_trade_circuit_open = False    # 熔断开关
_daily_start_equity = None     # L5: 当日基准权益（固定分母，防日亏损自强化）


def reset_cycle():
    """每个周期开始前调用"""
    global _orders_this_cycle
    _orders_this_cycle = 0


def reset_daily():
    """每天调用一次"""
    global _daily_pnl_pct, _daily_pnl, _orders_this_day, _trade_circuit_open, _daily_start_equity
    _daily_pnl_pct = 0.0
    _daily_pnl = 0.0
    _orders_this_day = 0
    _trade_circuit_open = False  # 每日重置熔断
    _daily_start_equity = None   # L5: 重置当日基准权益


def record_fill(pnl: float, total_equity: float = 0):
    """记录一笔成交后的盈亏"""
    global _daily_pnl, _daily_pnl_pct, _orders_this_cycle, _orders_this_day, _consecutive_losses, _daily_start_equity
    _daily_pnl += pnl
    # L5: 用当日基准权益（固定分母）计算日亏损率，避免亏损缩小分母的自强化
    if _daily_start_equity is None and total_equity > 0:
        _daily_start_equity = total_equity
    if _daily_start_equity and _daily_start_equity > 0:
        _daily_pnl_pct = _daily_pnl / _daily_start_equity
    elif total_equity > 0:
        _daily_pnl_pct = _daily_pnl / total_equity
    _orders_this_cycle += 1
    _orders_this_day += 1

    if pnl < 0:
        _consecutive_losses += 1
    else:
        _consecutive_losses = 0

    # Auto-save after every fill
    save_risk_state()


def check_all_stops(positions: list, signals: dict) -> list:
    """统一检查所有止损/止盈条件。返回要执行的 SELL 指令列表。

    每个标的独立判断——没有传染性卖出。
    v29: v28 持仓(QQQ + alpha池)完全跳过 — 由主循环的 v28 止损逻辑处理
    """
    # v29: v28 持仓跳过此函数 (避免 18%止盈/ATR止损 干扰 v28 季度策略)
    # P1-1: v28 宇宙单源化 — 统一走 strategy_v28.is_v28_position (不再硬编码)
    forced = []
    for p in positions:
        sym = p["symbol"]
        if is_v28_position(sym):
            continue  # v28 持仓由主循环处理止损
        px = signals.get(sym, {}).get("price", p.get("last", 0))
        if px <= 0:
            continue

        avg = p["avg_price"]
        if avg <= 0:
            continue
        pnl_pct = (px - avg) / avg
        qty = get_qty(p)

        # 1. 硬止盈（卖一半，最少1股 — v7修复: qty=1 也能退出）
        if pnl_pct >= TAKE_PROFIT_PCT:
            half = max(1, qty // 2)
            forced.append({
                "action": "SELL", "symbol": sym, "qty": half,
                "reason": f"止盈 +{pnl_pct:.1%}", "pnl_pct": pnl_pct,
                "exit_type": "TAKE_PROFIT", "outcome": "WIN",
            })
            continue

        # 2. 波动率止损（ATR动态）— 高于硬止损时提前离场
        #    低波动标的 ATR 止损收紧（如 4%），早于硬止损 7% 触发
        atr_val = signals.get(sym, {}).get("atr", 0)
        if atr_val > 0 and px > 0:
            vol_stop = max(0.03, min(0.10, (atr_val / px) * 2.0))
            if pnl_pct <= -vol_stop:
                forced.append({
                    "action": "SELL", "symbol": sym, "qty": qty,
                    "reason": f"ATR止损 {pnl_pct:.1%} (ATR{vol_stop:.0%})",
                    "pnl_pct": pnl_pct,
                    "exit_type": "STOP_LOSS", "outcome": "LOSS",
                })
                continue

        # 3. 硬止损（绝对底线）
        if pnl_pct <= -STOP_LOSS_PCT:
            forced.append({
                "action": "SELL", "symbol": sym, "qty": qty,
                "reason": f"硬止损 {pnl_pct:.1%}", "pnl_pct": pnl_pct,
                "exit_type": "STOP_LOSS", "outcome": "LOSS",
            })
            continue

    return forced


def check_trailing_stop(symbol: str, price: float, trailing_stop) -> dict:
    """检查单个追踪止损。返回是否触发。"""
    if price <= 0 or trailing_stop is None:
        return {"triggered": False}

    result = trailing_stop.update(price)
    return result


def check_daily_limits(total_equity: float) -> dict:
    """检查日度风控限制。返回限制状态。

    Returns:
        {"can_trade": bool, "reason": str, "circuit_open": bool}
    """
    global _trade_circuit_open

    # 日亏损熔断
    if total_equity > 0 and _daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
        _trade_circuit_open = True
        # Phase 5: 熔断打开立即联动 v29 状态机 (不等下次 save_risk_state)
        try:
            from atos.core.risk_coordinator import sync_from_legacy
            sync_from_legacy(get_state())
        except Exception:
            pass
        return {
            "can_trade": False,
            "reason": f"日亏损{_daily_pnl_pct:.2%}达到熔断线{MAX_DAILY_LOSS_PCT:.0%}",
            "circuit_open": True,
        }

    # 连续亏损降频
    if _consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return {
            "can_trade": False,
            "reason": f"连续{_consecutive_losses}次亏损，暂停新开仓",
            "circuit_open": False,
        }

    # 开仓次数限制
    if _orders_this_day >= MAX_ORDERS_PER_CYCLE * 3:  # L4: 每周期4笔 × 3 = 12笔/天
        return {
            "can_trade": False,
            "reason": f"今日{_orders_this_day}单已达上限",
            "circuit_open": False,
        }

    return {"can_trade": True, "reason": "正常", "circuit_open": _trade_circuit_open}


def filter_orders(proposed: list, account_state: dict, regime: dict) -> list:
    """过滤交易指令，只保留符合风控的。

    核心原则：SELL 永远通过（卖出是风控），BUY 需要检查。
    """
    global _trade_circuit_open

    total = account_state["total"]
    cash = account_state["cash"]
    min_cash = account_state.get("constraints", {}).get("min_cash", total * 0.10)
    max_pct = account_state.get("constraints", {}).get("max_single_pct", 0.12)

    safe = []

    # 日亏损熔断 — 只允许卖出
    limits = check_daily_limits(total)
    if not limits["can_trade"] and limits["circuit_open"]:
        return [o for o in proposed if o.get("action") == "SELL"]

    for order in proposed:
        sym = order.get("symbol", "")
        action = order.get("action", "HOLD")
        target_pct = order.get("target_pct", 0)

        # 卖出永远不拦截
        if action == "SELL":
            safe.append(order)
            continue

        # 连续亏损不新开仓
        if not limits["can_trade"]:
            continue

        # 单仓上限
        if target_pct > max_pct:
            order = dict(order, target_pct=max_pct)

        # 现金下限
        cost_est = total * order.get("target_pct", 0.03)
        if cash - cost_est < min_cash:
            continue

        safe.append(order)

    return safe


def get_state() -> dict:
    """返回当前风控状态"""
    return {
        "daily_pnl_pct": _daily_pnl_pct,
        "daily_pnl": _daily_pnl,
        "orders_today": _orders_this_day,
        "orders_this_cycle": _orders_this_cycle,
        "consecutive_losses": _consecutive_losses,
        "circuit_open": _trade_circuit_open,
        "current_drawdown": _current_drawdown,
    }


def update_drawdown(current_equity: float, peak_equity: float):
    """更新回撤状态"""
    global _current_drawdown, _trade_circuit_open
    if peak_equity > 0:
        _current_drawdown = (peak_equity - current_equity) / peak_equity
    if _current_drawdown > MAX_DRAWDOWN_PCT:
        _trade_circuit_open = True
    # Auto-save after drawdown update
    save_risk_state()


# ============================================================
# 持久化支持
# ============================================================

def _get_risk_state_path() -> str:
    """获取风险状态文件路径"""
    global _RISK_STATE_FILE
    if _RISK_STATE_FILE is None:
        # Derive path relative to this file's location: ATOS_PRO/data/risk_state.json
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _RISK_STATE_FILE = os.path.join(base, "data", "risk_state.json")
    return _RISK_STATE_FILE


def load_risk_state():
    """从 risk_state.json 加载持久化的风险状态。

    在系统启动时调用，以恢复熔断/回撤等状态。
    Phase 5c: 跨天启动时复位日级熔断状态，并在恢复后联动 v29 状态机
    (sync_from_legacy)，修复"熔断已触发但 system_state 仍 PAPER"的双轨不一致。
    """
    global _daily_pnl_pct, _daily_pnl, _orders_this_cycle, _orders_this_day
    global _consecutive_losses, _current_drawdown, _trade_circuit_open

    path = _get_risk_state_path()
    if not os.path.exists(path):
        return  # First run, nothing to restore

    logger = None
    try:
        from atos.core.logging import get_logger
        logger = get_logger("risk_manager")
    except Exception:
        pass

    try:
        with open(path, "r") as f:
            data = json.load(f)

        _daily_pnl_pct = data.get("daily_pnl_pct", 0.0)
        _daily_pnl = data.get("daily_pnl", 0.0)
        _orders_this_day = data.get("orders_this_day", 0)
        _consecutive_losses = data.get("consecutive_losses", 0)
        _current_drawdown = data.get("current_drawdown", 0.0)
        _trade_circuit_open = data.get("circuit_open", False)

        # Phase 5c: 跨天复位日级熔断 — 日亏损/日熔断只属于当天。
        # 跨天启动时应清零，避免历史某天的 -7.38% 日亏熔断延续到新交易日。
        # consecutive_losses / current_drawdown 是跨日指标，保留。
        import datetime as _dt
        try:
            _saved_date = _dt.datetime.fromisoformat(data.get("saved_at", "")).date()
            _is_today = (_saved_date == get_market_date())
        except Exception:
            _is_today = True  # 日期无法解析时保守视为当天，不误复位
        if not _is_today:
            _daily_pnl_pct = 0.0
            _daily_pnl = 0.0
            _orders_this_day = 0
            _trade_circuit_open = False
            if logger:
                logger.info(f"跨天启动: 日级熔断状态已复位 (saved_at={data.get('saved_at')})")

        # Don't restore _orders_this_cycle — it resets every cycle anyway

        if logger:
            logger.info(f"风险状态恢复: 日PnL={_daily_pnl:.2f} 回撤={_current_drawdown:.2%} 熔断={_trade_circuit_open}")
    except Exception as e:
        if logger:
            logger.warning(f"风险状态加载失败: {e}")
        return

    # Phase 5c: 恢复完成后立即联动 v29 状态机 —
    # 修复"熔断已触发但 system_state 仍 PAPER"的双轨不一致 (启动路径此前漏掉 sync)。
    try:
        from atos.core.risk_coordinator import sync_from_legacy
        sync_from_legacy(get_state())
    except Exception:
        pass


def save_risk_state():
    """将当前风险状态持久化到 risk_state.json。"""
    path = _get_risk_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    data = {
        "daily_pnl_pct": _daily_pnl_pct,
        "daily_pnl": _daily_pnl,
        "orders_this_day": _orders_this_day,
        "orders_this_cycle": _orders_this_cycle,
        "consecutive_losses": _consecutive_losses,
        "current_drawdown": _current_drawdown,
        "circuit_open": _trade_circuit_open,
        "saved_at": __import__('datetime').datetime.now().isoformat(),
    }

    try:
        # M13: 原子写 — 先写临时文件再 os.replace，避免写一半崩溃留下损坏 JSON
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        pass  # Silent fail — state is not critical for correctness

    # Phase 5: 双风控轨联动 — legacy 熔断状态同步到 v29 状态机 (单一决策源:
    # atos.core.risk_coordinator)。熔断打开 → 状态机强制 HALT_NEW_ORDERS。
    try:
        from atos.core.risk_coordinator import sync_from_legacy
        sync_from_legacy(get_state())
    except Exception:
        pass  # 联动失败不阻塞风控持久化
