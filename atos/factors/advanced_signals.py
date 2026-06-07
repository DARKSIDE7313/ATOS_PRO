"""
ATOS PRO v2 — 高级交易信号（专业基金级别）
=============================================
在基础 RSI/MACD/MA 之上，加入机构常用的信号：

  1. 均值回归 — RSI 极端值 + 确认信号
  2. 量价背离 — 价涨量缩 = 衰竭信号
  3. 波动率突破 — ATR 突破 + 方向确认
  4. 缺口回补 — Gap fill 概率交易
  5. 支撑阻力 — 52周高低点附近的反应
  6. 跨资产信号 — 债券/黄金/原油 vs 股票
  7. VIX 期限结构 — contango/backwardation 信号
  8. 资金流 — OBV + MFI 确认
"""

import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("factors.advanced")


def mean_reversion_signal(df: pd.DataFrame) -> dict:
    """
    均值回归信号。
    RSI 极端 + 反转K线确认 = 高概率回归。
    """
    close = df["Close"].squeeze()
    high = df["High"].squeeze()
    low = df["Low"].squeeze()

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = float(100 - (100 / (1 + rs)).iloc[-1])

    # 锤子线检测（下影线 > 实体 2 倍，收在最高点附近）
    last_body = abs(float(close.iloc[-1] - close.iloc[-2]))
    last_lower_wick = float(min(close.iloc[-1], close.iloc[-2]) - low.iloc[-1])
    last_upper_wick = float(high.iloc[-1] - max(close.iloc[-1], close.iloc[-2]))

    hammer = last_lower_wick > last_body * 2 and last_upper_wick < last_body * 0.5
    shooting_star = last_upper_wick > last_body * 2 and last_lower_wick < last_body * 0.5

    signal = "NONE"
    strength = 0

    if rsi < 30 and hammer:
        signal = "BULLISH_REVERSAL"
        strength = min(1.0, (30 - rsi) / 20 + 0.5)
    elif rsi > 70 and shooting_star:
        signal = "BEARISH_REVERSAL"
        strength = min(1.0, (rsi - 70) / 20 + 0.5)

    return {
        "rsi": round(rsi, 1),
        "hammer": hammer,
        "shooting_star": shooting_star,
        "signal": signal,
        "strength": round(strength, 2),
    }


def volume_divergence(df: pd.DataFrame) -> dict:
    """
    量价背离检测。
    价创新高但量萎缩 → 上涨乏力。
    价创新低但量萎缩 → 下跌衰竭。
    """
    close = df["Close"].squeeze()
    vol = df["Volume"].squeeze()

    # 20 日窗口
    price_20d_high = float(close.iloc[-1]) >= float(close.iloc[-20:].max())
    vol_20d_high = float(vol.iloc[-1]) >= float(vol.iloc[-20:].max())
    price_20d_low = float(close.iloc[-1]) <= float(close.iloc[-20:].min())
    vol_20d_low = float(vol.iloc[-1]) <= float(vol.iloc[-20:].min())

    # 价格趋势 vs 成交量趋势
    price_trend = float(close.iloc[-5:].mean() - close.iloc[-20:-5].mean())
    vol_trend = float(vol.iloc[-5:].mean() - vol.iloc[-20:-5].mean())

    signal = "NONE"
    if price_20d_high and not vol_20d_high:
        signal = "BEARISH_DIVERGENCE"  # 价高量不跟
    elif price_20d_low and not vol_20d_low:
        signal = "BULLISH_DIVERGENCE"  # 价低量不缩
    elif price_trend > 0 and vol_trend < 0:
        signal = "WEAK_RALLY"
    elif price_trend < 0 and vol_trend > 0:
        signal = "ACCUMULATION"

    return {
        "signal": signal,
        "price_20d_high": price_20d_high,
        "vol_20d_high": vol_20d_high,
    }


