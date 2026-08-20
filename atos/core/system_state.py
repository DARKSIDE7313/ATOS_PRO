#!/usr/bin/env python3
"""
ATOS Institutional v2 — Global System State Machine
=====================================================
规格书 §3: 全局状态机

所有交易权限由状态决定。状态转移全部记录到 risk_events。
任何模块可以查询状态，但只有本模块能修改状态。

状态权限矩阵 (规格书 §3.2):
  PAPER/SHADOW:    模拟交易
  LIVE_LIMITED:    小额新仓
  LIVE_NORMAL:     全部允许
  RISK_REDUCED:    仅高评分新仓, 可减仓/平仓
  HALT_NEW_ORDERS: 禁止新仓, 允许平仓
  KILL_SWITCH:     全部禁止 (应急平仓除外)
  RECONCILIATION_REQUIRED: 停止新仓, 强制对账
"""
import json
import os
import threading
import datetime
from enum import Enum

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_FILE = os.path.join(BASE, 'data', 'system_state.json')
EVENTS_FILE = os.path.join(BASE, 'data', 'risk_events.jsonl')


class SystemState(str, Enum):
    BOOTSTRAP = "BOOTSTRAP"
    DATA_WARMUP = "DATA_WARMUP"
    PAPER = "PAPER"
    SHADOW_LIVE = "SHADOW_LIVE"
    LIVE_LIMITED = "LIVE_LIMITED"
    LIVE_NORMAL = "LIVE_NORMAL"
    RISK_REDUCED = "RISK_REDUCED"
    HALT_NEW_ORDERS = "HALT_NEW_ORDERS"
    KILL_SWITCH = "KILL_SWITCH"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


# 状态权限矩阵: (允许新仓, 允许加仓, 允许减仓, 允许平仓)
_PERMISSIONS = {
    SystemState.BOOTSTRAP:               (False, False, False, False),
    SystemState.DATA_WARMUP:             (False, False, False, False),
    SystemState.PAPER:                   (True,  True,  True,  True),   # 模拟
    SystemState.SHADOW_LIVE:             (False, False, False, False),
    SystemState.LIVE_LIMITED:            (True,  True,  True,  True),   # 小额
    SystemState.LIVE_NORMAL:             (True,  True,  True,  True),
    SystemState.RISK_REDUCED:            (True,  False, True,  True),   # 新仓需高评分
    SystemState.HALT_NEW_ORDERS:         (False, False, True,  True),
    SystemState.KILL_SWITCH:             (False, False, False, False),  # 应急平仓由专门路径
    SystemState.RECONCILIATION_REQUIRED: (False, False, True,  True),
}

# 合法转移表 (规格书 §3.1)
_TRANSITIONS = {
    SystemState.BOOTSTRAP: {SystemState.DATA_WARMUP, SystemState.KILL_SWITCH},
    SystemState.DATA_WARMUP: {SystemState.PAPER, SystemState.LIVE_NORMAL, SystemState.KILL_SWITCH},
    SystemState.PAPER: {SystemState.LIVE_LIMITED, SystemState.LIVE_NORMAL, SystemState.KILL_SWITCH},
    SystemState.SHADOW_LIVE: {SystemState.PAPER, SystemState.LIVE_LIMITED, SystemState.KILL_SWITCH},
    SystemState.LIVE_LIMITED: {SystemState.LIVE_NORMAL, SystemState.RISK_REDUCED,
                               SystemState.HALT_NEW_ORDERS, SystemState.KILL_SWITCH,
                               SystemState.RECONCILIATION_REQUIRED},
    SystemState.LIVE_NORMAL: {SystemState.RISK_REDUCED, SystemState.HALT_NEW_ORDERS,
                              SystemState.KILL_SWITCH, SystemState.RECONCILIATION_REQUIRED},
    SystemState.RISK_REDUCED: {SystemState.LIVE_NORMAL, SystemState.HALT_NEW_ORDERS,
                               SystemState.KILL_SWITCH, SystemState.RECONCILIATION_REQUIRED},
    SystemState.HALT_NEW_ORDERS: {SystemState.LIVE_NORMAL, SystemState.RISK_REDUCED,
                                  SystemState.KILL_SWITCH, SystemState.RECONCILIATION_REQUIRED},
    SystemState.KILL_SWITCH: {SystemState.PAPER, SystemState.LIVE_LIMITED},  # 不直接回 LIVE_NORMAL
    SystemState.RECONCILIATION_REQUIRED: {SystemState.HALT_NEW_ORDERS, SystemState.KILL_SWITCH,
                                          SystemState.LIVE_NORMAL},
}


