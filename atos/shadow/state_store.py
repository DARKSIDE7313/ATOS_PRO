"""
ATOS PRO — Shadow 状态持久化单一模块 (Phase 5 框架重塑)
==========================================================
历史问题: shadow_state.json 的 state dict 在 shadow_trader.py 中
**4 处重复构造** (_finalize_cycle / _save_account_state / main 的紧急停止
/ main 的最终保存)，字段不完全一致 (报告 §7.2: 紧急停止缺 activation_price)。

本模块是 shadow_state.json 平铺 schema (报告 §8.2) 的 **唯一构造/写入/读取点**:

  {"initial_cash", "cash", "positions", "trade_history", "cycle_returns",
   "cycle_count", "equity", "peak_equity", "drawdown", "equity_history",
   "last_cycle", "stop_loss_blacklist", "strategy_decay_factor", "trailing_stops",
   (紧急保存时附加) "stopped_at", "reason"}

所有写入都是原子写 (atomic_write)。账户每次成交后立即保存 (P0 修复语义保留)。
"""

import os
import json
import datetime

from atos.core.logging import get_logger
from atos.debugger.safety_net import atomic_write

logger = get_logger("shadow_state")


def get_base_dir() -> str:
    """ATOS_PRO 根目录"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_state_file_path() -> str:
    return os.path.join(get_base_dir(), "data", "shadow_state.json")


def serialize_trailing_stops(account) -> dict:
    """追踪止损序列化 — 唯一实现 (含 activation_price, 修复紧急停止缺字段问题)"""
    if not hasattr(account, "trailing_stops") or not account.trailing_stops:
        return {}
    return {
        sym: {
            "trail_pct": round(ts.trail_pct, 4),
            "highest_price": round(ts.highest_price, 2),
            "stop_price": round(ts.stop_price, 2),
            "entry_price": round(ts.entry_price, 2),
            "activation_price": round(ts.activation_price, 2) if ts.activation_price is not None else None,
            "breach_count": ts._breach_count,
            "confirm_cycles": ts.confirm_cycles,
        }
        for sym, ts in account.trailing_stops.items()
    }


def build_state_dict(account, extra: dict = None) -> dict:
    """shadow_state.json 平铺 schema 的单一构造函数。

    Args:
        account: ShadowAccount 实例
        extra:   附加字段 (如紧急保存的 {"stopped_at": ..., "reason": ...})
    """
    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "peak_equity": account.peak_equity,
        "drawdown": round((account.peak_equity - account.total_equity) / account.peak_equity, 6)
                    if account.peak_equity > 0 else 0,
        "equity_history": getattr(account, "equity_history", []),
        "last_cycle": datetime.datetime.now().isoformat(),
        "stop_loss_blacklist": account.stop_loss_blacklist,
        "strategy_decay_factor": account.strategy_decay_factor,
        "trailing_stops": serialize_trailing_stops(account),
    }
    if extra:
        state.update(extra)
    return state


def save_account_state(account) -> None:
    """P0 修复: 原子化保存账户状态（含 .bak 备份机制）。

    原 shadow_trader._save_account_state — execute() 每次成交后调用。
    """
    state_file = get_state_file_path()
    bak_file = state_file + ".bak"
    os.makedirs(os.path.dirname(state_file), exist_ok=True)

    state = build_state_dict(account)

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


def save_emergency_state(account, reason: str = "EMERGENCY_STOP") -> None:
    """紧急停止/退出时的状态保存 — 统一入口 (替代原 4 处内联构造)。

    与常规保存相同的完整 schema + stopped_at/reason 附加字段。
    """
    state_file = get_state_file_path()
    state = build_state_dict(account, extra={
        "stopped_at": datetime.datetime.now().isoformat(),
        "reason": reason,
    })
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        atomic_write(state_file, json.dumps(state, indent=2))
    except Exception:
        # 紧急路径兜底: 原子写失败时直接写
        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass


def load_saved_state() -> dict:
    """读取 shadow_state.json (main() 启动恢复用)。不存在返回 None。"""
    state_file = get_state_file_path()
    if not os.path.exists(state_file):
        return None
    with open(state_file) as f:
        return json.load(f)