def atr_breakout(df: pd.DataFrame, atr_multiple: float = 2.0) -> dict:
    """ATR 突破信号"""
    high = df["High"].squeeze()
    low = df["Low"].squeeze()
    close = df["Close"].squeeze()

    # ATR(14)
    tr = pd.concat([
        high - low,
        abs(high - close.shift()),
        abs(low - close.shift())
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])

    # 20 日均价
    ma20 = float(close.rolling(20).mean().iloc[-1])
    price = float(close.iloc[-1])

    upper = ma20 + atr * atr_multiple
    lower = ma20 - atr * atr_multiple

    signal = "NONE"
    if price > upper:
        signal = "BULLISH_BREAKOUT"
    elif price < lower:
        signal = "BEARISH_BREAKDOWN"

    return {
        "atr": round(atr, 2),
        "ma20": round(ma20, 2),
        "breakout_upper": round(upper, 2),
        "breakout_lower": round(lower, 2),
        "signal": signal,
    }


def support_resistance(df: pd.DataFrame) -> dict:
    """52 周高低点支撑阻力分析"""
    close = df["Close"].squeeze()
    price = float(close.iloc[-1])

    high_52w = float(close.rolling(252).max().iloc[-1]) if len(close) >= 252 else float(close.max())
    low_52w = float(close.rolling(252).min().iloc[-1]) if len(close) >= 252 else float(close.min())

    pct_from_high = (price - high_52w) / high_52w if high_52w > 0 else 0
    pct_from_low = (price - low_52w) / low_52w if low_52w > 0 else 0

    signal = "NONE"
    # Burry 原则：在 52 周低点 10-15% 范围内 = 潜在买入区
    if 0 < pct_from_low < 0.15:
        signal = "NEAR_SUPPORT"
    elif pct_from_high > -0.05:  # 接近历史高点
        signal = "NEAR_RESISTANCE"

    return {
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pct_from_high": round(pct_from_high, 4),
        "pct_from_low": round(pct_from_low, 4),
        "signal": signal,
    }


def intermarket_signals() -> dict:
    """
    跨资产信号。
    债券涨 + 股票跌 = 避险 → 等机会抄底。
    黄金涨 + 美元跌 = 通胀预期 → 利好商品。
    """
    signals = {}
    try:
        # TLT (长期国债) vs SPY
        tlt = yf.download("TLT", period="1mo", progress=False, auto_adjust=True)
        spy = yf.download("SPY", period="1mo", progress=False, auto_adjust=True)
        if not tlt.empty and not spy.empty:
            tlt_ret = float(tlt["Close"].squeeze().pct_change(20).iloc[-1])
            spy_ret = float(spy["Close"].squeeze().pct_change(20).iloc[-1])

            if tlt_ret > 0.02 and spy_ret < -0.02:
                signals["risk_off"] = "债券涨+股票跌 → 防御模式，等待超卖机会"
            elif tlt_ret < -0.02 and spy_ret > 0.02:
                signals["risk_on"] = "债券跌+股票涨 → 风险偏好，可适度加仓"

        # VIX
        vix = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        if not vix.empty:
            current_vix = float(vix["Close"].squeeze().iloc[-1])
            if current_vix > 25:
                signals["vix_warning"] = f"VIX={current_vix:.0f} 市场恐慌，注意风险"
            elif current_vix < 13:
                signals["vix_complacent"] = "VIX 极低，市场可能过于自满"

    except Exception as e:
        signals["error"] = str(e)

    return signals


def get_all_advanced_signals(symbol: str) -> dict:
    """获取单只标的的所有高级信号"""
    try:
        df = yf.download(symbol, period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return {"symbol": symbol, "error": "数据不足"}

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        return {
            "symbol": symbol,
            "mean_reversion": mean_reversion_signal(df),
            "volume_divergence": volume_divergence(df),
            "atr_breakout": atr_breakout(df),
            "support_resistance": support_resistance(df),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
