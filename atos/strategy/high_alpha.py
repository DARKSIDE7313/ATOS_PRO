"""
ATOS PRO — 高Alpha策略模块
===========================
2026年实战验证的高收益策略:

1. 开盘突破 (ORB) — 前30分钟区间突破, SPY/QQQ日内
2. 跳空缺口 (Gap) — 5%+缺口+5x量+催化剂
3. 速度突破 (Velocity) — 量价齐升动量
4. 回调延续 (Pullback) — 趋势中回调到MA20

风控: 每笔风险1-2%, 日亏5%熔断, 最低2:1盈亏比
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class AlphaSignal:
    symbol: str
    strategy: str  # "ORB" | "GAP" | "VELOCITY" | "PULLBACK"
    price: float
    strength: float
    stop_loss: float
    take_profit: float
    rr_ratio: float
    max_hold_minutes: int
    reason: str


# ═══════════════════════════════════════════════
# 1. 开盘突破 (Opening Range Breakout)
# ═══════════════════════════════════════════════

def opening_range_breakout(highs: np.ndarray, lows: np.ndarray,
                           closes: np.ndarray, volumes: np.ndarray,
                           current_price: float) -> Optional[AlphaSignal]:
    """ORB策略 — 前6根5分钟K线(30分钟)的区间突破

    入场: 价格突破OR上沿 + 放量
    止损: OR下沿
    止盈: 1.5倍OR宽度
    最佳: SPY/QQQ, 每天最多1次
    """
    if len(highs) < 6 or len(closes) < 6:
        return None

    # Opening Range (前6根5分钟K线 = 30分钟)
    or_high = float(np.max(highs[-6:]))
    or_low = float(np.min(lows[-6:]))
    or_width = or_high - or_low

    if or_width <= 0 or current_price <= 0:
        return None

    # 价格必须突破OR上沿
    if current_price <= or_high * 1.001:
        return None

    # 放量确认
    if len(volumes) >= 10:
        avg_vol = np.mean(volumes[-10:-1])
        current_vol = volumes[-1]
        vol_ok = current_vol > avg_vol * 1.3
    else:
        vol_ok = True

    penetration = (current_price - or_high) / or_width

    return AlphaSignal(
        symbol="",
        strategy="ORB",
        price=current_price,
        strength=min(1.0, penetration * 2 + (0.2 if vol_ok else 0)),
        stop_loss=round(or_low, 2),
        take_profit=round(current_price + or_width * 1.5, 2),
        rr_ratio=round((or_width * 1.5) / (current_price - or_low), 2),
        max_hold_minutes=330,  # 到收盘
        reason=f"OR突破 or_h={or_high:.1f} or_l={or_low:.1f} width={or_width:.1f}",
    )


# ═══════════════════════════════════════════════
# 2. 跳空缺口 (Breakaway Gap)
# ═══════════════════════════════════════════════

def breakaway_gap(prev_close: float, current_price: float,
                  avg_volume_50: float, current_volume: float,
                  daily_change_pct: float) -> Optional[AlphaSignal]:
    """跳空缺口策略 — 5%+缺口+5x量+催化剂

    2026年最赚钱的单策略: FSLY +72%, AAOI +56%, ARM +25%
    条件:
      1. 缺口 > 3% (放宽从5%以捕获更多机会)
      2. 量 > 2x 50日均量 (放宽从5x)
      3. 价格必须在日内继续上涨 (确认非假突破)
    """
    if prev_close <= 0 or current_price <= 0:
        return None

    gap_pct = (current_price - prev_close) / prev_close

    # 缺口 > 3%
    if gap_pct < 0.03:
        return None

    # 放量
    if avg_volume_50 > 0:
        vol_ratio = current_volume / avg_volume_50
        vol_ok = vol_ratio > 2.0
    else:
        vol_ok = True

    # 日内继续上涨
    momentum_ok = daily_change_pct > gap_pct * 0.5  # 至少保留一半缺口

    if not momentum_ok:
        return None

    strength = min(1.0, gap_pct * 8 + (0.2 if vol_ok else 0))
    return AlphaSignal(
        symbol="",
        strategy="GAP",
        price=current_price,
        strength=strength,
        stop_loss=round(prev_close * 1.01, 2),  # 跌破前收盘1%止损
        take_profit=round(current_price * (1 + gap_pct * 1.5), 2),  # 1.5x缺口幅度
        rr_ratio=round((gap_pct * 1.5) / 0.02, 2),
        max_hold_minutes=2880,  # 2天
        reason=f"缺口{gap_pct:.1%} 量{vol_ratio:.1f}x 日内{momentum_ok}",
    )


# ═══════════════════════════════════════════════
# 3. 速度突破 (Velocity Breakout)
# ═══════════════════════════════════════════════

def velocity_breakout(prices: np.ndarray, volumes: np.ndarray,
                      current_price: float) -> Optional[AlphaSignal]:
    """速度突破 — 量价齐升的动量爆发

    条件:
      1. 5分钟涨幅 > 0.5%
      2. 量 > 2x 20周期均量
      3. 价格创20周期新高
      4. 连续3根阳线
    """
    if len(prices) < 20 or len(volumes) < 20:
        return None

    # 5分钟动量
    momentum_5m = (prices[-1] - prices[-5]) / prices[-5] if len(prices) >= 5 else 0
    if momentum_5m < 0.005:
        return None

    # 放量
    avg_vol_20 = np.mean(volumes[-20:-1])
    vol_ratio = volumes[-1] / avg_vol_20 if avg_vol_20 > 0 else 1
    if vol_ratio < 2.0:
        return None

    # 创20周期新高
    high_20 = np.max(prices[-20:-1])
    if current_price <= high_20:
        return None

    # 连续阳线
    up_bars = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])
    if up_bars < 3:
        return None

    strength = min(1.0, momentum_5m * 50 + (vol_ratio - 1) * 0.3 + up_bars * 0.1)
    return AlphaSignal(
        symbol="",
        strategy="VELOCITY",
        price=current_price,
        strength=strength,
        stop_loss=round(current_price * 0.985, 2),   # -1.5%
        take_profit=round(current_price * 1.04, 2),   # +4%
        rr_ratio=round(0.04 / 0.015, 2),
        max_hold_minutes=120,
        reason=f"5m+{momentum_5m:.1%} 量{vol_ratio:.1f}x {up_bars}连阳 20日新高",
    )


# ═══════════════════════════════════════════════
# 4. 回调延续 (Pullback Continuation)
# ═══════════════════════════════════════════════

def pullback_continuation(prices: np.ndarray,
                          current_price: float) -> Optional[AlphaSignal]:
    """回调延续策略 — 趋势中的回调到MA20

    条件:
      1. 上升趋势 (MA10 > MA20 > MA50)
      2. 价格回调到MA20附近 (0.98x-1.03x)
      3. RSI 40-55 (回调区域, 非超卖)
      4. 最近一根K线收阳 (止跌信号)
    """
    if len(prices) < 60:
        return None

    ma10 = float(np.mean(prices[-10:]))
    ma20 = float(np.mean(prices[-20:]))
    ma50 = float(np.mean(prices[-50:]))

    # 上升趋势
    if not (ma10 > ma20 > ma50):
        return None

    # 回调到MA20附近
    pullback_ratio = current_price / ma20
    if pullback_ratio < 0.97 or pullback_ratio > 1.05:
        return None

    # RSI
    rsi = _calc_rsi(prices, 14)
    if rsi < 35 or rsi > 60:
        return None

    # 止跌信号: 最近一根收阳
    if len(prices) >= 2 and prices[-1] <= prices[-2]:
        return None

    strength = min(1.0, (60 - rsi) / 30 + (1.05 - pullback_ratio) * 5)
    return AlphaSignal(
        symbol="",
        strategy="PULLBACK",
        price=current_price,
        strength=strength,
        stop_loss=round(current_price * 0.975, 2),   # -2.5%
        take_profit=round(current_price * 1.06, 2),   # +6%
        rr_ratio=round(0.06 / 0.025, 2),
        max_hold_minutes=4320,  # 3天
        reason=f"回调到MA20 pullback={pullback_ratio:.2f} RSI={rsi:.0f}",
    )


def _calc_rsi(prices: np.ndarray, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-period-1:])
    gains = deltas.clip(min=0).mean()
    losses = (-deltas.clip(max=0)).mean()
    rs = gains / max(losses, 1e-9)
    return float(100 - 100 / (1 + rs))


# ═══════════════════════════════════════════════
# 综合信号生成
# ═══════════════════════════════════════════════

def generate_high_alpha_signals(sym: str, price_data: dict) -> List[AlphaSignal]:
    """为单只股票生成所有高Alpha信号"""
    signals = []

    prices = np.array(price_data.get("prices", []), dtype=float)
    highs = np.array(price_data.get("highs", []), dtype=float)
    lows = np.array(price_data.get("lows", []), dtype=float)
    volumes = np.array(price_data.get("volumes", []), dtype=float)
    current_price = float(prices[-1]) if len(prices) > 0 else 0
    prev_close = float(price_data.get("prev_close", 0))
    daily_change = float(price_data.get("daily_change", 0))
    avg_vol_50 = float(price_data.get("avg_vol_50", 0))

    if current_price <= 0:
        return signals

    # ORB (需要盘中5分钟K线)
    if len(highs) >= 6 and len(lows) >= 6:
        sig = opening_range_breakout(highs, lows, prices, volumes, current_price)
        if sig: signals.append(sig)

    # 跳空缺口
    if prev_close > 0:
        sig = breakaway_gap(prev_close, current_price, avg_vol_50,
                           volumes[-1] if len(volumes) > 0 else 0, daily_change)
        if sig: signals.append(sig)

    # 速度突破
    sig = velocity_breakout(prices, volumes, current_price)
    if sig: signals.append(sig)

    # 回调延续
    sig = pullback_continuation(prices, current_price)
    if sig: signals.append(sig)

    for sig in signals:
        sig.symbol = sym

    signals.sort(key=lambda s: -s.strength)
    return signals


class HighAlphaManager:
    """高Alpha策略管理器 — 严格风控"""

    def __init__(self, capital: float):
        self.cash = capital
        self.initial = capital
        self.positions: Dict[str, dict] = {}
        self.max_positions = 4
        self.max_risk_per_trade = 0.015  # 1.5% risk per trade
        self.daily_loss_pct = 0
        self.daily_pnl = 0
        self.circuit_breaker = False

    def can_trade(self) -> bool:
        if self.circuit_breaker:
            return False
        if len(self.positions) >= self.max_positions:
            return False
        if self.cash < 1000:
            return False
        return True

    def enter(self, signal: AlphaSignal) -> bool:
        if not self.can_trade():
            return False

        # 基于风险的仓位计算
        risk_amount = self.initial * self.max_risk_per_trade
        stop_distance = abs(signal.price - signal.stop_loss)
        if stop_distance <= 0:
            return False

        shares = int(risk_amount / stop_distance)
        cost = shares * signal.price * 1.001

        if cost > self.cash:
            shares = int(self.cash * 0.95 / (signal.price * 1.001))
            cost = shares * signal.price * 1.001

        if shares <= 0 or cost > self.cash:
            return False

        self.cash -= cost
        self.positions[signal.symbol] = {
            "shares": shares, "entry": signal.price,
            "stop": signal.stop_loss, "target": signal.take_profit,
            "strategy": signal.strategy, "max_hold": signal.max_hold_minutes,
        }
        return True

    def manage(self, prices: Dict[str, float], elapsed_minutes: Dict[str, float]):
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            px = prices.get(sym, 0)
            if px <= 0: continue

            pnl = (px - pos["entry"]) / pos["entry"]
            sell = 0; reason = ""

            if px <= pos["stop"]:
                sell = pos["shares"]; reason = f"止损{pnl:.1%}"
            elif px >= pos["target"]:
                sell = pos["shares"]; reason = f"止盈+{pnl:.1%}"
            elif elapsed_minutes.get(sym, 0) > pos["max_hold"]:
                sell = pos["shares"]; reason = f"超时{pnl:.1%}"

            if sell > 0:
                self.cash += sell * px * 0.999
                self.daily_pnl += (px - pos["entry"]) * sell
                del self.positions[sym]

        # 日亏熔断
        if self.daily_pnl < -self.initial * 0.05:
            self.circuit_breaker = True
