"""
ATOS PRO — 风控协调器 (Phase 5 框架重塑)
===========================================
历史问题 (双风控轨并存, 报告 §7.1.4):
  - 旧轨: atos/live/risk_manager.py — 日亏熔断/连亏/回撤熔断 (risk_state.json)
  - 新轨: atos/core/system_state.py — v29 九态状态机 (system_state.json)
  两轨状态不联动: 2026-08-20 risk_state 熔断 circuit_open=true (25连亏),
  而 system_state 仍是 PAPER — 风控门继续放行买入, 熔断形同虚设。

本模块是 "是否允许交易" 的 **单一决策源**:
  1. risk_manager 每次持久化状态时调用 :func:`sync_from_legacy` —
     legacy 熔断打开 → 状态机强制 HALT_NEW_ORDERS (联动);
     熔断关闭 → 仅当 HALT 是本协调器造成的才自动恢复。
  2. :func:`trading_permission` 汇总两轨 + kill switch 文件,
     任何模块需要判断交易权限时只问这里。

不修改 risk_manager / system_state 各自的既有 API 与阈值 (策略行为不变),
只在 risk_manager.save_risk_state() 这个唯一收口点注入联动调用。
"""

import os
import importlib as _importlib

from atos.core.system_state import SystemStateMachine, SystemState

logger = _importlib.import_module('logging').getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KILL_FILE = os.path.join(BASE, 'data', 'KILL_SWITCH')

# 协调器强加 HALT 时使用的 reason 前缀 (自动恢复时据此识别"自己造成的" HALT)
_SYNC_REASON_PREFIX = "risk_manager circuit_open sync"

# 协调器允许主动强制降级的状态 (正常交易态)。
# KILL_SWITCH / RECONCILIATION_REQUIRED / HALT_NEW_ORDERS 绝不主动触碰。
_HALTABLE_STATES = (
    SystemState.PAPER,
    SystemState.LIVE_LIMITED,
    SystemState.LIVE_NORMAL,
    SystemState.RISK_REDUCED,
)


def sync_from_legacy(rm_state: dict) -> bool:
    """把 legacy risk_manager 状态联动到 v29 状态机。

    调用时机: risk_manager.save_risk_state() (每次熔断/回撤/成交状态变化后)。

    Args:
        rm_state: risk_manager.get_state() 的返回 dict,
                  至少需要 ``circuit_open`` 键。

    Returns:
        True 表示本次调用造成了状态机转移。
    """
    try:
        sm = SystemStateMachine.get()
    except Exception as e:
        logger.warning(f"risk_coordinator: 状态机不可用, 跳过联动: {e}")
        return False

    if not isinstance(rm_state, dict):
        rm_state = rm_state or {}

    circuit_open = bool(rm_state.get("circuit_open", False))

    # ── 熔断打开 → 强制 HALT_NEW_ORDERS ──
    if circuit_open:
        if sm.state in _HALTABLE_STATES:
            detail = (f"daily_pnl={rm_state.get('daily_pnl', 0):.2f} "
                      f"dd={rm_state.get('current_drawdown', 0):.2%} "
                      f"consec_losses={rm_state.get('consecutive_losses', 0)}")
            ok = sm.transition(
                SystemState.HALT_NEW_ORDERS,
                f"{_SYNC_REASON_PREFIX}: {detail}",
                severity="CRITICAL",
                force=True,   # 紧急联动: 不改变 _TRANSITIONS 规则表, 显式强制
            )
            if ok:
                logger.warning(
                    f"🔗 风控联动: legacy 熔断打开 → 状态机 {sm.state.value} ({detail})"
                )
            return ok
        return False

    # ── 熔断关闭 → 仅恢复协调器自己造成的 HALT ──
    if sm.state == SystemState.HALT_NEW_ORDERS and \
            str(getattr(sm, "_reason", "")).startswith(_SYNC_REASON_PREFIX):
        ok = sm.transition(
            SystemState.PAPER,
            "risk_manager circuit closed → auto resume",
            force=True,
        )
        if ok:
            logger.info("🔗 风控联动: legacy 熔断关闭 → 状态机恢复 PAPER")
        return ok

    return False


def trading_permission() -> dict:
    """单一决策源: 汇总两轨 + kill switch 文件的统一交易权限判定。

    Returns:
        {
          "can_open_new": bool,     # 允许开新仓 (两轨都放行才为 True)
          "can_close": bool,        # 允许平仓/减仓
          "system_state": str,      # v29 状态机当前状态
          "legacy_circuit_open": bool,
          "kill_file": bool,        # data/KILL_SWITCH 文件存在
          "reason": str,
        }
    """
    try:
        sm = SystemStateMachine.get()
        state = sm.state
        sm_open = sm.can_open_new()
        sm_close = sm.can_close() or sm.can_reduce()
    except Exception as e:
        # 状态机不可用 → fail closed 开仓, 允许平仓
        return {
            "can_open_new": False,
            "can_close": True,
            "system_state": f"ERROR:{e}",
            "legacy_circuit_open": None,
            "kill_file": os.path.exists(KILL_FILE),
            "reason": "state machine unavailable (fail closed on new orders)",
        }

    # legacy 轨 (惰性导入避免循环: risk_manager 调用本模块的 sync)
    legacy_circuit = None
    try:
        from atos.live.risk_manager import get_state as _rm_get_state
        legacy_circuit = bool(_rm_get_state().get("circuit_open", False))
    except Exception:
        legacy_circuit = None

    kill_file = os.path.exists(KILL_FILE)

    can_open = sm_open and (legacy_circuit is not True) and not kill_file
    reasons = []
    if not sm_open:
        reasons.append(f"state={state.value}")
    if legacy_circuit is True:
        reasons.append("legacy circuit open")
    if kill_file:
        reasons.append("KILL_SWITCH file")

    return {
        "can_open_new": can_open,
        "can_close": sm_close,
        "system_state": state.value,
        "legacy_circuit_open": legacy_circuit,
        "kill_file": kill_file,
        "reason": "; ".join(reasons) if reasons else "ok",
    }
