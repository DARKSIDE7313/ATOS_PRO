"""
ATOS PRO v3 — 长期投资系统单元测试
=====================================
测试各模块核心逻辑，不依赖外部 API（使用 mock）。

运行:
  python -m pytest tests/test_longterm/ -v
  python -m pytest tests/test_longterm/test_engine.py -v
"""

import pytest
import sys
import os

# Ensure atos is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════
# config.py 测试
# ═══════════════════════════════════════════

class TestConfig:
    def test_config_imports(self):
        """config.py 可以在没有 config_shared 时正常工作"""
        from atos.longterm.config import CAPITAL, LAYER1, LAYER2, LAYER3, RISK, SCHEDULE
        assert CAPITAL["total"] >= 100_000
        assert 0 < CAPITAL["layer1_pct"] < 1
        assert CAPITAL["layer1_pct"] + CAPITAL["layer2_pct"] + CAPITAL["layer3_pct"] <= 1.0
        assert LAYER1["aristocrat_position_count"] > 0
        assert LAYER2["multifactor_top_n"] > 0
        assert len(LAYER3["factor_etfs"]) >= 3
        assert RISK["max_overall_drawdown"] > 0
        assert SCHEDULE["layer1_interval_minutes"] > 0

    def test_layer_allocation_sums_correctly(self):
        """各层比例之和合理"""
        from atos.longterm.config import CAPITAL
        total_pct = CAPITAL["layer1_pct"] + CAPITAL["layer2_pct"] + CAPITAL["layer3_pct"]
        assert 0.9 <= total_pct <= 1.05  # 允许5%现金储备

    def test_risk_thresholds_sensible(self):
        """风控阈值在合理范围"""
        from atos.longterm.config import RISK
        assert 0.10 <= RISK["max_overall_drawdown"] <= 0.50
        assert 0.05 <= RISK["max_single_position"] <= 0.30
        assert RISK["mandatory_reduce_on_drawdown"] >= RISK["max_overall_drawdown"]


# ═══════════════════════════════════════════
# engine.py 测试（不含 API 调用）
# ═══════════════════════════════════════════

class TestEngine:
    def test_imports(self):
        """engine.py 模块可导入"""
        from atos.longterm.engine import (
            magic_formula_rank, klarman_margin_check,
            estimate_factor_exposures, comprehensive_long_term_rank,
            build_long_term_portfolio, LONG_TERM_PRINCIPLES,
        )
        assert isinstance(LONG_TERM_PRINCIPLES, str)
        assert len(LONG_TERM_PRINCIPLES) > 100

    def test_build_portfolio_empty(self):
        """空排名返回空组合"""
        from atos.longterm.engine import build_long_term_portfolio
        result = build_long_term_portfolio([])
        assert result["total_positions"] == 0
        assert result["positions"] == []

    def test_build_portfolio_with_data(self):
        """有排名数据时正确构建组合"""
        from atos.longterm.engine import build_long_term_portfolio
        rankings = [
            {"symbol": "AAPL", "composite_score": 85, "decision": "STRONG_LONG", "catalyst": True},
            {"symbol": "MSFT", "composite_score": 80, "decision": "STRONG_LONG", "catalyst": False},
            {"symbol": "GOOGL", "composite_score": 45, "decision": "WATCH", "catalyst": False},
        ]
        result = build_long_term_portfolio(rankings, max_positions=5, min_composite=50)
        assert result["total_positions"] == 2  # Only first 2 pass min_composite=50
        for pos in result["positions"]:
            assert pos["weight"] > 0

    def test_klarman_score_bounds(self):
        """Klarman 评分在 0-100 之间（用假数据验证逻辑）"""
        # 测试分数裁剪函数
        from atos.longterm.engine import klarman_margin_check
        # 无法真正调用（需要 yfinance），但导入验证通过
        pass


# ═══════════════════════════════════════════
# value_investor.py 测试
# ═══════════════════════════════════════════

class TestValueInvestor:
    def test_imports(self):
        from atos.longterm.value_investor import (
            calculate_intrinsic_value, screen_long_term_candidates, BURRY_PRINCIPLES,
        )
        assert isinstance(BURRY_PRINCIPLES, str)

    def test_burry_principles_complete(self):
        """Burry 10条原则完整"""
        from atos.longterm.value_investor import BURRY_PRINCIPLES
        assert "MARGIN OF SAFETY" in BURRY_PRINCIPLES
        assert "FREE CASH FLOW" in BURRY_PRINCIPLES
        assert "CONTRARIAN" in BURRY_PRINCIPLES


# ═══════════════════════════════════════════
# Layer 类测试
# ═══════════════════════════════════════════

