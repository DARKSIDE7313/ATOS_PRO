"""
ATOS PRO v2 — AI 决策引擎
=========================
多理论辩论 + 记忆学习 + 置信度校准
"""
from atos.ai.engine_v2 import get_advice_v2
from atos.ai.debate import debate, batch_debate, ANALYSTS
from atos.ai.memory import (
    record_decision, record_outcome, get_similar_history,
    get_mistake_patterns, detect_and_record_pattern,
    get_ai_confidence_adjustment, get_memory_stats, init_db,
)

__all__ = [
    "get_advice_v2",
    "debate", "batch_debate", "ANALYSTS",
    "record_decision", "record_outcome", "get_similar_history",
    "get_mistake_patterns", "detect_and_record_pattern",
    "get_ai_confidence_adjustment", "get_memory_stats", "init_db",
]
