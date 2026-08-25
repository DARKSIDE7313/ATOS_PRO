"""risk_manager 单元测试 — atos/live/risk_manager.py

覆盖:
  - check_all_stops: v28 持仓跳过、非 v28 止盈/止损触发
  - reset_daily: 跨天复位全部日级状态
  - record_fill: 累计当日 PnL、锁定基准权益、连亏计数
  - check_daily_limits: 日亏熔断 / 连续亏损 / 订单上限 / 正常

运行:
  venv/bin/python -m unittest discover -s tests
"""
import unittest
from unittest.mock import patch

import atos.live.risk_manager as rm


class RiskManagerBase(unittest.TestCase):
    """隔离模块级全局状态，避免测试间串扰"""

    def setUp(self):
        rm.reset_daily()
        rm._daily_pnl = 0.0
        rm._daily_pnl_pct = 0.0
        rm._orders_this_cycle = 0
        rm._orders_this_day = 0
        rm._consecutive_losses = 0
        rm._current_drawdown = 0.0
        rm._trade_circuit_open = False
        rm._daily_start_equity = None

    def tearDown(self):
        rm.reset_daily()
        rm._trade_circuit_open = False


class TestCheckAllStops(RiskManagerBase):
    """统一止损/止盈检查 — v28 持仓完全跳过"""

    def test_v28_core_and_alpha_positions_skipped(self):
        positions = [
            {"symbol": "QQQ", "avg_price": 700.0, "shares": 10, "qty": 10, "last": 700.0},
            {"symbol": "NVDA", "avg_price": 100.0, "shares": 10, "qty": 10, "last": 100.0},
        ]
        signals = {"QQQ": {"price": 700.0}, "NVDA": {"price": 100.0}}
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(forced, [])

    def test_v28_skipped_even_when_profit_beyond_take_profit(self):
        positions = [{"symbol": "QQQ", "avg_price": 700.0, "shares": 10, "qty": 10, "last": 700.0}]
        signals = {"QQQ": {"price": 900.0}}  # +28% > 18% 止盈线，仍应被跳过
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(forced, [])

    def test_non_v28_take_profit_sells_half(self):
        positions = [{"symbol": "SPY", "avg_price": 100.0, "shares": 10, "qty": 10, "last": 100.0}]
        signals = {"SPY": {"price": 120.0}}  # +20% > 18%
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["exit_type"], "TAKE_PROFIT")
        self.assertEqual(forced[0]["symbol"], "SPY")
        self.assertEqual(forced[0]["qty"], 5)  # 卖一半，最少 1 股

    def test_non_v28_take_profit_qty_one_minimum(self):
        positions = [{"symbol": "SPY", "avg_price": 100.0, "shares": 1, "qty": 1, "last": 100.0}]
        signals = {"SPY": {"price": 120.0}}
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["qty"], 1)  # qty//2 = 0 → max(1, 0)

    def test_non_v28_hard_stop_loss_sells_all(self):
        positions = [{"symbol": "SPY", "avg_price": 100.0, "shares": 10, "qty": 10, "last": 100.0}]
        signals = {"SPY": {"price": 93.0}}  # -7% < -5% 硬止损
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["exit_type"], "STOP_LOSS")
        self.assertEqual(forced[0]["qty"], 10)
        self.assertEqual(forced[0]["outcome"], "LOSS")

    def test_non_v28_atr_stop_triggers_earlier_than_hard_stop(self):
        positions = [{"symbol": "SPY", "avg_price": 100.0, "shares": 10, "qty": 10, "last": 100.0}]
        signals = {"SPY": {"price": 95.5, "atr": 2.0}}  # -4.5% → ATR stop 4.2%，早于硬止损 5%
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["exit_type"], "STOP_LOSS")

    def test_invalid_price_skipped(self):
        positions = [{"symbol": "SPY", "avg_price": 100.0, "shares": 10, "qty": 10, "last": 0}]
        signals = {"SPY": {"price": 0}}
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(forced, [])

    def test_invalid_avg_price_skipped(self):
        positions = [{"symbol": "SPY", "avg_price": 0, "shares": 10, "qty": 10, "last": 100.0}]
        signals = {"SPY": {"price": 100.0}}
        forced = rm.check_all_stops(positions, signals)
        self.assertEqual(forced, [])


