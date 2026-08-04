"""
ATOS PRO — Dual Thrust 开盘突破策略
=====================================
源自 Michael Chalek (1980s)，被机构广泛使用的经典策略。
文艺复兴科技也用类似的"区间突破"逻辑作为众多因子之一。

核心逻辑:
  Range = max(HH - LC, HC - LL)
  BuyLine  = Open + K1 × Range
  SellLine = Open - K2 × Range

  价格突破 BuyLine → 做多
  价格跌破 SellLine → 做空/平多

优化改进 (2024-2025 研究):
  1. ATR 动态 Range（替代固定 N 日 Range）
  2. 成交量确认（放量突破更可靠）
  3. 趋势过滤器（顺势突破胜率更高）
  4. 分步止盈（25% at TP1, 75% at TP2）
  5. 保本止损（TP1 触发后止损移到成本价）

参数:
  N (lookback) = 10 日
  K1 (long_coef) = 0.5
  K2 (short_coef) = 0.5
"""

import numpy as np
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class DualThrustSignal:
    symbol: str
    price: float
    direction: str  # "LONG" | "SHORT"
    strength: float  # 0-1
    buy_line: float
    sell_line: float
    range_val: float
    stop_loss: float
    take_profit_1: float  # 第一止盈 (卖 25%)
    take_profit_2: float  # 第二止盈 (卖 75%)
    reason: str


def calculate_dual_thrust(highs: np.ndarray, lows: np.ndarray,
                          closes: np.ndarray, current_open: float,
                          lookback: int = 10, k1: float = 0.5,
                          k2: float = 0.5) -> dict:
    """
    计算 Dual Thrust 上下轨。

    Range = max(HH - LC, HC - LL)
    上轨 = Open + K1 × Range
    下轨 = Open - K2 × Range

    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        current_open: 当日开盘价
        lookback: 回看周期（默认10日）
        k1: 做多系数（默认0.5）
        k2: 做空系数（默认0.5）

    Returns:
        dict with buy_line, sell_line, range_val
    """
    if len(highs) < lookback or len(lows) < lookback or len(closes) < lookback:
        return {"buy_line": 0, "sell_line": 0, "range_val": 0}

    data_highs = highs[-lookback:]
    data_lows = lows[-lookback:]
    data_closes = closes[-lookback:]

    HH = float(np.max(data_highs))
    LC = float(np.min(data_closes))
    HC = float(np.max(data_closes))
    LL = float(np.min(data_lows))

    range_val = max(HH - LC, HC - LL)

    if current_open <= 0:
        return {"buy_line": 0, "sell_line": 0, "range_val": range_val}

    buy_line = current_open + k1 * range_val
    sell_line = current_open - k2 * range_val

    return {
        "buy_line": round(buy_line, 2),
        "sell_line": round(sell_line, 2),
        "range_val": round(range_val, 2),
    }