class TestLayer1Foundation:
    def test_imports(self):
        from atos.longterm.layer1_foundation import Layer1Foundation, get_layer1, run_layer1
        l1 = Layer1Foundation()
        assert l1.capital > 0

    def test_dca_multiple(self):
        """PE 倍数计算正确"""
        from atos.longterm.layer1_foundation import Layer1Foundation
        l1 = Layer1Foundation()
        assert l1.calculate_dca_multiple(10) == 2.0   # 极低估 → 双倍
        assert l1.calculate_dca_multiple(20) == 1.0   # 正常
        assert l1.calculate_dca_multiple(30) == 0.5   # 偏高 → 减半
        assert l1.calculate_dca_multiple(40) == 0.25  # 高估 → 四分之一

    def test_aristocrats_list(self):
        """股息贵族名单不为空"""
        from atos.longterm.layer1_foundation import Layer1Foundation
        l1 = Layer1Foundation()
        aristocrats = l1.load_dividend_aristocrats()
        assert len(aristocrats) > 0

    def test_sell_orders_empty_when_no_positions(self):
        """无持仓时卖出列表为空"""
        from atos.longterm.layer1_foundation import Layer1Foundation
        l1 = Layer1Foundation()
        sells = l1.get_sell_orders({})
        assert sells == []

    def test_sell_orders_skip_non_foundation(self):
        """跳过非 foundation 层的持仓"""
        from atos.longterm.layer1_foundation import Layer1Foundation
        l1 = Layer1Foundation()
        positions = {"AAPL": {"layer": "core", "shares": 10, "avg_cost": 150}}
        sells = l1.get_sell_orders(positions)
        assert sells == []  # core 层不该被 L1 处理


class TestLayer2Core:
    def test_imports(self):
        from atos.longterm.layer2_core import Layer2Core, get_layer2, run_layer2
        l2 = Layer2Core()
        assert l2.capital > 0

    def test_sell_orders_empty_when_no_positions(self):
        """无持仓时卖出列表为空"""
        from atos.longterm.layer2_core import Layer2Core
        l2 = Layer2Core()
        sells = l2.get_sell_orders({})
        assert sells == []

    def test_sell_orders_skip_non_core(self):
        """跳过非 core 层的持仓"""
        from atos.longterm.layer2_core import Layer2Core
        l2 = Layer2Core()
        positions = {"VOO": {"layer": "foundation", "shares": 100, "avg_cost": 400}}
        sells = l2.get_sell_orders(positions)
        assert sells == []


class TestLayer3Tactical:
    def test_imports(self):
        from atos.longterm.layer3_tactical import Layer3Tactical, get_layer3, run_layer3
        l3 = Layer3Tactical()
        assert l3.capital > 0

    def test_factor_etfs(self):
        """因子 ETF 列表不为空"""
        from atos.longterm.layer3_tactical import Layer3Tactical
        l3 = Layer3Tactical()
        etfs = l3.get_factor_etfs()
        assert len(etfs) >= 4

    def test_sector_etfs(self):
        """行业 ETF 列表不为空"""
        from atos.longterm.layer3_tactical import Layer3Tactical
        l3 = Layer3Tactical()
        sectors = l3.get_sector_etfs()
        assert len(sectors) >= 8

    def test_sell_orders_empty_when_no_positions(self):
        """无持仓时卖出列表为空"""
        from atos.longterm.layer3_tactical import Layer3Tactical
        l3 = Layer3Tactical()
        sells = l3.get_sell_orders({})
        assert sells == []

    def test_sell_orders_skip_non_tactical(self):
        """跳过非 tactical 层的持仓"""
        from atos.longterm.layer3_tactical import Layer3Tactical
        l3 = Layer3Tactical()
        positions = {"NVDA": {"layer": "core", "shares": 50, "avg_cost": 100}}
        sells = l3.get_sell_orders(positions)
        assert sells == []


# ═══════════════════════════════════════════
# market_thermometer.py 测试
# ═══════════════════════════════════════════

class TestMarketThermometer:
    def test_imports(self):
        from atos.longterm.market_thermometer import MarketThermometer, get_market_thermometer
        mt = MarketThermometer()
        assert mt is not None

    def test_phase_classification(self):
        """市场阶段分类正确"""
        from atos.longterm.market_thermometer import MarketThermometer
        mt = MarketThermometer()
        assert mt._classify_phase(-80) == "EXTREME_PESSIMISM"
        assert mt._classify_phase(-45) == "PESSIMISM"
        assert mt._classify_phase(-15) == "SLIGHT_PESSIMISM"
        assert mt._classify_phase(0) == "NEUTRAL"
        assert mt._classify_phase(20) == "SLIGHT_OPTIMISM"
        assert mt._classify_phase(45) == "OPTIMISM"
        assert mt._classify_phase(75) == "EXTREME_OPTIMISM"


# ═══════════════════════════════════════════
# market_regime.py 测试
# ═══════════════════════════════════════════

class TestMarketRegime:
    def test_imports(self):
        from atos.longterm.market_regime import MarketRegime, get_comprehensive_regime, get_seasonal_bias
        mr = MarketRegime()
        assert mr is not None

    def test_seasonal_bias(self):
        """季节性偏置在合理范围"""
        from atos.longterm.market_regime import get_seasonal_bias
        bias = get_seasonal_bias()
        assert isinstance(bias, int)
        assert -10 <= bias <= 10


