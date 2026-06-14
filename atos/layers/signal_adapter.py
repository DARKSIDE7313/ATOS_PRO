"""signal_adapter.py — Layer 2 → Layer 3 標準化適配"""
import logging
from datetime import datetime, timezone
from typing import Callable
from .vibe_bridge import AtosSignal, load_cached_signals

logger = logging.getLogger("atos.signal_adapter")
_DIR_MAP = {"long": 1, "short": -1, "flat": 0}


class SignalAdapter:
    """將 Vibe 信號轉換為 ATOS Layer 3 (執行層) 可用的標準格式"""

    def __init__(self, min_confidence: float = 0.55, max_signals: int = 10):
        self.min_confidence = min_confidence
        self.max_signals = max_signals

    def filter_and_rank(self, signals: list[AtosSignal]) -> list[AtosSignal]:
        valid = [
            s
            for s in signals
            if s.is_valid()
            and s.confidence >= self.min_confidence
            and s.direction != "flat"
        ]
        return sorted(valid, key=lambda s: s.confidence, reverse=True)[
            : self.max_signals
        ]

    def to_layer3_item(self, signal: AtosSignal, kelly_size: float) -> dict:
        return {
            "ticker": signal.ticker,
            "direction": signal.direction,
            "direction_int": _DIR_MAP.get(signal.direction, 0),
            "confidence": round(signal.confidence, 4),
            "position_size": round(kelly_size, 4),
            "source": signal.source,
            "reason": signal.reason,
            "generated_at": signal.generated_at,
            "expires_at": signal.expires_at,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    def adapt(
        self, signals: list[AtosSignal], kelly_fn: Callable
    ) -> list[dict]:
        ranked = self.filter_and_rank(signals)
        result = []
        for sig in ranked:
            try:
                size = kelly_fn(sig.ticker, sig.confidence)
                adjusted = round(min(size, 1.0), 4)
                result.append(self.to_layer3_item(sig, adjusted))
            except Exception as e:
                logger.error(
                    "[SignalAdapter] Kelly failed for %s: %s", sig.ticker, e
                )
        logger.info("[SignalAdapter] %d → %d signals", len(signals), len(result))
        return result

    def fallback_adapt(self, kelly_fn: Callable) -> list[dict]:
        logger.warning("[SignalAdapter] Fallback to cached signals.")
        return self.adapt(load_cached_signals(), kelly_fn)