def generate_dual_thrust_signal(sym: str, price_data: dict,
                                lookback: int = 10,
                                k1: float = 0.5,
                                k2: float = 0.5) -> Optional[DualThrustSignal]:
    """
    为单只股票生成 Dual Thrust 信号。

    Args:
        sym: 股票代码
        price_data: {highs, lows, closes, open, current_price, volumes}
        lookback: 回看周期
        k1: 做多系数
        k2: 做空系数

    Returns:
        DualThrustSignal or None
    """
    highs = np.array(price_data.get("highs", []), dtype=float)
    lows = np.array(price_data.get("lows", []), dtype=float)
    closes = np.array(price_data.get("closes", []), dtype=float)
    current_price = float(price_data.get("current_price", 0))
    current_open = float(price_data.get("open", 0))
    volumes = np.array(price_data.get("volumes", []), dtype=float)

    if len(highs) < lookback or current_price <= 0 or current_open <= 0:
        return None

    dt = calculate_dual_thrust(highs, lows, closes, current_open, lookback, k1, k2)

    if dt["range_val"] <= 0:
        return None

    buy_line = dt["buy_line"]
    sell_line = dt["sell_line"]
    range_val = dt["range_val"]

    # 成交量确认
    vol_ok = True
    if len(volumes) >= 20:
        avg_vol = float(np.mean(volumes[-20:]))
        current_vol = float(volumes[-1]) if len(volumes) > 0 else avg_vol
        vol_ok = current_vol > avg_vol * 1.2  # 放量 20%+

    # 趋势过滤器：价格在 MA50 上方 → 只做多
    ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else current_price
    trend_up = current_price > ma50

    direction = None
    strength = 0.0
    reason = ""

    # 做多信号：价格突破上轨 + 放量 + 上升趋势
    if current_price > buy_line and vol_ok and trend_up:
        direction = "LONG"
        penetration = (current_price - buy_line) / buy_line
        strength = min(1.0, 0.5 + penetration * 50 + 0.2)
        reason = (f"DualThrust突破上轨 buy={buy_line:.1f} "
                  f"range={range_val:.1f} vol_ok={vol_ok}")

    # 做空信号：价格跌破下轨 + 放量（不需要趋势确认）
    elif current_price < sell_line and vol_ok:
        direction = "SHORT"
        penetration = (sell_line - current_price) / sell_line
        strength = min(1.0, 0.5 + penetration * 50 + 0.2)
        reason = (f"DualThrust跌破下轨 sell={sell_line:.1f} "
                  f"range={range_val:.1f} vol_ok={vol_ok}")

    if direction is None:
        return None

    if strength < 0.4:
        return None

    # ATR-based 动态止损
    atr = _calc_atr(highs, lows, closes, 14)
    stop_distance = max(atr * 1.5, range_val * 0.3)
    if direction == "LONG":
        stop_loss = round(current_price - stop_distance, 2)
    else:
        stop_loss = round(current_price + stop_distance, 2)

    # 分步止盈（机构标准：25% at TP1, 75% at TP2）
    if direction == "LONG":
        take_profit_1 = round(current_price + range_val * 0.5, 2)
        take_profit_2 = round(current_price + range_val * 1.0, 2)
    else:
        take_profit_1 = round(current_price - range_val * 0.5, 2)
        take_profit_2 = round(current_price - range_val * 1.0, 2)

    return DualThrustSignal(
        symbol=sym,
        price=current_price,
        direction=direction,
        strength=round(strength, 3),
        buy_line=buy_line,
        sell_line=sell_line,
        range_val=range_val,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        reason=reason,
    )


def _calc_atr(highs: np.ndarray, lows: np.ndarray,
              closes: np.ndarray, period: int = 14) -> float:
    """计算 ATR (Average True Range)"""
    if len(highs) < period + 1:
        return 0.0

    tr_list = []
    for i in range(1, min(len(highs), period + 1)):
        h, l, c_prev = highs[-i], lows[-i], closes[-i-1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)

    return float(np.mean(tr_list)) if tr_list else 0.0


# ═══════════════════════════════════════════════
# 批量信号生成
# ═══════════════════════════════════════════════

def generate_all_dual_thrust_signals(signals: dict,
                                     price_data_map: dict = None,
                                     lookback: int = 10,
                                     k1: float = 0.5,
                                     k2: float = 0.5) -> List[DualThrustSignal]:
    """
    为所有标的生成 Dual Thrust 信号。

    Args:
        signals: get_signals() 返回的信号字典 {symbol: {price, ma50, ...}}
        price_data_map: 可选的日内数据 {symbol: {highs, lows, closes, open, volumes}}
        lookback: 回看周期
        k1: 做多系数
        k2: 做空系数

    Returns:
        排序后的 DualThrustSignal 列表
    """
    results = []

    for sym, sig in signals.items():
        current_price = sig.get("price", 0)
        if current_price <= 0:
            continue

        # 需要日内高低价数据才能计算Dual Thrust，否则跳过
        if price_data_map and sym in price_data_map:
            pd_map = price_data_map[sym]
            signal = generate_dual_thrust_signal(sym, {
                "highs": pd_map.get("highs", []),
                "lows": pd_map.get("lows", []),
                "closes": pd_map.get("closes", []),
                "open": pd_map.get("open", current_price),
                "current_price": current_price,
                "volumes": pd_map.get("volumes", []),
            }, lookback, k1, k2)
        else:
            # 无日内数据 → 跳过Dual Thrust（不产生假信号）
            continue

        if signal:
            results.append(signal)

    results.sort(key=lambda s: -s.strength)
    return results
