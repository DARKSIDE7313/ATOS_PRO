"""
ATOS PRO v3 — AI 决策引擎
=========================
多理论辩论 + 记忆学习 + 置信度校准

v3 变更：AI 只有否决权，主决策由因子引擎完成。
保留旧函数名兼容。

Phase 7 (2026-08-25): engine_v2 / debate 已归档至 archive/phase7_deadcode_20260825/，
移除对应重导出。当前唯一活跃实现: atos/ai/advisor_enhanced.py (v6)。
"""
# 兼容旧接口（返回空壳）
def debate(*args, **kwargs):
    return {"symbol": "", "final_action": "HOLD", "final_confidence": 0.3,
            "analyst_opinions": {}, "debate_summary": "DEPRECATED", "risk_flags": []}

def batch_debate(*args, **kwargs):
    return []

class ANALYSTS:
    """DEPRECATED v3"""
    pass

from atos.ai.memory import (
    record_decision, record_outcome, get_similar_history,
    get_mistake_patterns, detect_and_record_pattern,
    get_ai_confidence_adjustment, get_memory_stats, init_db,
)

__all__ = [
    "debate", "batch_debate", "ANALYSTS",
    "record_decision", "record_outcome", "get_similar_history",
    "get_mistake_patterns", "detect_and_record_pattern",
    "get_ai_confidence_adjustment", "get_memory_stats", "init_db",
]
