"""
ATOS PRO v2 — Shadow Trading
=============================
本地模拟交易，不连 FutuOpenD，零风险验证策略。
"""
from atos.shadow.shadow_trader import ShadowAccount, run_shadow_cycle

__all__ = ["ShadowAccount", "run_shadow_cycle"]
