"""
ATOS PRO v2 — 专业级风控升级
=============================
基于 2025-2026 量化最佳实践：
  1. 三重障碍法 (Triple-Barrier) — 替代固定止盈止损
  2. 波动率目标仓位 — Robert Carver 风格
  3. 动态追踪止损 — 随盈利上移
  4. 回撤后减仓 — 亏损后缩小仓位
"""

import math
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("risk.professional")


# ========== 1. 三重障碍法 (Triple-Barrier) ==========

def triple_barrier(entry_price: float, current_price: float,
                   entry_time_days: float, current_time_days: float,
                   volatility: float = 0.02,
                   max_hold_days: int = 20) -> dict:
    """
    三重障碍法 — 替代固定止盈止损。

    三个退出条件，哪个先触发就退出：
      障碍1 (止盈): 价格达到 volatility 倍数的盈利
      障碍2 (止损): 价格跌破 volatility 倍数的亏损
      障碍3 (时间): 持仓时间超过上限

    返回: {"exit": True/False, "reason": "...", "barrier": "profit|stop|time"}
    """
    # 计算自适应障碍（基于当前波动率）
    if entry_price <= 0:
        return {"exit": False, "reason": "triple_barrier: entry_price无效", "barrier": None, "pnl_pct": 0}
    profit_barrier = entry_price * (1 + volatility * 2.0)   # 2倍波动率止盈
    stop_barrier = entry_price * (1 - volatility * 1.5)     # 1.5倍波动率止损

    # 障碍1: 止盈
    if current_price >= profit_barrier:
        return {
            "exit": True,
            "reason": f"三重障碍-止盈: ${current_price:.2f} ≥ ${profit_barrier:.2f}",
            "barrier": "profit",
            "pnl_pct": round((current_price - entry_price) / entry_price, 4),
        }

    # 障碍2: 止损
    if current_price <= stop_barrier:
        return {
            "exit": True,
            "reason": f"三重障碍-止损: ${current_price:.2f} ≤ ${stop_barrier:.2f}",
            "barrier": "stop",
            "pnl_pct": round((current_price - entry_price) / entry_price, 4),
        }

    # 障碍3: 时间到期
    hold_days = current_time_days - entry_time_days
    if hold_days >= max_hold_days:
        return {
            "exit": True,
            "reason": f"三重障碍-时间: 持仓{hold_days:.0f}天 ≥ {max_hold_days}天",
            "barrier": "time",
            "pnl_pct": round((current_price - entry_price) / entry_price, 4),
            "hold_days": hold_days,
        }

    return {"exit": False, "hold_days": hold_days}


# ========== 2. 波动率目标仓位 ==========

def vol_target_position(capital: float, price: float,
                         volatility: float,
                         target_annual_vol: float = 0.15,
                         max_position_pct: float = 0.25) -> dict:
    """
    Robert Carver 风格的波动率目标：

    仓位大小 = (目标年化波动率 / 实际年化波动率) × 资本

    如果当前市场波动大 → 自动减仓
    如果当前市场波动小 → 可以多配
    """
    if price <= 0 or volatility <= 0:
        return {"shares": 0, "weight": 0, "reason": "无效价格或波动率"}

    # 年化波动率（日波动率 × √252）
    ann_vol = volatility * math.sqrt(252)

    # 波动率调整系数
    vol_scalar = target_annual_vol / ann_vol if ann_vol > 0 else 1.0

    # 上限
    vol_scalar = min(vol_scalar, 2.0)   # 低波动最多放大2倍
    vol_scalar = max(vol_scalar, 0.1)   # 高波动最少保留10%

    # 目标仓位
    target_weight = vol_scalar * max_position_pct
    target_weight = min(target_weight, max_position_pct)
    target_value = capital * target_weight
    shares = max(1, int(target_value / price))

    return {
        "shares": shares,
        "weight": round(target_weight, 4),
        "ann_vol": round(ann_vol, 4),
        "vol_scalar": round(vol_scalar, 2),
        "reason": (
            f"年化波动={ann_vol:.1%} → 调整系数={vol_scalar:.2f} → "
            f"目标仓位={target_weight:.1%}"
        ),
    }


