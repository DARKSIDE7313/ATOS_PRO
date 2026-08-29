"""strategy_v28 单元测试 — atos/shadow/strategy_v28.py

覆盖:
  - is_v28_position 持仓判定（QQQ 核心仓 + alpha 动量池）
  - 止损/移动止损参数存在性（v28 与 QQQ 各自独立）
  - v28_check_exits 闭市止损（审计 P2: 闭市无止损 — 卖出检查与交易时段解耦）

运行:
  venv/bin/python -m unittest discover -s tests
"""
import unittest

from atos.shadow.strategy_v28 import (
    V28_ALPHA_COUNT,
    V28_ALPHA_UNIVERSE,
    V28_CORE_PCT,
    V28_CORE_SYMBOL,
    V28_QQQ_TRAILING,
    V28_REBALANCE_DAYS,
    V28_STOP_LOSS,
    V28_TRAILING_STOP,
    is_v28_position,
    v28_check_exits,
)


class _FakeAccount:
    """最小 account 替身 — 记录 execute 调用，模拟持仓。"""

    def __init__(self, positions):
        self.positions = positions
        self.executed = []

    def execute(self, sym, side, qty, price, reason):
        self.executed.append({"sym": sym, "side": side, "qty": qty,
                              "price": price, "reason": reason})
        # 模拟卖出后持仓清零
        if side == "SELL":
            pos = self.positions.get(sym)
            if pos:
                pos["qty"] = max(0, pos.get("qty", 0) - qty)
        return True


class TestV28CheckExits(unittest.TestCase):
    """v28_check_exits — 闭市时段止损（与 is_market_hours 解耦）。"""

    def _qqq_pos(self, avg=100.0, last=100.0, peak=None):
        return {"QQQ": {
            "qty": 10, "avg_price": avg, "last_price": last,
            "peak_price": peak if peak is not None else avg,
        }}

    def test_qqq_trailing_stop_triggers(self):
        # 峰值130 (浮盈30%>5%) → 现价110 → 回撤15.4% >= 12% → 卖出
        acct = _FakeAccount(self._qqq_pos(avg=100.0, last=110.0, peak=130.0))
        v28_check_exits(acct, {"QQQ": {"price": 110.0}})
        self.assertEqual(len(acct.executed), 1)
        self.assertEqual(acct.executed[0]["sym"], "QQQ")
        self.assertEqual(acct.executed[0]["side"], "SELL")
        self.assertIn("移动止损", acct.executed[0]["reason"])

    def test_qqq_no_trigger_below_profit_threshold(self):
        # 峰值未超 avg*1.05 → 不启用移动止损 → 不卖出
        acct = _FakeAccount(self._qqq_pos(avg=100.0, last=98.0, peak=103.0))
        v28_check_exits(acct, {"QQQ": {"price": 98.0}})
        self.assertEqual(len(acct.executed), 0)

    def test_qqq_small_drawdown_no_trigger(self):
        # 峰值125 → 现价118 → 回撤5.6% < 12% → 不卖出
        acct = _FakeAccount(self._qqq_pos(avg=100.0, last=118.0, peak=125.0))
        v28_check_exits(acct, {"QQQ": {"price": 118.0}})
        self.assertEqual(len(acct.executed), 0)

    def test_individual_stop_loss_triggers(self):
        # alpha 个股: 亏损 -6% <= -5% → 卖出
        positions = {"NVDA": {"qty": 5, "avg_price": 100.0, "last_price": 94.0}}
        acct = _FakeAccount(positions)
        v28_check_exits(acct, {"NVDA": {"price": 94.0}})
        self.assertEqual(len(acct.executed), 1)
        self.assertIn("止损", acct.executed[0]["reason"])

    def test_individual_trailing_stop_triggers(self):
        # alpha 个股: 峰值108 (浮盈>3%) → 现价98 → 回撤9.3% >= 8% → 卖出
        positions = {"NVDA": {"qty": 5, "avg_price": 100.0, "last_price": 98.0,
                              "peak_price": 108.0}}
        acct = _FakeAccount(positions)
        v28_check_exits(acct, {"NVDA": {"price": 98.0}})
        self.assertEqual(len(acct.executed), 1)
        self.assertIn("移动止损", acct.executed[0]["reason"])

    def test_no_signals_no_action(self):
        # 价格不可得 (signals 缺价且持仓无 last) → 跳过
        positions = {"NVDA": {"qty": 5, "avg_price": 100.0}}
        acct = _FakeAccount(positions)
        v28_check_exits(acct, {})
        self.assertEqual(len(acct.executed), 0)

    def test_peak_price_updates(self):
        # 现价高于峰值 → 峰值应被更新
        positions = {"NVDA": {"qty": 5, "avg_price": 100.0, "last_price": 100.0,
                              "peak_price": 100.0}}
        acct = _FakeAccount(positions)
        v28_check_exits(acct, {"NVDA": {"price": 110.0}})
        self.assertEqual(acct.positions["NVDA"]["peak_price"], 110.0)
        self.assertEqual(len(acct.executed), 0)


class TestIsV28Position(unittest.TestCase):
    """v28 持仓判定 — 唯一权威入口"""

    def test_core_symbol_is_v28(self):
        self.assertTrue(is_v28_position(V28_CORE_SYMBOL))
        self.assertTrue(is_v28_position("QQQ"))

    def test_alpha_universe_is_v28(self):
        for sym in V28_ALPHA_UNIVERSE:
            self.assertTrue(is_v28_position(sym), f"{sym} 应判定为 v28 持仓")

    def test_non_v28_symbols(self):
        for sym in ("SPY", "XOM", "JPM", "BRK.B", ""):
            self.assertFalse(is_v28_position(sym), f"{sym!r} 不应判定为 v28 持仓")

    def test_matching_is_case_sensitive(self):
        self.assertFalse(is_v28_position("qqq"))
        self.assertFalse(is_v28_position("nvda"))

    def test_core_symbol_not_duplicated_in_alpha(self):
        self.assertNotIn(V28_CORE_SYMBOL, V28_ALPHA_UNIVERSE)


class TestV28StopParams(unittest.TestCase):
    """止损参数存在性 — 主循环依赖这些常量"""

    def test_individual_stop_loss_params(self):
        self.assertEqual(V28_STOP_LOSS, 0.05)        # 个股止损 5%
        self.assertEqual(V28_TRAILING_STOP, 0.08)    # 个股移动止损 8%
        self.assertGreater(V28_TRAILING_STOP, V28_STOP_LOSS)

    def test_qqq_trailing_stop_param(self):
        self.assertEqual(V28_QQQ_TRAILING, 0.12)     # QQQ 移动止损 12%
        self.assertGreater(V28_QQQ_TRAILING, V28_TRAILING_STOP)

    def test_core_allocation_params(self):
        self.assertEqual(V28_CORE_SYMBOL, "QQQ")
        self.assertEqual(V28_CORE_PCT, 0.60)         # 60% 核心仓
        self.assertEqual(V28_ALPHA_COUNT, 7)         # 7 只 alpha
        self.assertEqual(V28_REBALANCE_DAYS, 63)     # 季度再平衡

    def test_alpha_universe_has_enough_candidates(self):
        self.assertGreaterEqual(len(V28_ALPHA_UNIVERSE), V28_ALPHA_COUNT)


if __name__ == "__main__":
    unittest.main()
