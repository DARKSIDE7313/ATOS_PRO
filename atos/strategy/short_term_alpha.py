"""
ATOS PRO — 短线Alpha引擎
========================
专门为短线交易设计的信号系统（持有1小时-3天）

与因子引擎的区别:
  因子引擎: 中周期 (周-月), 价值/质量/动量等基本面因子
  短线Alpha: 短周期 (小时-天), 突破/回调/量价/盘口

4个短线信号源:
  1. 开盘突破 (Opening Range Breakout) — 经典短线策略
  2. 日内动量 (Intraday Momentum) — 价格相对VWAP
  3. 回调买入 (Pullback to MA) — 趋势中回调时入场
  4. 放量突破 (Volume Breakout) — 量价配合

止损: -2% 硬止损 (短线必须严格)
止盈: +3% 卖一半, +5% 全卖
持有期: 1小时-3天
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class ShortTermSignal:
    symbol: str
    price: float
    signal_type: str  # "breakout" | "momentum" | "pullback" | "volume"
    strength: float   # 0-1
    stop_loss: float
    take_profit_1: float  # 卖一半
    take_profit_2: float  # 全卖
    max_hold_minutes: int
    reasons: List[str]


def opening_range_breakout(prices: np.ndarray, highs: np.ndarray,
                           lows: np.ndarray, current_price: float,
                           or_high: float, or_low: float) -> Optional[ShortTermSignal]:
    """开盘突破信号 — 经典短线策略

    逻辑: 价格突破开盘区间上沿 → 做多
    开盘区间 = 前30分钟最高/最低价
    """
    if current_price <= 0 or or_high <= 0:
        return None

    # 突破OR上沿
    breakout = current_price > or_high * 1.002  # 0.2% buffer
    if not breakout:
        return None

    # 确认: 放量 (当前成交量 > 前5根平均)
    if len(prices) >= 5:
        recent_range = highs[-5:] - lows[-5:]
        current_range = highs[-1] - lows[-1] if len(highs) > 0 else 0
        avg_range = np.mean(recent_range)
        vol_ok = current_range > avg_range * 1.2 if avg_range > 0 else True
    else:
        vol_ok = True

    if not vol_ok:
        return None

    strength = min(1.0, (current_price - or_high) / or_high * 100 + 0.3)
    return ShortTermSignal(
        symbol="", price=current_price,
        signal_type="breakout",
        strength=round(strength, 3),
        stop_loss=round(or_high * 0.98, 2),  # 跌破OR上沿2%止损
        take_profit_1=round(current_price * 1.03, 2),
        take_profit_2=round(current_price * 1.05, 2),
        max_hold_minutes=180,  # 3小时
        reasons=["开盘突破", f"OR高={or_high:.2f}", "放量确认" if vol_ok else ""],
    )


def intraday_momentum(prices: np.ndarray, vwap: float,
                      current_price: float) -> Optional[ShortTermSignal]:
    """日内动量信号

    条件:
      1. 价格在VWAP上方 (买方主导)
      2. 过去5分钟涨幅 > 0.3% (动量确认)
      3. RSI 14 在 50-75 (够强但不过热)
    """
    if len(prices) < 20 or current_price <= 0 or vwap <= 0:
        return None

    # 价格在VWAP上方
    above_vwap = current_price > vwap * 1.002
    if not above_vwap:
        return None

    # 5分钟动量
    if len(prices) >= 5:
        momentum_5m = (prices[-1] - prices[-5]) / prices[-5]
        if momentum_5m < 0.003:
            return None
    else:
        momentum_5m = 0

    # RSI
    rsi = _calc_rsi(prices, 14)
    if rsi < 50 or rsi > 75:
        return None

    strength = min(1.0, (rsi - 50) / 25 * 0.5 + momentum_5m * 50)
    return ShortTermSignal(
        symbol="", price=current_price,
        signal_type="momentum",
        strength=round(strength, 3),
        stop_loss=round(current_price * 0.98, 2),  # -2%
        take_profit_1=round(current_price * 1.03, 2),
        take_profit_2=round(current_price * 1.05, 2),
        max_hold_minutes=120,
        reasons=[f"VWAP上方", f"5m动量={momentum_5m:.1%}", f"RSI={rsi:.0f}"],
    )


def pullback_to_ma(prices: np.ndarray, current_price: float) -> Optional[ShortTermSignal]:
    """回调买入信号 — 趋势中的回调

    条件:
      1. 价格在MA20上方 (上升趋势)
      2. 价格从高点回落 > 2% (回调幅度)
      3. 价格在MA10附近 (支撑位)
      4. RSI 30-50 (超卖反弹区域)
    """
    if len(prices) < 30:
        return None

    ma10 = float(np.mean(prices[-10:]))
    ma20 = float(np.mean(prices[-20:]))
    high_5d = float(np.max(prices[-5:]))

    # 上升趋势: MA10 > MA20
    uptrend = ma10 > ma20
    if not uptrend:
        return None

    # 回调: 从5日高回落 > 2%
    pullback_pct = (high_5d - current_price) / high_5d
    if pullback_pct < 0.02:
        return None

    # 接近MA10支撑
    near_ma10 = abs(current_price - ma10) / ma10 < 0.015
    if not near_ma10:
        return None

    # RSI 超卖区域
    rsi = _calc_rsi(prices, 14)
    if rsi < 30 or rsi > 50:
        return None

    strength = min(1.0, pullback_pct * 15 + (50 - rsi) / 50)
    return ShortTermSignal(
        symbol="", price=current_price,
        signal_type="pullback",
        strength=round(strength, 3),
        stop_loss=round(current_price * 0.98, 2),
        take_profit_1=round(current_price * 1.03, 2),
        take_profit_2=round(high_5d, 2),  # 目标回到前高
        max_hold_minutes=240,
        reasons=[f"回调{pullback_pct:.1%}", f"MA10支撑", f"RSI={rsi:.0f}"],
    )


def volume_breakout(prices: np.ndarray, volumes: np.ndarray,
                    current_price: float) -> Optional[ShortTermSignal]:
    """放量突破信号

    条件:
      1. 日涨幅 > 1.5%
      2. 成交量 > 20日均量的 1.5倍
      3. 价格创5日新高
    """
    if len(prices) < 25 or len(volumes) < 25:
        return None

    # 日涨幅
    daily_change = (prices[-1] - prices[-2]) / prices[-2] if len(prices) >= 2 else 0
    if daily_change < 0.015:
        return None

    # 放量
    avg_vol_20 = np.mean(volumes[-21:-1])
    current_vol = volumes[-1]
    if avg_vol_20 <= 0 or current_vol / avg_vol_20 < 1.5:
        return None

    # 创5日新高
    high_5d = np.max(prices[-6:-1])  # 不包含今天
    new_high = current_price > high_5d
    if not new_high:
        return None

    strength = min(1.0, daily_change * 30 + (current_vol / avg_vol_20 - 1) * 0.3)
    return ShortTermSignal(
        symbol="", price=current_price,
        signal_type="volume",
        strength=round(strength, 3),
        stop_loss=round(current_price * 0.98, 2),
        take_profit_1=round(current_price * 1.03, 2),
        take_profit_2=round(current_price * 1.06, 2),
        max_hold_minutes=300,
        reasons=[f"涨幅{daily_change:.1%}", f"放量{current_vol/avg_vol_20:.1f}x", "5日新高"],
    )


# ═══════════════════════════════════════════════════
# 综合短线信号生成
# ═══════════════════════════════════════════════════

def generate_short_term_signals(sym: str, prices: np.ndarray,
                                highs: np.ndarray = None,
                                lows: np.ndarray = None,
                                volumes: np.ndarray = None,
                                vwap: float = None,
                                or_high: float = None,
                                or_low: float = None) -> List[ShortTermSignal]:
    """为单只股票生成所有短线信号"""
    current_price = float(prices[-1]) if len(prices) > 0 else 0
    if current_price <= 0:
        return []

    signals = []

    # 放量突破
    if volumes is not None and len(volumes) >= 25:
        sig = volume_breakout(prices, volumes, current_price)
        if sig: signals.append(sig)

    # 开盘突破
    if or_high is not None and or_high > 0:
        sig = opening_range_breakout(prices, highs or prices, lows or prices,
                                     current_price, or_high, or_low or or_high)
        if sig: signals.append(sig)

    # 日内动量
    if vwap is not None and vwap > 0:
        sig = intraday_momentum(prices, vwap, current_price)
        if sig: signals.append(sig)

    # 回调买入
    sig = pullback_to_ma(prices, current_price)
    if sig: signals.append(sig)

    # 设置symbol
    for sig in signals:
        sig.symbol = sym

    signals.sort(key=lambda s: -s.strength)
    return signals


def _calc_rsi(prices: np.ndarray, period: int = 14) -> float:
    """快速RSI计算"""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-period-1:])
    gains = deltas.clip(min=0).mean()
    losses = (-deltas.clip(max=0)).mean()
    rs = gains / max(losses, 1e-9)
    return float(100 - 100 / (1 + rs))


# ═══════════════════════════════════════════════════
# 短线持仓管理
# ═══════════════════════════════════════════════════

@dataclass
class STPosition:
    symbol: str
    shares: int
    entry_price: float
    entry_time: float  # timestamp
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    max_hold_minutes: int
    max_price: float
    half_sold: bool = False


class ShortTermManager:
    """短线持仓管理器 — 快速进出"""

    def __init__(self, capital: float):
        self.cash = capital
        self.initial = capital
        self.positions: Dict[str, STPosition] = {}
        self.max_positions = 5
        self.max_single_pct = 0.15
        self.trades: List[dict] = []

    def can_buy(self) -> bool:
        return len(self.positions) < self.max_positions and self.cash > 1000

    def buy(self, signal: ShortTermSignal, shares: int, commission: float = 0.001):
        cost = shares * signal.price * (1 + commission)
        if cost > self.cash:
            shares = int(self.cash / (signal.price * (1 + commission)))
            cost = shares * signal.price * (1 + commission)
        if shares <= 0 or cost > self.cash:
            return False

        self.cash -= cost
        import time
        self.positions[signal.symbol] = STPosition(
            symbol=signal.symbol, shares=shares,
            entry_price=signal.price, entry_time=time.time(),
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            max_hold_minutes=signal.max_hold_minutes,
            max_price=signal.price,
        )
        self.trades.append({"action": "BUY", "symbol": signal.symbol,
                           "shares": shares, "price": signal.price,
                           "type": signal.signal_type})
        return True

    def manage(self, prices: Dict[str, float], now: float):
        """管理所有持仓 — 止盈/止损/时间止损"""
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym not in prices:
                continue

            px = prices[sym]
            pos.max_price = max(pos.max_price, px)
            elapsed = (now - pos.entry_time) / 60  # 分钟
            pnl = (px - pos.entry_price) / pos.entry_price

            sell_shares = 0
            reason = ""

            # 止损 -2%
            if pnl <= -0.02:
                sell_shares = pos.shares
                reason = f"短线止损 {pnl:.1%}"
            # 第一止盈 +3% — 卖一半
            elif pnl >= 0.03 and not pos.half_sold and pos.shares >= 2:
                sell_shares = pos.shares // 2
                pos.half_sold = True
                reason = f"短线止盈1 +{pnl:.1%}"
            # 第二止盈 +5% — 全卖
            elif pnl >= 0.05:
                sell_shares = pos.shares
                reason = f"短线止盈2 +{pnl:.1%}"
            # 时间止损 — 超时未盈利
            elif elapsed > pos.max_hold_minutes and pnl < 0.01:
                sell_shares = pos.shares
                reason = f"时间止损 ({elapsed:.0f}分, {pnl:.1%})"

            if sell_shares > 0:
                self.cash += sell_shares * px * 0.999
                pos.shares -= sell_shares
                self.trades.append({"action": "SELL", "symbol": sym,
                                   "shares": sell_shares, "price": px,
                                   "pnl": round(pnl*100, 2), "reason": reason})
                if pos.shares <= 0:
                    del self.positions[sym]
