"""PreTradeRiskGate 单元测试 — atos/core/risk_gate.py

覆盖:
  - APPROVE / REDUCE / REJECT 三分支
  - 重复订单幂等拒绝
  - 非法参数拒绝
  - Kill switch 状态拒绝
  - SELL 超持仓自动减量
  - 账户异常 fail closed (GATE_EXCEPTION)

隔离策略: 独立状态机实例 + mock _audit/_save_seen/_load_seen，
避免读写 data/ 下的 risk_decisions.jsonl / seen_orders.json / system_state.json。

运行:
  venv/bin/python -m unittest discover -s tests
"""
import datetime
import unittest
from unittest.mock import patch

from atos.core.risk_gate import OrderIntent, PreTradeRiskGate
from atos.core.system_state import SystemState, SystemStateMachine


class FakeAccount:
    def __init__(self, total_equity=300000, cash=150000, positions=None):
        self.total_equity = total_equity
        self.cash = cash
        self.positions = positions or {}


class BrokenAccount:
    """positions 访问即抛异常 — 触发 fail closed"""

    @property
    def positions(self):
        raise RuntimeError("simulated account breakdown")


def _indep_sm(state=SystemState.PAPER):
    sm = object.__new__(SystemStateMachine)
    sm._state = state
    sm._state_since = datetime.datetime.utcnow()
    sm._reason = "test"
    return sm


class RiskGateBase(unittest.TestCase):
    def setUp(self):
        # 禁止读写 data/ 下的真实文件
        self._p_load = patch.object(SystemStateMachine, "_load", lambda self: None)
        self._p_save = patch.object(SystemStateMachine, "_save", lambda self: None)
        self._p_log = patch.object(SystemStateMachine, "_log_event", lambda self, *a, **k: None)
        self._p_gate_load = patch.object(PreTradeRiskGate, "_load_seen", lambda self: None)
        self._p_gate_save = patch.object(PreTradeRiskGate, "_save_seen", lambda self: None)
        self._p_gate_audit = patch.object(PreTradeRiskGate, "_audit", lambda self, *a, **k: None)
        self._p_load.start()
        self._p_save.start()
        self._p_log.start()
        self._p_gate_load.start()
        self._p_gate_save.start()
        self._p_gate_audit.start()

        self.gate = PreTradeRiskGate()
        self.gate.sm = _indep_sm(SystemState.PAPER)

    def tearDown(self):
        self._p_gate_audit.stop()
        self._p_gate_save.stop()
        self._p_gate_load.stop()
        self._p_log.stop()
        self._p_save.stop()
        self._p_load.stop()


class TestRiskGateApprove(RiskGateBase):
    def test_approve_normal_buy(self):
        acct = FakeAccount(total_equity=300000, cash=150000, positions={})
        d = self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, "v28动量alpha"), acct)
        self.assertEqual(d.decision, "APPROVE")
        self.assertEqual(d.approved_quantity, 100)

    def test_approve_separate_signal_ids_not_duplicate(self):
        acct = FakeAccount(total_equity=300000, cash=150000, positions={})
        d1 = self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, reason="alpha", signal_id="sigA"), acct)
        d2 = self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, reason="alpha", signal_id="sigB"), acct)
        self.assertEqual(d1.decision, "APPROVE")
        self.assertEqual(d2.decision, "APPROVE")


class TestRiskGateReject(RiskGateBase):
    def test_reject_duplicate_order(self):
        acct = FakeAccount(total_equity=300000, cash=150000, positions={})
        self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, "dup"), acct)
        d = self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, "dup"), acct)
        self.assertEqual(d.decision, "REJECT")
        self.assertTrue(any("DUPLICATE" in r for r in d.reasons))

    def test_reject_zero_quantity(self):
        acct = FakeAccount()
        d = self.gate.check(OrderIntent("NVDA", "BUY", 0, 220.0, "bad"), acct)
        self.assertEqual(d.decision, "REJECT")
        self.assertTrue(any("INVALID_PARAMS" in r for r in d.reasons))

    def test_reject_invalid_price(self):
        acct = FakeAccount()
        d = self.gate.check(OrderIntent("NVDA", "BUY", 10, 0.0, "bad"), acct)
        self.assertEqual(d.decision, "REJECT")

    def test_reject_kill_switch_active(self):
        acct = FakeAccount()
        self.gate.sm.kill("test kill")
        d = self.gate.check(OrderIntent("MSFT", "BUY", 10, 480.0, "x"), acct)
        self.assertEqual(d.decision, "REJECT")
        self.assertTrue(any("KILL_SWITCH_ACTIVE" in r for r in d.reasons))
        self.assertEqual(d.approved_quantity, 0)


class TestRiskGateReduce(RiskGateBase):
    def test_reduce_when_exceeds_single_cap(self):
        # 已有 AAPL $5000；单仓上限 12%*300000=36000，room=31000
        acct = FakeAccount(positions={"AAPL": {"shares": 50, "qty": 50, "avg_price": 100.0, "last_price": 100.0}})
        d = self.gate.check(OrderIntent("AAPL", "BUY", 500, 100.0, "add"), acct)
        self.assertEqual(d.decision, "REDUCE")
        self.assertGreater(d.approved_quantity, 0)
        self.assertLess(d.approved_quantity, 500)
        self.assertTrue(any("REDUCED_TO_CAP" in r for r in d.reasons))

    def test_reduce_when_cash_insufficient(self):
        # 现金极少 → 只能买得起一部分
        acct = FakeAccount(total_equity=300000, cash=10000, positions={})
        d = self.gate.check(OrderIntent("NVDA", "BUY", 100, 220.0, "buy"), acct)
        self.assertEqual(d.decision, "REDUCE")
        self.assertLess(d.approved_quantity, 100)

    def test_reduce_sell_beyond_held_quantity(self):
        acct = FakeAccount(positions={"QQQ": {"shares": 50, "qty": 50, "avg_price": 100.0, "last_price": 100.0}})
        d = self.gate.check(OrderIntent("QQQ", "SELL", 100, 100.0, "close"), acct)
        self.assertEqual(d.decision, "REDUCE")
        self.assertEqual(d.approved_quantity, 50)  # 自动减到实际持仓


class TestRiskGateFailClosed(RiskGateBase):
    def test_account_error_fails_closed(self):
        d = self.gate.check(OrderIntent("NVDA", "BUY", 10, 220.0, "boom"), BrokenAccount())
        self.assertEqual(d.decision, "REJECT")
        self.assertEqual(d.approved_quantity, 0)
        self.assertTrue(any("GATE_EXCEPTION" in r for r in d.reasons))


if __name__ == "__main__":
    unittest.main()