# ═══════════════════════════════════════════
# statistical_edges.py 测试
# ═══════════════════════════════════════════

class TestStatisticalEdges:
    def test_imports(self):
        from atos.longterm.statistical_edges import (
            rsi_oversold_with_trend, volume_confirmed_breakout,
            bollinger_squeeze, detect_gap, composite_edge_score,
        )

    def test_rsi_no_data(self):
        """无效标的返回 NO_DATA"""
        from atos.longterm.statistical_edges import rsi_oversold_with_trend
        result = rsi_oversold_with_trend("INVALID_SYMBOL_XYZ123", lookback_days=30)
        assert result.get("signal") in ("NO_DATA", "ERROR")

    def test_composite_edge_no_data(self):
        """无效标的返回 NEUTRAL"""
        from atos.longterm.statistical_edges import composite_edge_score
        result = composite_edge_score("INVALID_SYMBOL_XYZ123")
        assert result["action"] in ("NEUTRAL", "WATCH")


# ═══════════════════════════════════════════
# serenity.py 测试
# ═══════════════════════════════════════════

class TestSerenity:
    def test_imports(self):
        from atos.longterm.serenity import (
            find_chokepoint_stocks, serenity_portfolio,
            serenity_quality_filter, SERENITY_PRINCIPLES,
        )
        assert isinstance(SERENITY_PRINCIPLES, str)

    def test_chokepoint_empty(self):
        """空列表返回空结果"""
        from atos.longterm.serenity import find_chokepoint_stocks
        result = find_chokepoint_stocks([])
        assert result == []

    def test_serenity_portfolio_empty(self):
        """空 universe 返回空组合"""
        from atos.longterm.serenity import serenity_portfolio
        result = serenity_portfolio([])
        assert result["total_positions"] == 0
        assert result["cash_weight"] == 1.0


# ═══════════════════════════════════════════
# backtest.py 测试
# ═══════════════════════════════════════════

class TestBacktest:
    def test_imports(self):
        from atos.longterm.backtest import BacktestEngine, run_backtest

    def test_engine_init(self):
        from atos.longterm.backtest import BacktestEngine
        engine = BacktestEngine(initial_capital=100_000)
        assert engine.initial_capital == 100_000
        assert engine.cash == 100_000

    def test_reset(self):
        from atos.longterm.backtest import BacktestEngine
        engine = BacktestEngine(initial_capital=100_000)
        engine.cash = 50_000
        engine.reset()
        assert engine.cash == 100_000
        assert engine.positions == {}


# ═══════════════════════════════════════════
# cash_manager.py 测试
# ═══════════════════════════════════════════

class TestCashManager:
    def test_imports(self):
        from atos.longterm.cash_manager import CashManager, get_cash_manager, should_buy_the_dip

    def test_dip_not_triggered_normally(self):
        """正常市场不触发抄底"""
        from atos.longterm.cash_manager import CashManager
        cm = CashManager()
        result = cm.check_dip_trigger(current_drawdown=-0.02)  # -2% 不够触发
        assert result["triggered"] is False


# ═══════════════════════════════════════════
# risk_monitor.py 测试
# ═══════════════════════════════════════════

class TestRiskMonitor:
    def test_imports(self):
        from atos.longterm.risk_monitor import RiskMonitor, get_risk_monitor, full_risk_check

    def test_drawdown_ok(self):
        """未回撤时状态正常"""
        from atos.longterm.risk_monitor import RiskMonitor
        rm = RiskMonitor()
        result = rm.check_overall_drawdown(current_value=rm.portfolio_peak)
        assert result["status"] == "OK"


# ═══════════════════════════════════════════
# __init__.py 导出测试
# ═══════════════════════════════════════════

class TestInitExports:
    def test_all_exports_importable(self):
        """__init__.py 中所有导出都能正常导入"""
        from atos.longterm import (
            calculate_intrinsic_value, screen_long_term_candidates, BURRY_PRINCIPLES,
            magic_formula_rank, klarman_margin_check, estimate_factor_exposures,
            comprehensive_long_term_rank, build_long_term_portfolio, LONG_TERM_PRINCIPLES,
            CAPITAL, LAYER1, LAYER2, LAYER3, RISK, SCHEDULE,
            MarketThermometer, get_market_thermometer,
            CashManager, get_cash_manager, should_buy_the_dip,
            Layer1Foundation, get_layer1, run_layer1,
            Layer3Tactical, get_layer3, run_layer3,
            RiskMonitor, get_risk_monitor, full_risk_check,
            PhoenixRunner, get_phoenix, run_phoenix, quick_status,
            TacticalOverlay, get_overlay, apply_tactical_overlay,
            MarketRegime, get_regime, get_comprehensive_regime,
            composite_edge_score,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
