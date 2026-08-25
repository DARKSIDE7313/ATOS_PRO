"""SystemStateMachine 单元测试 — atos/core/system_state.py

覆盖:
  - 状态机转移矩阵（合法 / 非法转移）
  - KILL_SWITCH 状态只能回到 PAPER / LIVE_LIMITED（人工审核后）
  - kill() 从任意状态可进入
  - 权限矩阵（can_open_new / can_add / can_reduce / can_close / is_halted）

隔离策略: mock _load/_save/_log_event，避免读写 data/ 下的
system_state.json / risk_events.jsonl。

运行:
  venv/bin/python -m unittest discover -s tests
"""
import unittest
from unittest.mock import patch

from atos.core.system_state import SystemState, SystemStateMachine, _TRANSITIONS


class SystemStateBase(unittest.TestCase):
    def setUp(self):
        self._p_load = patch.object(SystemStateMachine, "_load", lambda self: None)
        self._p_save = patch.object(SystemStateMachine, "_save", lambda self: None)
        self._p_log = patch.object(SystemStateMachine, "_log_event", lambda self, *a, **k: None)
        self._p_load.start()
        self._p_save.start()
        self._p_log.start()
        self.sm = SystemStateMachine()
        self.sm._state = SystemState.PAPER

    def tearDown(self):
        self._p_log.stop()
        self._p_save.stop()
        self._p_load.stop()


class TestStateMachineTransitions(SystemStateBase):
    def test_all_legal_transitions_succeed(self):
        for src, dsts in _TRANSITIONS.items():
            for dst in dsts:
                self.sm._state = src
                self.assertTrue(self.sm.transition(dst, "legal"), f"{src} → {dst} 应合法")
                self.assertEqual(self.sm.state, dst)

    def test_illegal_transitions_rejected_and_state_unchanged(self):
        all_states = set(SystemState)
        for src, dsts in _TRANSITIONS.items():
            illegal = all_states - dsts - {src}
            if not illegal:
                continue
            dst = next(iter(illegal))
            self.sm._state = src
            self.assertFalse(self.sm.transition(dst, "illegal"), f"{src} → {dst} 应被拒")
            self.assertEqual(self.sm.state, src)  # 非法转移不改变状态

    def test_force_transition_bypasses_matrix(self):
        self.sm._state = SystemState.RISK_REDUCED
        self.assertTrue(self.sm.transition(SystemState.PAPER, "force", force=True))
        self.assertEqual(self.sm.state, SystemState.PAPER)

    def test_self_transition_rejected(self):
        self.sm._state = SystemState.LIVE_NORMAL
        self.assertFalse(self.sm.transition(SystemState.LIVE_NORMAL, "self"))


class TestKillSwitchState(SystemStateBase):
    def test_kill_from_any_state(self):
        for st in SystemState:
            self.sm._state = st
            self.sm.kill(f"from {st.value}")
            self.assertEqual(self.sm.state, SystemState.KILL_SWITCH)

    def test_kill_switch_only_recovers_to_paper_or_limited(self):
        self.sm.kill("test")
        self.assertEqual(self.sm.state, SystemState.KILL_SWITCH)
        # 不能直接回 LIVE_NORMAL / SHADOW_LIVE
        self.assertFalse(self.sm.transition(SystemState.LIVE_NORMAL, "resume"))
        self.assertFalse(self.sm.transition(SystemState.SHADOW_LIVE, "resume"))
        # 人工审核 → PAPER
        self.assertTrue(self.sm.transition(SystemState.PAPER, "manual review"))
        # 再次 kill → LIVE_LIMITED 也允许
        self.sm.kill("test2")
        self.assertTrue(self.sm.transition(SystemState.LIVE_LIMITED, "limited resume"))

    def test_kill_disables_all_permissions(self):
        self.sm.kill("test")
        self.assertFalse(self.sm.can_open_new())
        self.assertFalse(self.sm.can_add())
        self.assertFalse(self.sm.can_reduce())
        self.assertFalse(self.sm.can_close())


class TestPermissions(SystemStateBase):
    def test_paper_full_permissions(self):
        self.sm._state = SystemState.PAPER
        self.assertTrue(self.sm.can_open_new())
        self.assertTrue(self.sm.can_add())
        self.assertTrue(self.sm.can_reduce())
        self.assertTrue(self.sm.can_close())

    def test_live_normal_full_permissions(self):
        self.sm._state = SystemState.LIVE_NORMAL
        self.assertTrue(self.sm.can_open_new())
        self.assertTrue(self.sm.can_close())

    def test_risk_reduced_blocks_add(self):
        self.sm._state = SystemState.RISK_REDUCED
        self.assertTrue(self.sm.can_open_new())   # 仅高评分新仓
        self.assertFalse(self.sm.can_add())       # 禁止加仓
        self.assertTrue(self.sm.can_reduce())
        self.assertTrue(self.sm.can_close())

    def test_halt_new_orders_allows_close(self):
        self.sm._state = SystemState.HALT_NEW_ORDERS
        self.assertFalse(self.sm.can_open_new())
        self.assertFalse(self.sm.can_add())
        self.assertTrue(self.sm.can_reduce())
        self.assertTrue(self.sm.can_close())

    def test_bootstrap_blocks_everything(self):
        self.sm._state = SystemState.BOOTSTRAP
        self.assertFalse(self.sm.can_open_new())
        self.assertFalse(self.sm.can_close())

    def test_is_halted_states(self):
        self.sm._state = SystemState.KILL_SWITCH
        self.assertTrue(self.sm.is_halted())
        self.sm._state = SystemState.HALT_NEW_ORDERS
        self.assertTrue(self.sm.is_halted())
        self.sm._state = SystemState.RECONCILIATION_REQUIRED
        self.assertTrue(self.sm.is_halted())
        self.sm._state = SystemState.PAPER
        self.assertFalse(self.sm.is_halted())


class TestStatus(SystemStateBase):
    def test_status_reports_current_state(self):
        self.sm._state = SystemState.LIVE_NORMAL
        st = self.sm.status()
        self.assertEqual(st["state"], "LIVE_NORMAL")
        self.assertTrue(st["can_open_new"])
        self.assertTrue(st["can_close"])


if __name__ == "__main__":
    unittest.main()
