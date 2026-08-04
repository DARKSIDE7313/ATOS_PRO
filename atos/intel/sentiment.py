"""
ATOS Intel — 市场情绪分析
=========================
多维度情绪指标，辅助 AI 判断市场风险偏好。

数据源:
  - Fear & Greed Index (CNN)
  - VIX (CBOE 波动率)
  - Put/Call Ratio
  - AAII Sentiment Survey
  - Finnhub Market Sentiment
"""

import json, time, urllib.request
from typing import Dict, Optional
from atos.intel.news_engine import _cache_get, _cache_set, _fetch_url, FINNHUB_KEY
from atos.core.logging import get_logger

logger = get_logger("intel.sentiment")


def fetch_fear_greed() -> dict:
    """获取 CNN Fear & Greed Index"""
    cached = _cache_get("fear_greed", ttl_seconds=900)
    if cached:
        return cached.get("data", {})

    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        content = _fetch_url(url, timeout=10)
        if not content:
            return _default_fear_greed()

        data = json.loads(content)
        result = {
            "score": data.get("fear_and_greed", {}).get("score", 50),
            "rating": data.get("fear_and_greed", {}).get("rating", "neutral"),
            "timestamp": data.get("fear_and_greed", {}).get("timestamp", ""),
            "source": "cnn_fear_greed",
        }

        # 分类
        score = result["score"]
        if score >= 75:
            result["zone"] = "EXTREME_GREED"
        elif score >= 60:
            result["zone"] = "GREED"
        elif score >= 40:
            result["zone"] = "NEUTRAL"
        elif score >= 25:
            result["zone"] = "FEAR"
        else:
            result["zone"] = "EXTREME_FEAR"

        _cache_set("fear_greed", {"data": result})
        return result

    except Exception as e:
        logger.debug(f"Fear & Greed: {e}")
        return _default_fear_greed()


def _default_fear_greed() -> dict:
    return {"score": 50, "rating": "neutral", "zone": "NEUTRAL", "source": "default"}


def fetch_vix_level() -> dict:
    """获取 VIX 当前水平和解读"""
    cached = _cache_get("vix_level", ttl_seconds=300)
    if cached:
        return cached.get("data", {})

    try:
        import yfinance as yf
        vix_data = yf.download("^VIX", period="5d", interval="1d",
                               progress=False, auto_adjust=True)
        if not vix_data.empty:
            vix = float(vix_data["Close"].squeeze().iloc[-1])
        else:
            vix = 18.0
    except Exception:
        vix = 18.0

    if vix < 12:
        level = "EXTREMELY_LOW"
        risk = "COMPLACENCY"
    elif vix < 15:
        level = "LOW"
        risk = "LOW_RISK"
    elif vix < 20:
        level = "NORMAL"
        risk = "NORMAL"
    elif vix < 25:
        level = "ELEVATED"
        risk = "CAUTION"
    elif vix < 30:
        level = "HIGH"
        risk = "HIGH_RISK"
    else:
        level = "EXTREME"
        risk = "PANIC"

    result = {
        "vix": round(vix, 1),
        "level": level,
        "risk": risk,
        "trading_implication": _vix_implication(level),
        "source": "cboe_vix",
    }

    _cache_set("vix_level", {"data": result})
    return result


def _vix_implication(level: str) -> str:
    implications = {
        "EXTREMELY_LOW": "极度低波动 → 注意尾部风险，适当减仓",
        "LOW": "低波动 → 正常交易，可适度杠杆",
        "NORMAL": "正常 → 标准仓位",
        "ELEVATED": "偏高 → 降低仓位，收紧止损",
        "HIGH": "高波动 → 减仓，增加对冲",
        "EXTREME": "极度恐慌 → 现金为王，等待企稳",
    }
    return implications.get(level, "正常交易")


def fetch_market_sentiment() -> dict:
    """获取综合市场情绪（Finnhub）"""
    if not FINNHUB_KEY:
        return {"available": False}

    cached = _cache_get("market_sentiment", ttl_seconds=900)
    if cached:
        return cached.get("data", {})

    try:
        url = f"https://finnhub.io/api/v1/news/sentiment?token={FINNHUB_KEY}"
        content = _fetch_url(url, timeout=10)
        if not content:
            return {"available": False}

        data = json.loads(content)
        result = {
            "available": True,
            "bullish_pct": data.get("sentiment", {}).get("bullishPercent", 0),
            "bearish_pct": data.get("sentiment", {}).get("bearishPercent", 0),
            "buzz": data.get("buzz", {}),
            "source": "finnhub_sentiment",
        }

        _cache_set("market_sentiment", {"data": result})
        return result

    except Exception as e:
        logger.debug(f"Market sentiment: {e}")
        return {"available": False}


def get_sentiment_summary() -> dict:
    """获取情绪摘要（供 AI 决策使用）"""
    fg = fetch_fear_greed()
    vix = fetch_vix_level()

    # 综合评分: 0=极度恐惧, 100=极度贪婪
    fear_greed_score = fg.get("score", 50)
    vix_score = max(0, 100 - vix.get("vix", 18) * 4)

    composite = round(fear_greed_score * 0.6 + vix_score * 0.4)

    if composite >= 75:
        bias = "BULLISH"
        advice = "市场情绪乐观，可进攻"
    elif composite >= 40:
        bias = "NEUTRAL"
        advice = "市场情绪中性，正常交易"
    else:
        bias = "BEARISH"
        advice = "市场情绪悲观，防守为主"

    return {
        "composite_score": composite,
        "bias": bias,
        "advice": advice,
        "fear_greed": fg,
        "vix": vix,
        "updated": time.time(),
    }