class TestRecordFill(RiskManagerBase):
    """成交记录 — 累计当日 PnL / 锁分母 / 连亏计数"""

    @patch("atos.live.risk_manager.save_risk_state")
    def test_accumulates_daily_pnl(self, mock_save):
        rm.record_fill(-1000.0, total_equity=100000.0)
        rm.record_fill(-500.0, total_equity=100000.0)
        self.assertEqual(rm._daily_pnl, -1500.0)
        self.assertAlmostEqual(rm._daily_pnl_pct, -0.015, places=6)
        self.assertEqual(rm._orders_this_day, 2)
        mock_save.assert_called()

    @patch("atos.live.risk_manager.save_risk_state")
    def test_wins_reset_consecutive_losses(self, mock_save):
        rm.record_fill(-100.0, 100000.0)
        rm.record_fill(-100.0, 100000.0)
        self.assertEqual(rm._consecutive_losses, 2)
        rm.record_fill(50.0, 100000.0)
        self.assertEqual(rm._consecutive_losses, 0)

    @patch("atos.live.risk_manager.save_risk_state")
    def test_fixed_denominator_start_equity(self, mock_save):
        # 首次成交锁定基准权益，之后亏损不缩小分母（防自强化）
        rm.record_fill(-1000.0, total_equity=100000.0)
        rm.record_fill(-1000.0, total_equity=90000.0)  # equity 已缩水
        self.assertEqual(rm._daily_start_equity, 100000.0)
        self.assertAlmostEqual(rm._daily_pnl_pct, -0.02, places=6)

    @patch("atos.live.risk_manager.save_risk_state")
    def test_reset_daily_clears_all(self, mock_save):
        rm.record_fill(-3000.0, 100000.0)
        rm._trade_circuit_open = True
        rm.reset_daily()
        self.assertEqual(rm._daily_pnl, 0.0)
        self.assertEqual(rm._daily_pnl_pct, 0.0)
        self.assertEqual(rm._orders_this_day, 0)
        self.assertFalse(rm._trade_circuit_open)
        self.assertIsNone(rm._daily_start_equity)


class TestCheckDailyLimits(RiskManagerBase):
    """日度限制 — 熔断 / 连亏 / 订单上限 / 正常"""

    def test_normal_allows_trade(self):
        d = rm.check_daily_limits(100000.0)
        self.assertTrue(d["can_trade"])
        self.assertFalse(d["circuit_open"])

    @patch("atos.live.risk_manager.save_risk_state")
    @patch("atos.core.risk_coordinator.sync_from_legacy")
    def test_daily_loss_opens_circuit(self, mock_sync, mock_save):
        rm.record_fill(-3000.0, 100000.0)  # -3% <= -2.5%
        d = rm.check_daily_limits(100000.0)
        self.assertFalse(d["can_trade"])
        self.assertTrue(d["circuit_open"])
        mock_sync.assert_called_once()

    def test_consecutive_losses_block_new_orders(self):
        rm._consecutive_losses = 3  # max_consecutive_losses
        d = rm.check_daily_limits(100000.0)
        self.assertFalse(d["can_trade"])
        self.assertFalse(d["circuit_open"])  # 连亏降频不是熔断

    def test_order_count_limit_reached(self):
        rm._orders_this_day = 12  # MAX_ORDERS_PER_CYCLE * 3
        d = rm.check_daily_limits(100000.0)
        self.assertFalse(d["can_trade"])

    def test_order_count_below_limit_allowed(self):
        rm._orders_this_day = 11
        d = rm.check_daily_limits(100000.0)
        self.assertTrue(d["can_trade"])


if __name__ == "__main__":
    unittest.main()
