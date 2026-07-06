"""
ATOS PRO v5 — 因子工厂
=======================
价值 + 动量 + 质量 + 技术 + 盈利修正 + 巴菲特过滤器 = 综合评分
"""
from atos.factors.value import get_value_factors, batch_value_factors
from atos.factors.momentum import get_momentum_factors, batch_momentum_factors
from atos.factors.quality import get_quality_factors, batch_quality_factors
from atos.factors.earnings import get_earnings_revision, batch_earnings_revision
from atos.factors.buffett_checklist import (
    quality_elimination_check, quick_veto_check,
    batch_buffett_filter, HARD_VETO_CHECKS,
)
from atos.factors.engine import (
    combine, ic_analysis, get_top_picks,
    DEFAULT_WEIGHTS, REGIME_WEIGHTS,
)

__all__ = [
    "get_value_factors", "batch_value_factors",
    "get_momentum_factors", "batch_momentum_factors",
    "get_quality_factors", "batch_quality_factors",
    "get_earnings_revision", "batch_earnings_revision",
    "quality_elimination_check", "quick_veto_check",
    "batch_buffett_filter", "HARD_VETO_CHECKS",
    "combine", "ic_analysis", "get_top_picks",
    "DEFAULT_WEIGHTS", "REGIME_WEIGHTS",
]
