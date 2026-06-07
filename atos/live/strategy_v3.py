# ENHANCED — multi-timeframe signal source for ATOS factor engine.

"""
ATOS PRO v3 — 短期策略引擎（重写）
==================================
多时间框架 + 自适应权重 + Kelly仓位 + 波动率目标

信号权重随市场状态自动调整：
  BULL: 动量40% 趋势30% 突破20% 反转10%
  BEAR: 反转30% 价值30% 趋势20% 动量20%
  HIGH_VOL: 反转40% 价值30% 趋势20% 动量10%
"""

import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("strategy_v3")

# 信号权重矩阵
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum":0.40,"trend":0.30,"breakout":0.20,"reversal":0.10},
    "BULL_WEAK":   {"momentum":0.30,"trend":0.25,"breakout":0.15,"reversal":0.30},
    "HIGH_VOL":    {"momentum":0.10,"trend":0.20,"breakout":0.10,"reversal":0.60},
    "BEAR":        {"momentum":0.20,"trend":0.20,"breakout":0.10,"reversal":0.50},
    "UNKNOWN":     {"momentum":0.25,"trend":0.25,"breakout":0.25,"reversal":0.25},
}


def multi_timeframe_signal(symbol: str) -> dict:
    """5分钟/1小时/日线 三时间框架信号融合"""
    try:
        d5m = yf.download(symbol, period="5d", interval="5m", progress=False, auto_adjust=True)
        d1h = yf.download(symbol, period="1mo", interval="1h", progress=False, auto_adjust=True)
        d1d = yf.download(symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)

        signals = {}

        # 5分钟框架：短期动量和成交量
        if not d5m.empty and len(d5m) > 50:
            close5 = d5m["Close"].squeeze()
            vol5 = d5m["Volume"].squeeze()
            # 短期动量（过去20根5分钟K线）
            mom5 = float(close5.iloc[-1] / close5.iloc[-20] - 1) if len(close5) > 20 else 0
            # 量比
            vol_ratio5 = float(vol5.iloc[-5:].mean() / vol5.iloc[-50:].mean()) if len(vol5) > 50 else 1
            signals["m5_momentum"] = round(mom5, 4)
            signals["m5_vol_ratio"] = round(vol_ratio5, 2)

        # 1小时框架：趋势强度
        if not d1h.empty and len(d1h) > 20:
            close1h = d1h["Close"].squeeze()
            ma20h = float(close1h.rolling(20).mean().iloc[-1])
            ma50h = float(close1h.rolling(50).mean().iloc[-1]) if len(close1h) > 50 else ma20h
            price1h = float(close1h.iloc[-1])
            signals["h1_trend"] = "UP" if price1h > ma20h > ma50h else ("DOWN" if price1h < ma20h < ma50h else "NEUTRAL")
            signals["h1_strength"] = round(abs(price1h - ma20h) / ma20h, 4) if ma20h > 0 else 0

        # 日线框架：中长期确认
        if not d1d.empty and len(d1d) > 50:
            close1d = d1d["Close"].squeeze()
            rsi = _rsi_val(close1d)
            signals["d1_rsi"] = round(rsi, 1)
            signals["d1_trend"] = "UP" if float(close1d.iloc[-1]) > float(close1d.rolling(50).mean().iloc[-1]) else "DOWN"

        return signals
    except Exception as e:
        return {"error": str(e)}


def _rsi_val(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return float(100 - (100 / (1 + rs)).iloc[-1])


def composite_signal(symbol: str, regime: str, signals: dict) -> dict:
    """多信号融合 → 单一决策"""
    weights = WEIGHT_MATRIX.get(regime, WEIGHT_MATRIX["UNKNOWN"])
    mtf = multi_timeframe_signal(symbol)

    score = 0.5
    reasons = []

    # 动量信号
    m5_mom = mtf.get("m5_momentum", 0)
    if m5_mom > 0.005:
        score += weights["momentum"] * 0.3
        reasons.append(f"5m动量+{m5_mom:.1%}")
    elif m5_mom < -0.005:
        score -= weights["momentum"] * 0.3
        reasons.append(f"5m动量{m5_mom:.1%}")

    # 趋势信号
    h1_trend = mtf.get("h1_trend", "NEUTRAL")
    if h1_trend == "UP":
        score += weights["trend"] * 0.25
    elif h1_trend == "DOWN":
        score -= weights["trend"] * 0.25

    # 日线确认
    d1_rsi = mtf.get("d1_rsi", 50)
    d1_trend = mtf.get("d1_trend", "NEUTRAL")
    if d1_rsi < 30:
        score += weights["reversal"] * 0.3
    elif d1_rsi > 70:
        score -= weights["breakout"] * 0.2

    if d1_trend == "UP":
        score += weights["trend"] * 0.1

    # 技术信号融合
    rsi = signals.get("rsi", 50)
    trend = signals.get("trend", "NEUTRAL")
    bb = signals.get("bollinger", {}).get("pct_b", 0.5)

    if trend == "UP": score += 0.05
    elif trend == "DOWN": score -= 0.05
    if 30 <= rsi <= 70: score += 0.03
    if bb < 0.2: score += weights["reversal"] * 0.2  # 超卖反弹
    if bb > 0.8: score -= weights["breakout"] * 0.15

    score = max(0.05, min(0.95, score))

    decision = "BUY" if score > 0.60 else ("SELL" if score < 0.35 else "HOLD")

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "decision": decision,
        "weights_used": weights,
        "reasons": reasons[:5],
        "multi_timeframe": mtf,
    }


def get_v3_signals(symbols: list[str], regime: str = "UNKNOWN") -> dict:
    """
    批量获取多时间框架（5m/1h/1d）信号。

    为 ATOS 因子引擎提供 ENHANCED 信号源，
    返回 {symbol: {composite, decision, multi_timeframe, reasons}} 格式
    供 combine() 作为第5因子 "multiframe" 使用。
    """
    results = {}
    for sym in symbols:
        try:
            result = composite_signal(sym, regime, {})
            results[sym] = {
                "composite": result["score"],
                "decision": result["decision"],
                "multi_timeframe": result.get("multi_timeframe", {}),
                "reasons": result.get("reasons", []),
            }
        except Exception as e:
            logger.warning(f"v3信号失败 {sym}: {e}")
            results[sym] = {"composite": 0.5, "decision": "HOLD", "multi_timeframe": {}}
    logger.info(f"v3多时间框架信号: {len(results)} 只")
    return results
