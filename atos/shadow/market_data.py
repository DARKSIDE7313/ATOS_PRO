"""
ATOS PRO — Shadow 市场数据与趋势判断 (Phase 5 框架重塑)
==========================================================
从 shadow_trader.py 抽离:
  - get_market_data_cached(): SPY/VIX 数据 10 分钟缓存 (Futu 优先, yfinance 降级)
  - compute_spy_trend(): SPY 趋势分级 (BULL/CAUTIOUS/BEAR/UNKNOWN)

逻辑逐字迁移, 零参数改动。
"""

import datetime

import pandas as pd
import numpy as np
import yfinance as yf

from atos.core.logging import get_logger

logger = get_logger("shadow_trader")

# ============================================================
# 缓存层
# ============================================================
_spy_cache = None
_vix_cache = None
_cache_ts = None
_CACHE_TTL_MINUTES = 10  # 10分钟缓存


def get_market_data_cached():
    """缓存SPY/VIX数据（中国大陆优化：Futu优先）"""
    global _spy_cache, _vix_cache, _cache_ts
    now = datetime.datetime.now()
    if _spy_cache is not None and _vix_cache is not None and _cache_ts is not None:
        if (now - _cache_ts).total_seconds() < _CACHE_TTL_MINUTES * 60:
            return _spy_cache, _vix_cache

    # 🆕 优先用Futu历史数据（中国大陆不被墙）
    try:
        from atos.data.futu_historical import get_spy_vix_data
        spy, vix = get_spy_vix_data()
        if spy is not None and not spy.empty and len(spy) >= 50:
            _spy_cache, _vix_cache, _cache_ts = spy, vix, now
            return spy, vix
    except Exception:
        pass

    # Fallback to yfinance
    try:
        spy = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True, timeout=15)
        vix = yf.download("^VIX", period="1y", interval="1d", progress=False, auto_adjust=True, timeout=15)
    except Exception:
        spy, vix = pd.DataFrame(), pd.DataFrame()

    _spy_cache, _vix_cache, _cache_ts = spy, vix, now
    return spy, vix


def compute_spy_trend(spy) -> str:
    """SPY 趋势分级 — 逐字迁移自 run_shadow_cycle 的 SPY趋势过滤段。

    v22: 放宽 BULL 判断 — 价格高于 MA20 即是牛市，不要求 >2%
    Returns: "BULL" | "CAUTIOUS" | "BEAR" | "UNKNOWN"
    """
    spy_trend = "BULL"  # Default optimistic
    try:
        spy_close_raw = spy["Close"]
        if isinstance(spy_close_raw, pd.DataFrame):
            spy_close = spy_close_raw.squeeze()
        else:
            spy_close = spy_close_raw

        # Convert to numpy, drop NaN
        spy_vals = spy_close.dropna().values
        if len(spy_vals) < 20:
            raise ValueError(f"SPY数据不足 ({len(spy_vals)}根有效K线)")

        spy_current = float(spy_vals[-1])
        spy_ma20 = float(np.mean(spy_vals[-20:]))
        spy_ma50 = float(np.mean(spy_vals[-50:])) if len(spy_vals) >= 50 else spy_ma20

        # 🏦 v22: 放宽 BULL 判断 — 价格高于 MA20 即是牛市，不要求 >2%
        if spy_current < spy_ma20 and spy_current < spy_ma50 and spy_ma20 < spy_ma50:
            spy_trend = "BEAR"
            logger.warning(f"🐻 SPY死叉: ${spy_current:.0f} < MA20=${spy_ma20:.0f} < MA50=${spy_ma50:.0f}")
        elif spy_current < spy_ma20 * 0.98:
            spy_trend = "CAUTIOUS"
            logger.info(f"🟡 SPY谨慎: ${spy_current:.0f} < MA20*0.98=${spy_ma20*0.98:.0f}")
        elif spy_current > spy_ma20:
            spy_trend = "BULL"
        else:
            spy_trend = "BULL"  # 默认乐观 — 轻微低于MA20不算谨慎
    except Exception as e:
        spy_trend = "UNKNOWN"
        logger.warning(f"SPY趋势分析失败 → 降级UNKNOWN: {e}")
    return spy_trend
