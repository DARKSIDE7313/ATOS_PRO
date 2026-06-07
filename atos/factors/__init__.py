"""
ATOS PRO v2 — 因子工厂
=======================
价值 + 动量 + 质量 + 技术 = 综合评分
"""
from atos.factors.value import get_value_factors, batch_value_factors
from atos.factors.momentum import get_momentum_factors, batch_momentum_factors
from atos.factors.quality import get_quality_factors, batch_quality_factors
from atos.factors.engine import (
    combine, ic_analysis, get_top_picks,
    DEFAULT_WEIGHTS, REGIME_WEIGHTS,
)

__all__ = [
    "get_value_factors", "batch_value_factors",
    "get_momentum_factors", "batch_momentum_factors",
    "get_quality_factors", "batch_quality_factors",
    "combine", "ic_analysis", "get_top_picks",
    "DEFAULT_WEIGHTS", "REGIME_WEIGHTS",
]