# ========== 3. 动态追踪止损 ==========

class TrailingStop:
    """
    追踪止损 — 随着盈利上移止损线。

    例如：买入价 $100，追踪 3%
      - 涨到 $110 → 止损线上移到 $106.7
      - 跌回 $106.7 → 卖出，锁定 $6.7 利润
      - 不会在 $103 就卖出（固定止损会）
    """

    def __init__(self, trail_pct: float = 0.05, confirm_cycles: int = 3):
        self.trail_pct = trail_pct
        self.highest_price = 0.0
        self.stop_price = 0.0
        self.entry_price = 0.0
        self.confirm_cycles = confirm_cycles
        self._breach_count = 0

    def init(self, entry_price: float):
        self.entry_price = entry_price
        self.highest_price = entry_price
        self.stop_price = entry_price * (1 - self.trail_pct)
        self._breach_count = 0

    def update(self, current_price: float) -> dict:
        """返回是否触发止损（需连续N次跌破确认，防5分钟噪音误杀）"""
        if current_price > self.highest_price:
            self.highest_price = current_price
            self.stop_price = self.highest_price * (1 - self.trail_pct)
            self._breach_count = 0

        breached = current_price <= self.stop_price
        if breached:
            self._breach_count += 1
        else:
            self._breach_count = 0

        triggered = self._breach_count >= self.confirm_cycles
        profit_pct = ((current_price - self.entry_price) / self.entry_price
                      if self.entry_price > 0 else 0.0)

        return {
            "triggered": triggered,
            "current_price": current_price,
            "highest_price": self.highest_price,
            "stop_price": round(self.stop_price, 2),
            "profit_from_peak": round((current_price - self.highest_price) / self.highest_price, 4) if self.highest_price > 0 else 0,
            "unrealized_pnl": round(profit_pct, 4),
            "breach_count": self._breach_count,
            "confirm_cycles": self.confirm_cycles,
            "reason": (
                f"追踪止损触发: ${current_price:.2f} ≤ ${self.stop_price:.2f} (确认{self._breach_count}/{self.confirm_cycles}次)"
                if triggered else
                f"追踪中: 高点${self.highest_price:.2f} 止损${self.stop_price:.2f}" +
                (f" [跌破{self._breach_count}/{self.confirm_cycles}]" if breached else "")
            ),
        }


# ========== 4. 回撤后减仓 ==========

def kelly_after_drawdown(base_kelly_pct: float,
                          current_drawdown: float,
                          max_drawdown_limit: float = 0.10) -> dict:
    """
    亏钱后自动缩小仓位 — 防止连亏时越亏越多。

    规则：
      - 回撤 < 2%  → 正常仓位
      - 回撤 2-5%  → 仓位减半
      - 回撤 5-10% → 仓位减到 1/4
      - 回撤 > 10% → 暂停新开仓
    """
    if current_drawdown < 0.02:
        scale = 1.0
        status = "正常"
    elif current_drawdown < 0.05:
        scale = 0.5
        status = "减半"
    elif current_drawdown < max_drawdown_limit:
        scale = 0.25
        status = "大幅减仓"
    else:
        scale = 0.0
        status = "暂停新开仓"

    adjusted_kelly = base_kelly_pct * scale

    logger.info(
        f"回撤调整: 回撤={current_drawdown:.2%} → "
        f"系数={scale:.0%} → Kelly={base_kelly_pct:.1%}→{adjusted_kelly:.1%} [{status}]"
    )

    return {
        "current_drawdown": round(current_drawdown, 4),
        "scale": scale,
        "base_kelly": round(base_kelly_pct, 4),
        "adjusted_kelly": round(adjusted_kelly, 4),
        "status": status,
    }
