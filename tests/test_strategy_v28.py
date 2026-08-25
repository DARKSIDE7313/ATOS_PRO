"""strategy_v28 单元测试 — atos/shadow/strategy_v28.py

覆盖:
  - is_v28_position 持仓判定（QQQ 核心仓 + alpha 动量池）
  - 止损/移动止损参数存在性（v28 与 QQQ 各自独立）

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
)


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
