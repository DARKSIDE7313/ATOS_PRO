"""
Regime Engine — 市场状态引擎
================================
使用多维度指标判断当前市场状态（BULL_STRONG / BULL_WEAK / SIDEWAYS / BEAR / HIGH_VOL）。

指标来源：
  - SPY vs MA200（趋势方向）
  - VIX 绝对值 + 52周百分位（恐慌程度）
  - 市场宽度（MA50以上股票占比 → 广度确认）
  - 5天中位数平滑（防止频繁变脸）
"""
import pandas as pd
import numpy as np
from statistics import median


class RegimeEngine:
    """市场状态引擎 — 多维度综合判断"""

    def __init__(self):
        self.spy_prices = []         # SPY 日线收盘价序列
        self.vix_prices = []         # VIX 日线收盘价序列
        self.breadth_values = []     # 市场宽度序列 (0~1, MA50以上占比)
        self.regime_history = []     # 历史状态序列（用于平滑）

    def update(self, spy_close: float, vix_close: float = 15.0,
               breadth: float | None = None):
        """
        更新一个交易日的数据。

        Parameters
        ----------
        spy_close : float
            SPY 当日收盘价
        vix_close : float
            VIX 当日收盘价
        breadth : float, optional
            市场宽度 — MA50 以上股票占比（0~1），如 0.65 = 65%
        """
        self.spy_prices.append(spy_close)
        self.vix_prices.append(vix_close)
        if breadth is not None:
            self.breadth_values.append(breadth)

    def _compute_ma200(self, series: pd.Series) -> float | None:
        if len(series) < 200:
            return None
        return series.rolling(200).mean().iloc[-1]

    def _vix_percentile(self) -> float:
        """
        计算 VIX 在其自身 52 周（252 交易日）范围内的百分位。
        返回 0~1，数值越大说明当前 VIX 越接近自身高点。
        """
        if len(self.vix_prices) < 20:
            return 0.5  # 数据不足，保守取中位
        window = min(252, len(self.vix_prices))
        recent = self.vix_prices[-window:]
        current = recent[-1]
        low = min(recent)
        high = max(recent)
        if high == low:
            return 0.5
        return (current - low) / (high - low)

    def _breadth_signal(self) -> str:
        """
        根据市场宽度判断广度状态。
        breadth > 0.60 → 广泛参与（健康）
        breadth < 0.40 → 广度恶化（危险信号）
        无数据 → 返回 UNKNOWN
        """
        if len(self.breadth_values) < 1:
            return "UNKNOWN"
        current = self.breadth_values[-1]
        if current >= 0.60:
            return "HEALTHY"
        elif current >= 0.40:
            return "MIXED"
        else:
            return "WEAK"

    def _get_raw_regime(self) -> dict:
        """基于最新数据的原始（未平滑）状态判断"""
        if len(self.spy_prices) < 200:
            return {"regime": "UNKNOWN", "risk_multiplier": 0.5}

        spy = pd.Series(self.spy_prices)
        vix = pd.Series(self.vix_prices)

        spy_ma200 = self._compute_ma200(spy)
        if spy_ma200 is None:
            return {"regime": "UNKNOWN", "risk_multiplier": 0.5}

        current_spy = float(spy.iloc[-1])
        current_vix = float(vix.iloc[-1])
        vix_pctile = self._vix_percentile()
        breadth_sig = self._breadth_signal()

        # SPY 相对于 MA200 的偏离百分比
        spy_deviation = (current_spy - spy_ma200) / spy_ma200

        # ── 判断逻辑 ──

        # SIDEWAYS: SPY 在 MA200 ±3% 以内 且 VIX < 20
        if abs(spy_deviation) <= 0.03 and current_vix < 20:
            return {
                "regime": "SIDEWAYS",
                "risk_multiplier": 0.5,
                "vix_percentile": round(vix_pctile, 3),
                "breadth": breadth_sig,
            }

        # HIGH_VOL: VIX > 30（无论 SPY 位置如何，优先）
        if current_vix > 30:
            # 极度恐慌时仍然保留 0.2 的仓位（比原版 0.0 更合理）
            rm = 0.2 if current_vix > 40 else 0.3
            return {
                "regime": "HIGH_VOL",
                "risk_multiplier": rm,
                "vix_percentile": round(vix_pctile, 3),
                "breadth": breadth_sig,
            }

        # BULL_STRONG: SPY > MA200, VIX < 20, 广度健康
        if current_spy > spy_ma200 and current_vix < 20 and (breadth_sig == "HEALTHY" or breadth_sig == "UNKNOWN"):
            return {
                "regime": "BULL_STRONG",
                "risk_multiplier": 1.0,
                "vix_percentile": round(vix_pctile, 3),
                "breadth": breadth_sig,
            }

        # BULL_WEAK: SPY > MA200, VIX ≤ 30
        if current_spy > spy_ma200 and current_vix <= 30:
            rm = 0.6
            # 如果广度恶化，进一步降低风险暴露
            if breadth_sig == "WEAK":
                rm = 0.4
            return {
                "regime": "BULL_WEAK",
                "risk_multiplier": rm,
                "vix_percentile": round(vix_pctile, 3),
                "breadth": breadth_sig,
            }

        # BEAR: SPY < MA200 且 VIX 不高（VIX > 30 已在前面处理）
        # 使用 0.2 而非 0.0 — 即使熊市也可以保留小仓位
        rm = 0.2
        if vix_pctile > 0.7 or breadth_sig == "WEAK":
            rm = 0.1  # VIX 处于高位或广度弱 → 更保守
        return {
            "regime": "BEAR",
            "risk_multiplier": rm,
            "vix_percentile": round(vix_pctile, 3),
            "breadth": breadth_sig,
        }

    def get_regime(self) -> dict:
        """
        返回平滑后的市场状态（5天运行中位数防 whipsaw）。

        Returns
        -------
        dict
            {"regime": str, "risk_multiplier": float, ...}
        """
        raw = self._get_raw_regime()
        self.regime_history.append(raw["regime"])

        # 需要至少 5 个历史状态才能平滑
        if len(self.regime_history) >= 5:
            recent = self.regime_history[-5:]
            # Use mode (most common) instead of median — string median is alphabetical
            from collections import Counter
            smoothed = Counter(recent).most_common(1)[0][0]
        else:
            smoothed = raw["regime"]

        # 构建返回结果（保留原始状态的额外字段）
        result = dict(raw)
        result["regime"] = smoothed
        result["raw_regime"] = raw["regime"]  # 保留原始未平滑值供调试
        result["n_history"] = len(self.regime_history)
        return result
