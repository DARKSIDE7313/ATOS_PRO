"""TrailingStop 单元测试 — atos/risk/professional.py

覆盖:
  - init() 夹紧 trail_pct 到 [3%, 20%]
  - update() 随盈利上移止损线
  - confirm_cycles 连续跌破确认逻辑
  - entry_price=0 除零保护

运行:
  venv/bin/python -m unittest discover -s tests
"""
import unittest

from atos.risk.professional import TrailingStop


class TestTrailingStopInit(unittest.TestCase):
    """init() 参数夹紧与初始状态"""

    def test_init_clamps_trail_pct_below_min(self):
        ts = TrailingStop(trail_pct=0.01)  # 低于下限 3%
        ts.init(entry_price=100.0)
        self.assertEqual(ts.trail_pct, 0.03)
        self.assertAlmostEqual(ts.stop_price, 100.0 * 0.97, places=6)

    def test_init_clamps_trail_pct_above_max(self):
        ts = TrailingStop(trail_pct=0.50)  # 高于上限 20%
        ts.init(entry_price=100.0)
        self.assertEqual(ts.trail_pct, 0.20)
        self.assertAlmostEqual(ts.stop_price, 100.0 * 0.80, places=6)

    def test_init_within_range_unchanged(self):
        ts = TrailingStop(trail_pct=0.08)
        ts.init(entry_price=100.0)
        self.assertEqual(ts.trail_pct, 0.08)
        self.assertAlmostEqual(ts.stop_price, 100.0 * 0.92, places=6)

    def test_init_sets_entry_highest_and_resets_breach(self):
        ts = TrailingStop(trail_pct=0.05)
        ts.init(entry_price=100.0)
        self.assertEqual(ts.entry_price, 100.0)
        self.assertEqual(ts.highest_price, 100.0)
        self.assertEqual(ts._breach_count, 0)


class TestTrailingStopUpdate(unittest.TestCase):
    """update() 止损线上移与连续跌破确认"""

    def test_update_raises_stop_on_new_high(self):
        ts = TrailingStop(trail_pct=0.05, confirm_cycles=2)
        ts.init(entry_price=100.0)
        r = ts.update(110.0)  # 新高 → 止损线上移
        self.assertEqual(ts.highest_price, 110.0)
        self.assertAlmostEqual(ts.stop_price, 110.0 * 0.95, places=6)
        self.assertFalse(r["triggered"])

    def test_update_requires_consecutive_breaches(self):
        ts = TrailingStop(trail_pct=0.05, confirm_cycles=2)
        ts.init(entry_price=100.0)
        ts.update(110.0)  # stop = 104.5
        r1 = ts.update(104.0)  # 第一次跌破
        self.assertFalse(r1["triggered"])
        self.assertEqual(r1["breach_count"], 1)
        r2 = ts.update(103.5)  # 连续第二次跌破 → 触发
        self.assertTrue(r2["triggered"])
        self.assertEqual(r2["breach_count"], 2)

    def test_update_recovery_resets_breach_count(self):
        ts = TrailingStop(trail_pct=0.05, confirm_cycles=3)
        ts.init(entry_price=100.0)
        ts.update(110.0)
        ts.update(104.0)  # 第一次跌破
        self.assertEqual(ts._breach_count, 1)
        r = ts.update(107.0)  # 反弹回止损线上方 → 计数清零
        self.assertEqual(ts._breach_count, 0)
        self.assertFalse(r["triggered"])

    def test_confirm_cycles_one_triggers_immediately(self):
        ts = TrailingStop(trail_pct=0.05, confirm_cycles=1)
        ts.init(entry_price=100.0)
        ts.update(110.0)
        r = ts.update(104.0)
        self.assertTrue(r["triggered"])

    def test_update_does_not_track_new_high_when_falling(self):
        ts = TrailingStop(trail_pct=0.05, confirm_cycles=2)
        ts.init(entry_price=100.0)
        ts.update(110.0)  # stop=104.5
        ts.update(108.0)  # 未破止损，不触发
        self.assertEqual(ts.highest_price, 110.0)
        self.assertAlmostEqual(ts.stop_price, 104.5, places=6)


class TestTrailingStopZeroEntry(unittest.TestCase):
    """entry_price=0 除零保护"""

    def test_zero_entry_update_returns_safe_pnl(self):
        ts = TrailingStop(trail_pct=0.05)
        ts.init(entry_price=0.0)
        r = ts.update(0.0)
        self.assertEqual(r["unrealized_pnl"], 0.0)  # 不抛 ZeroDivisionError
        self.assertEqual(r["profit_from_peak"], 0.0)

    def test_zero_entry_then_trade_safe(self):
        ts = TrailingStop(trail_pct=0.05)
        ts.init(entry_price=0.0)
        ts.update(5.0)  # 新高
        ts.update(4.0)  # 跌破但 entry_price 仍为 0
        self.assertEqual(ts.entry_price, 0.0)
        self.assertAlmostEqual(ts.stop_price, 5.0 * 0.95, places=6)
        self.assertEqual(ts.update(4.0)["unrealized_pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
