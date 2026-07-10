"""
ATOS PRO v3 — 长期价值投资系统
===============================
Greenblatt 神奇公式 + Klarman 安全边际 + Marks 周期 + FF 多因子
+ Phoenix 凤凰长线综合策略（三层架构，含完整买入+卖出逻辑）
+ V3 Statistical Edges（91 页历史数据研究支持）
+ Backtest 回测引擎
"""
from atos.longterm.value_investor import calculate_intrinsic_value, screen_long_term_candidates, BURRY_PRINCIPLES
from atos.longterm.engine import (
    magic_formula_rank, klarman_margin_check, estimate_factor_exposures,
    comprehensive_long_term_rank, build_long_term_portfolio, LONG_TERM_PRINCIPLES,
)
from atos.longterm.config import CAPITAL, LAYER1, LAYER2, LAYER3, RISK, SCHEDULE
from atos.longterm.market_thermometer import MarketThermometer, get_market_thermometer
from atos.longterm.cash_manager import CashManager, get_cash_manager, should_buy_the_dip
from atos.longterm.layer1_foundation import Layer1Foundation, get_layer1, run_layer1
from atos.longterm.layer2_core import Layer2Core, get_layer2, run_layer2
from atos.longterm.layer3_tactical import Layer3Tactical, get_layer3, run_layer3
from atos.longterm.risk_monitor import RiskMonitor, get_risk_monitor, full_risk_check
from atos.longterm.phoenix_runner import PhoenixRunner, get_phoenix, run_phoenix, quick_status
from atos.longterm.tactical_overlay import TacticalOverlay, get_overlay, apply_tactical_overlay
from atos.longterm.market_regime import MarketRegime, get_regime, get_comprehensive_regime
from atos.longterm.statistical_edges import composite_edge_score
from atos.longterm.backtest import BacktestEngine, run_backtest

__all__ = [
    "calculate_intrinsic_value", "screen_long_term_candidates", "BURRY_PRINCIPLES",
    "magic_formula_rank", "klarman_margin_check", "estimate_factor_exposures",
    "comprehensive_long_term_rank", "build_long_term_portfolio", "LONG_TERM_PRINCIPLES",
    "CAPITAL", "LAYER1", "LAYER2", "LAYER3", "RISK", "SCHEDULE",
    "MarketThermometer", "get_market_thermometer",
    "CashManager", "get_cash_manager", "should_buy_the_dip",
    "Layer1Foundation", "get_layer1", "run_layer1",
    "Layer2Core", "get_layer2", "run_layer2",
    "Layer3Tactical", "get_layer3", "run_layer3",
    "RiskMonitor", "get_risk_monitor", "full_risk_check",
    "PhoenixRunner", "get_phoenix", "run_phoenix", "quick_status",
    "TacticalOverlay", "get_overlay", "apply_tactical_overlay",
    "MarketRegime", "get_regime", "get_comprehensive_regime",
    "composite_edge_score",
    "BacktestEngine", "run_backtest",
]