class SystemStateMachine:
    """全局状态机 — 线程安全单例"""

    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self._state = SystemState.PAPER
        self._state_since = datetime.datetime.utcnow()
        self._reason = "init"
        self._load()

    @classmethod
    def get(cls) -> "SystemStateMachine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 查询 ──────────────────────────────────────────────
    @property
    def state(self) -> SystemState:
        return self._state

    def can_open_new(self) -> bool:
        return _PERMISSIONS[self._state][0]

    def can_add(self) -> bool:
        return _PERMISSIONS[self._state][1]

    def can_reduce(self) -> bool:
        return _PERMISSIONS[self._state][2]

    def can_close(self) -> bool:
        return _PERMISSIONS[self._state][3]

    def is_halted(self) -> bool:
        return self._state in (SystemState.KILL_SWITCH, SystemState.HALT_NEW_ORDERS,
                               SystemState.RECONCILIATION_REQUIRED)

    # ── 转移 ──────────────────────────────────────────────
    def transition(self, new_state: SystemState, reason: str,
                   severity: str = "INFO", force: bool = False) -> bool:
        """尝试状态转移。非法转移返回 False 并记录 WARNING 事件。"""
        with self._lock:
            old = self._state
            if not force and new_state not in _TRANSITIONS.get(old, set()):
                self._log_event("WARNING", "ILLEGAL_TRANSITION",
                                f"非法转移 {old.value} → {new_state.value}: {reason}",
                                old, old)
                return False
            self._state = new_state
            self._state_since = datetime.datetime.utcnow()
            self._reason = reason
            self._log_event(severity, "STATE_TRANSITION",
                            f"{old.value} → {new_state.value}: {reason}", old, new_state)
            self._save()
            return True

    def kill(self, reason: str):
        """Kill switch — 任何状态都可进入, 永远成功"""
        with self._lock:
            old = self._state
            self._state = SystemState.KILL_SWITCH
            self._state_since = datetime.datetime.utcnow()
            self._reason = reason
            self._log_event("EMERGENCY", "KILL_SWITCH", reason, old, SystemState.KILL_SWITCH)
            self._save()

    # ── 持久化 ──────────────────────────────────────────────
    def _save(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({
                'state': self._state.value,
                'since': self._state_since.isoformat() + 'Z',
                'reason': self._reason,
            }, f)
        os.replace(tmp, STATE_FILE)  # 原子写入

    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    d = json.load(f)
                self._state = SystemState(d.get('state', 'PAPER'))
                self._reason = d.get('reason', 'restored')
            except Exception:
                self._state = SystemState.PAPER

    def _log_event(self, severity: str, code: str, message: str,
                   before: SystemState, after: SystemState):
        """Append-only risk event log (规格书 §2.1 risk_events)"""
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        event = {
            'risk_event_id': f"{datetime.datetime.utcnow().timestamp()}",
            'severity': severity,
            'code': code,
            'message': message,
            'system_state_before': before.value,
            'system_state_after': after.value,
            'created_at_utc': datetime.datetime.utcnow().isoformat() + 'Z',
        }
        with open(EVENTS_FILE, 'a') as f:
            f.write(json.dumps(event) + '\n')

    def status(self) -> dict:
        return {
            'state': self._state.value,
            'since': self._state_since.isoformat() + 'Z',
            'reason': self._reason,
            'can_open_new': self.can_open_new(),
            'can_close': self.can_close(),
        }


# ── CLI 测试 ──────────────────────────────────────────────
if __name__ == '__main__':
    sm = SystemStateMachine.get()
    print(f"初始状态: {sm.state.value}")
    print(f"权限: new={sm.can_open_new()} close={sm.can_close()}")

    # 测试合法转移
    assert sm.transition(SystemState.LIVE_NORMAL, "test"), "PAPER→LIVE_NORMAL 应成功"
    print(f"✅ PAPER → LIVE_NORMAL")

    # 测试回撤降档
    assert sm.transition(SystemState.RISK_REDUCED, "drawdown 5%"), "LIVE_NORMAL→RISK_REDUCED 应成功"
    print(f"✅ LIVE_NORMAL → RISK_REDUCED")
    print(f"   RISK_REDUCED 权限: new={sm.can_open_new()} add={sm.can_add()} close={sm.can_close()}")

    # 测试非法转移 (RISK_REDUCED 不能直接回 PAPER)
    assert not sm.transition(SystemState.PAPER, "test"), "RISK_REDUCED→PAPER 应被拒绝"
    print(f"✅ 非法转移被拒绝 (RISK_REDUCED → PAPER)")

    # 测试 kill switch
    sm.kill("test kill switch")
    assert sm.state == SystemState.KILL_SWITCH
    assert not sm.can_open_new() and not sm.can_close()
    print(f"✅ KILL_SWITCH: new={sm.can_open_new()} close={sm.can_close()}")

    # kill switch 不能直接回 LIVE_NORMAL
    assert not sm.transition(SystemState.LIVE_NORMAL, "test"), "KILL→LIVE_NORMAL 应被拒绝"
    print(f"✅ KILL_SWITCH 不能直接回 LIVE_NORMAL (必须经 PAPER/LIVE_LIMITED 审核)")

    # 恢复到 PAPER
    assert sm.transition(SystemState.PAPER, "manual review passed")
    print(f"✅ KILL_SWITCH → PAPER (人工审核)")

    # HALT_NEW_ORDERS
    sm.transition(SystemState.LIVE_NORMAL, "resume")
    sm.transition(SystemState.HALT_NEW_ORDERS, "data stale")
    print(f"✅ HALT_NEW_ORDERS: new={sm.can_open_new()} close={sm.can_close()} (允许平仓)")

    print(f"\n全部状态机测试通过 ✅")
    print(f"事件日志: {EVENTS_FILE}")
