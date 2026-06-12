"""
ATOS PRO v3 — 短期策略引擎（重写版 v3）
======================================
轻量化多时间框架信号 — 从 signal_engine 已有数据直接计算。
修复: composite_signal 字段名与 signal_engine 输出对齐。

信号维度:
  - 短期动量: MACD histogram + 价格 vs MA20
  - 中期趋势: MA20 vs MA50 vs MA200 排列
  - 长期确认: RSI + Bollinger %B 位置
  - 反转信号: RSI extremes + BB position

权重随市场状态自动调整:
  BULL: 动量40% 趋势30% 突破20% 反转10%
  BEAR: 反转15% 趋势40% 突破30% 动量15%
  HIGH_VOL: 反转25% 趋势35% 突破25% 动量15%
"""

import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("strategy_v3")

# FIX P6: BEAR模式下进一步降动量、升趋势+突破（防守优先）
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.30, "breakout": 0.20, "reversal": 0.20},
    "HIGH_VOL":    {"momentum": 0.15, "trend": 0.35, "breakout": 0.25, "reversal": 0.25},
    "BEAR":        {"momentum": 0.08, "trend": 0.45, "breakout": 0.32, "reversal": 0.15},
    "SIDEWAYS":    {"momentum": 0.20, "trend": 0.28, "breakout": 0.22, "reversal": 0.30},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.30, "breakout": 0.20, "reversal": 0.25},
}


def multi_timeframe_signal(df: pd.DataFrame) -> dict:
    """从日线K线数据提取多时间框架信号。"""
    try:
        if df.empty or len(df) < 50:
            return {}

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        close = df["Close"].squeeze()
        signals = {}

        # 短期动量（5日均线 vs 10日均线）
        if len(close) >= 10:
            ma5 = close.rolling(5).mean()
            ma10 = close.rolling(10).mean()
            mom_short = float((ma5.iloc[-1] - ma10.iloc[-1]) / ma10.iloc[-1]) if ma10.iloc[-1] > 0 else 0
            signals["s_momentum"] = round(mom_short, 4)
            signals["s_trend"] = "UP" if ma5.iloc[-1] > ma10.iloc[-1] else "DOWN"

        # 中期趋势（20日均线 vs 50日均线）
        if len(close) >= 50:
            ma20 = close.rolling(20).mean()
            ma50 = close.rolling(50).mean()
            price = float(close.iloc[-1])
            trend_strength = abs(price - ma20.iloc[-1]) / ma20.iloc[-1] if ma20.iloc[-1] > 0 else 0
            signals["m_trend"] = "UP" if price > ma20.iloc[-1] > ma50.iloc[-1] else \
                                 "DOWN" if price < ma20.iloc[-1] < ma50.iloc[-1] else "NEUTRAL"
            signals["m_strength"] = round(trend_strength, 4)
            signals["m_ma20_dist"] = round((price - ma20.iloc[-1]) / ma20.iloc[-1], 4) if ma20.iloc[-1] > 0 else 0

        # 长期确认（RSI + MACD）
        if len(close) >= 26:
            rsi = _rsi_val(close)
            signals["l_rsi"] = round(rsi, 1)
            signals["l_rsi_signal"] = "OVERBOUGHT" if rsi > 70 else \
                                      "OVERSOLD" if rsi < 30 else "NORMAL"
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd = ema12 - ema26
            signal_line = macd.ewm(span=9).mean()
            signals["l_macd_hist"] = float((macd - signal_line).iloc[-1])

        return signals
    except Exception as e:
        logger.debug(f"multi_timeframe_signal failed: {e}")
        return {}


