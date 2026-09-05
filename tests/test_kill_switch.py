"""KillSwitch 单元测试 — atos/core/kill_switch.py

覆盖:
  - check(): 日亏触发（阈值从 config_shared.RISK 读取，-2.5%→F3-2 为 -3%）
  - 回撤触发（F2 起 drawdown_liquidate_pct=25%）
  - 人工文件触发（data/KILL_SWITCH）
  - 日界翻转后基准权益复位
  - 日中断重启后首周期不误清当日累计

隔离策略: 替换 sm 为独立状态机实例并 mock _save/_log_event/_load，
避免读写 data/ 下的 system_state.json / risk_events.jsonl / KILL_SWITCH。

运行:
  venv/bin/python -m unittest discover -s tests
"""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from atos.core.kill_switch import KillSwitch
from atos.core.system_state import SystemState, SystemStateMachine
from atos.config_shared import RISK


def _indep_sm(state=SystemState.PAPER):
    """构造不读真实文件的独立状态机实例"""
    sm = object.__new__(SystemStateMachine)
    sm._state = state
    sm._state_since = datetime.datetime.utcnow()
    sm._reason = "test"
    return sm


class KillSwitchBase(unittest.TestCase):
    def setUp(self):
        # 禁止读写 data/ 下的真实状态文件
        self._p_load = patch.object(SystemStateMachine, "_load", lambda self: None)
        self._p_save = patch.object(SystemStateMachine, "_save", lambda self: None)
        self._p_log = patch.object(SystemStateMachine, "_log_event", lambda self, *a, **k: None)
        self._p_load.start()
        self._p_save.start()
        self._p_log.start()

        self.ks = KillSwitch()
        self.ks.sm = _indep_sm(SystemState.PAPER)

    def tearDown(self):
        self._p_log.stop()
        self._p_save.stop()
        self._p_load.stop()

    def _acct(self, equity, peak):
        return SimpleNamespace(total_equity=equity, peak_equity=peak)


class TestKillSwitchCheck(KillSwitchBase):
    """自动触发: 日亏 / 回撤 / 人工文件"""

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=False)
    def test_normal_no_trigger(self, mock_exists, mock_date):
        self.assertFalse(self.ks.check(self._acct(300000, 300000)))

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=False)
    def test_daily_loss_triggers_at_threshold(self, mock_exists, mock_date):
        # 阈值来自 config_shared.RISK 真源 (F3-2: -2.5% → -3%)
        expected_limit = -RISK["max_daily_loss_pct"]
        self.assertAlmostEqual(expected_limit, -0.03, places=6)
        self.ks.check(self._acct(300000, 300000))  # 首周期锁定基准 300000
        # 恰好 -3% → 触发
        self.assertTrue(self.ks.check(self._acct(291000, 300000)))
        self.assertEqual(self.ks.sm.state, SystemState.KILL_SWITCH)
        self.assertIn("daily loss", self.ks.sm._reason)

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=False)
    def test_daily_loss_below_threshold_no_trigger(self, mock_exists, mock_date):
        self.ks.check(self._acct(300000, 300000))
        self.assertFalse(self.ks.check(self._acct(296000, 300000)))  # -1.33% 未到线

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=False)
    def test_drawdown_triggers(self, mock_exists, mock_date):
        self.ks.check(self._acct(300000, 300000))  # 基准 300000
        self.ks.check(self._acct(400000, 400000))  # 新高，不触发
        # dd = (400000-299000)/400000 = 25.25% >= 25% (F2 drawdown_liquidate_pct)，日亏 -0.33% 不触发日亏分支
        self.assertTrue(self.ks.check(self._acct(299000, 400000)))
        self.assertEqual(self.ks.sm.state, SystemState.KILL_SWITCH)

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=True)
    def test_manual_file_triggers(self, mock_exists, mock_date):
        acct = self._acct(300000, 300000)
        self.assertTrue(self.ks.check(acct))
        self.assertEqual(self.ks.sm.state, SystemState.KILL_SWITCH)
        self.assertIn("MANUAL", self.ks.sm._reason)

    @patch("atos.core.kill_switch.get_market_date", return_value=datetime.date(2026, 8, 25))
    @patch("atos.core.kill_switch.os.path.exists", return_value=False)
    def test_already_killed_returns_true(self, mock_exists, mock_date):
        self.ks.sm = _indep_sm(SystemState.KILL_SWITCH)
        self.assertTrue(self.ks.check(self._acct(300000, 300000)))


class TestKillSwitchDayReset(KillSwitchBase):
    """日界翻转复位 / 重启首周期不误杀"""

    def test_day_boundary_resets_start_equity(self):
        day1 = datetime.date(2026, 8, 24)
        day2 = datetime.date(2026, 8, 25)
        with patch("atos.core.kill_switch.os.path.exists", return_value=False), \
             patch("atos.core.kill_switch.get_market_date", side_effect=[day1, day2]):
            self.assertFalse(self.ks.check(self._acct(300000, 300000)))  # day1 基准 300000
            self.assertEqual(self.ks.get_day_start_equity(), 300000)
            # day2 日界翻转 → 基准复位到当前权益，即使 equity 已跌也不误触发
            self.assertFalse(self.ks.check(self._acct(290000, 300000)))
            self.assertEqual(self.ks.get_day_start_equity(), 290000)

    def test_restart_first_cycle_does_not_falsely_trigger(self):
        # 模拟日中断重启：当日实际已亏 2.67%（292000），
        # 新实例首周期应以当前权益为基准，不误清/不误触发
        with patch("atos.core.kill_switch.os.path.exists", return_value=False), \
             patch("atos.core.kill_switch.get_market_date",
                   return_value=datetime.date(2026, 8, 25)):
            acct = self._acct(292000, 300000)  # dd 2.67% < 15%
            self.assertFalse(self.ks.check(acct))
            self.assertEqual(self.ks.get_day_start_equity(), 292000)

    def test_is_active_flags_killed_state(self):
        with patch("atos.core.kill_switch.os.path.exists", return_value=False):
            self.ks.sm = _indep_sm(SystemState.KILL_SWITCH)
            self.assertTrue(self.ks.is_active())


class TestKillSwitchReset(KillSwitchBase):
    def test_reset_requires_authorization(self):
        self.assertFalse(self.ks.reset(authorized=False))
        self.assertEqual(self.ks.sm.state, SystemState.PAPER)

    def test_reset_authorized_returns_to_paper(self):
        self.ks.sm = _indep_sm(SystemState.KILL_SWITCH)
        self.assertTrue(self.ks.reset(authorized=True))
        self.assertEqual(self.ks.sm.state, SystemState.PAPER)


if __name__ == "__main__":
    unittest.main()
