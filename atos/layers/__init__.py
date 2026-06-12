"""
ATOS Layers — Vibe-Trading Integration (Layer 2)
=================================================
VibeBridge connects ATOS PRO to the Vibe-Trading multi-agent quant desk.
Provides morning scans, alpha benchmarking, and shadow trade review.
"""

from atos.layers.vibe_bridge import VibeBridge, AtosSignal, load_cached_signals
from atos.layers.signal_adapter import SignalAdapter
from atos.layers.scheduler_patch import register_vibe_jobs

__all__ = [
    "VibeBridge",
    "AtosSignal",
    "load_cached_signals",
    "SignalAdapter",
    "register_vibe_jobs",
]
