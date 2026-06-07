"""
ATOS PRO v3 — Tactical Overlay for Phoenix
=============================================
将统计优势信号 + 市场机制过滤器注入 Phoenix 策略。

Pipeline: 去重 → 机制调整 → 统计过滤 → 执行
统计过滤自带缓存，避免重复 API 调用。
"""

import yfinance as yf
import datetime
from atos.core.logging import get_logger
from atos.longterm.market_regime import get_comprehensive_regime, get_seasonal_bias
from atos.longterm.statistical_edges import composite_edge_score

logger = get_logger("phoenix.overlay")


class TacticalOverlay:

    def __init__(self):
        self.last_regime = None
        self.last_overlay = {}
        self._edge_cache = {}

    def get_full_overlay(self) -> dict:
        regime = get_comprehensive_regime()
        seasonal = get_seasonal_bias()
        regime_mult = regime["position_multiplier"]
        seasonal_adj = 1.0 + seasonal * 0.005
        final_mult = regime_mult * seasonal_adj

        explanation = []
        if regime["regime"] in ("BULL_STRONG", "BULL_WEAK"):
            explanation.append("long")
        if regime["regime"] in ("BEAR", "CRISIS"):
            explanation.append("defense")
        if regime["yield_curve"]["inverted"]:
            explanation.append("yield_inverted")
        if seasonal > 0:
            explanation.append(f"seasonal+{seasonal}")
        elif seasonal < 0:
            explanation.append(f"seasonal{seasonal}")

        self.last_overlay = {
            "regime": regime["regime"],
            "regime_multiplier": round(regime_mult, 2),
            "seasonal_adj": round(seasonal_adj, 2),
            "final_position_multiplier": round(final_mult, 2),
            "explanation": "|".join(explanation) if explanation else "normal",
            "seasonal_bias": seasonal,
            "yield_curve_inverted": regime["yield_curve"]["inverted"],
            "vix_signal": regime["vix_signal"]["signal"],
        }
        return self.last_overlay

    def screen_orders(self, orders: list[dict]) -> tuple[list[dict], list[dict]]:
        passed = []
        filtered = []

        for order in orders:
            symbol = order.get("symbol", "")
            if not symbol:
                filtered.append(order)
                continue

            # Use cache to avoid repeated API calls
            if symbol in self._edge_cache:
                edge = self._edge_cache[symbol]
            else:
                edge = composite_edge_score(symbol)
                self._edge_cache[symbol] = edge

            confidence = edge["composite_confidence"]

            if confidence >= 70:
                order["edge_confidence"] = confidence
                order["edge_signals"] = edge["signals"]
                passed.append(order)
            elif confidence >= 50:
                order["edge_confidence"] = confidence
                if order.get("quantity", 0) > 1:
                    order["quantity"] = max(1, order["quantity"] // 2)
                passed.append(order)
            else:
                filtered.append(order)

        if filtered:
            logger.info(f"stat_filter: {len(orders)}->{len(passed)} pass, {len(filtered)} cut")

        return passed, filtered

    def adjust_for_regime(self, orders: list[dict]) -> list[dict]:
        overlay = self.get_full_overlay()
        mult = overlay["final_position_multiplier"]
        if mult == 1.0:
            return orders
        for order in orders:
            order["quantity"] = max(1, int(order.get("quantity", 0) * mult))
            order["regime_adjusted"] = True
        logger.info(f"regime: {mult:.2f}x ({overlay['regime']})")
        return orders

    def full_pipeline(self, orders: list[dict]) -> tuple[list[dict], dict]:
        if not orders:
            return [], {"pipeline": "empty"}
        orders = self.adjust_for_regime(orders)
        passed, filtered = self.screen_orders(orders)
        report = {
            "pipeline": "tactical_overlay",
            "total_in": len(orders), "passed": len(passed), "filtered": len(filtered),
            "overlay": self.last_overlay,
        }
        logger.info(f"overlay done: {len(orders)}->{len(passed)}")
        return passed, report


_overlay_instance: TacticalOverlay = None

def get_overlay() -> TacticalOverlay:
    global _overlay_instance
    if _overlay_instance is None:
        _overlay_instance = TacticalOverlay()
    return _overlay_instance

def apply_tactical_overlay(orders: list[dict]) -> tuple[list[dict], dict]:
    return get_overlay().full_pipeline(orders)