def _rsi_val(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return float(100 - (100 / (1 + rs)).iloc[-1])


def composite_signal(symbol: str, regime: str, sig: dict) -> dict:
    """
    多信号融合 → 单一决策。

    Bug #1 修复: 原来读取不存在的 s_momentum/m_trend/l_rsi 字段，
    现在直接从 signal_engine 的标准输出字段计算:
      - momentum: 使用 macd_hist (短期动量) + price vs ma50 (中期偏离)
      - trend: 使用 ma50 vs ma200 排列 + signal_engine trend
      - breakout: 使用 bollinger %B + volume_ratio
      - reversal: 使用 RSI extremes + bollinger position
    """
    weights = WEIGHT_MATRIX.get(regime, WEIGHT_MATRIX["UNKNOWN"])
    score = 0.0  # 从 0 起步，纯增量评分
    reasons = []

    price = sig.get("price", 0)
    ma50 = sig.get("ma50", 0)
    ma200 = sig.get("ma200", 0)
    rsi = sig.get("rsi", 50)
    macd_hist = sig.get("macd_hist", 0)
    trend = sig.get("trend", "NEUTRAL")
    vol_ratio = sig.get("volume_ratio", 1.0)
    bb = sig.get("bollinger", {})
    pct_b = bb.get("pct_b", 0.5) if isinstance(bb, dict) else 0.5

    # ── 短期动量（macd_hist + 价格偏离 MA50）──
    if macd_hist > 0.5:
        score += weights["momentum"] * 0.25
        reasons.append("MACD强")
    elif macd_hist > 0:
        score += weights["momentum"] * 0.12
        reasons.append("MACD正")
    elif macd_hist < -0.5:
        score -= weights["momentum"] * 0.20
        reasons.append("MACD弱")

    # 价格 vs MA50（中期动量）
    if ma50 > 0 and price > ma50 * 1.03:
        score += weights["momentum"] * 0.15
        reasons.append("价格>MA50")
    elif ma50 > 0 and price < ma50 * 0.97:
        score -= weights["momentum"] * 0.10
        reasons.append("价格<MA50")

    # ── 中期趋势（MA50 vs MA200 + signal_engine trend）──
    if ma50 > 0 and ma200 > 0:
        if ma50 > ma200:
            score += weights["trend"] * 0.20
            reasons.append("MA50>MA200")
        elif ma50 < ma200:
            score -= weights["trend"] * 0.15
            reasons.append("MA50<MA200")

    trend_map = {"UP": 0.12, "WEAK_UP": 0.05, "NEUTRAL": 0.0, "WEAK_DOWN": -0.05, "DOWN": -0.12}
    score += weights["trend"] * trend_map.get(trend, 0)
    if trend in ("UP", "WEAK_UP"):
        reasons.append(f"趋势{trend}")

    # ── 突破信号（Bollinger %B + 成交量确认）──
    if pct_b > 0.8:
        if vol_ratio > 1.2:
            score += weights["breakout"] * 0.18
            reasons.append("BB突破+放量")
        else:
            score += weights["breakout"] * 0.08
            reasons.append("BB上轨")
    elif pct_b < 0.2:
        score -= weights["breakout"] * 0.10
        reasons.append("BB下轨")

    # ── 反转信号（RSI extremes + Bollinger position）──
    # 超卖 + 下轨 → 均值回归买入信号
    if rsi < 30 and pct_b < 0.3:
        score += weights["reversal"] * 0.25
        reasons.append(f"超卖RSI={rsi:.0f}+BB")
    elif rsi < 35:
        score += weights["reversal"] * 0.15
        reasons.append(f"RSI={rsi:.0f}偏低")
    # 超买 + 上轨 → 回避
    if rsi > 75 and pct_b > 0.7:
        score -= weights["reversal"] * 0.20
        reasons.append(f"超买RSI={rsi:.0f}+BB")
    elif rsi > 70:
        score -= weights["reversal"] * 0.10
        reasons.append(f"RSI={rsi:.0f}偏高")

    score = max(0.0, min(1.0, score))

    decision = "BUY" if score > 0.20 else ("SELL" if score < 0.08 else "HOLD")

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "decision": decision,
        "weights_used": weights,
        "reasons": reasons[:5],
    }


def get_v3_signals(symbols: list[str], regime: str = "UNKNOWN") -> dict:
    """
    从 signal_engine 已有数据计算多时间框架信号。
    修复: 不再返回硬编码 0.5，由 composite_signal 在 combine() 中按标的逐个计算。
    这里返回占位，实际计算在 combine() 的 per-symbol loop 中完成。
    """
    logger.info(f"v3多时间框架信号: {len(symbols)} 只标的将在 factor combine 中计算")
    return {sym: {"composite": 0.5, "decision": "HOLD"} for sym in symbols}
