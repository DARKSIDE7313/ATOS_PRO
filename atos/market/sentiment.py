"""
ATOS PRO v2 — 市场情绪分析
===========================
数据源：yfinance 新闻 + Finnhub 免费 API（无需 Key 也能用基础功能）
给 AI 决策提供"市场现在在想什么"的背景。

输出：
  - market_sentiment: BULLISH / BEARISH / NEUTRAL
  - fear_greed_index: 0-100 (0=极度恐惧, 100=极度贪婪)
  - top_headlines: 影响最大的5条新闻
  - sector_sentiment: 各行业的情绪
"""

import yfinance as yf
import requests
import json
from atos.core.logging import get_logger

logger = get_logger("market.sentiment")


def get_sp500_sentiment() -> dict:
    """
    通过 yfinance 获取 S&P 500 相关新闻并估算情绪。
    简单方法：数正面词 vs 负面词。
    """
    try:
        spy = yf.Ticker("SPY")
        news = spy.news[:20] if hasattr(spy, 'news') and spy.news else []

        bullish_words = [
            "surge", "rally", "jump", "gain", "rise", "upbeat", "bullish",
            "beat", "upgrade", "strong", "growth", "record", "boost", "positive",
            "上涨", "反弹", "利好", "突破", "新高",
        ]
        bearish_words = [
            "plunge", "drop", "fall", "decline", "slump", "bearish", "crash",
            "fear", "downgrade", "weak", "recession", "risk", "warn", "negative",
            "下跌", "崩盘", "利空", "跌破", "新低",
        ]

        bullish_count = 0
        bearish_count = 0
        headlines = []

        for n in news[:20]:
            title = n.get("content", {}).get("title", "") or n.get("title", "")
            if not title:
                continue
            title_lower = title.lower()
            headlines.append(title)

            for w in bullish_words:
                if w in title_lower:
                    bullish_count += 1
            for w in bearish_words:
                if w in title_lower:
                    bearish_count += 1

        total = max(bullish_count + bearish_count, 1)
        # 恐惧贪婪指数: 50 中性, >50 贪婪, <50 恐惧
        fear_greed = int(50 + (bullish_count - bearish_count) / total * 50)
        fear_greed = max(0, min(100, fear_greed))

        if fear_greed > 65:
            sentiment = "GREEDY"
        elif fear_greed < 35:
            sentiment = "FEARFUL"
        elif bullish_count > bearish_count:
            sentiment = "BULLISH"
        elif bearish_count > bullish_count:
            sentiment = "BEARISH"
        else:
            sentiment = "NEUTRAL"

        logger.info(f"市场情绪: {sentiment} (恐惧贪婪={fear_greed}, B={bullish_count} bears={bearish_count})")

        return {
            "sentiment": sentiment,
            "fear_greed_index": fear_greed,
            "bullish_mentions": bullish_count,
            "bearish_mentions": bearish_count,
            "headlines": headlines[:5],
            "source": "yfinance_news",
        }

    except Exception as e:
        logger.error(f"情绪分析失败: {e}")
        return {"sentiment": "NEUTRAL", "fear_greed_index": 50, "error": str(e)}


def get_vix_sentiment() -> dict:
    """从 VIX 推断市场恐惧程度"""
    try:
        vix = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        if vix.empty:
            return {"vix": 18, "vix_signal": "NORMAL"}
        current = float(vix["Close"].squeeze().iloc[-1])

        if current < 15:
            signal = "COMPLACENT"  # 自满
        elif current < 20:
            signal = "NORMAL"
        elif current < 25:
            signal = "CAUTIOUS"
        elif current < 30:
            signal = "FEARFUL"
        else:
            signal = "PANIC"

        return {
            "vix": round(current, 1),
            "vix_signal": signal,
            "vix_percentile": "low" if current < 20 else ("high" if current > 25 else "mid"),
        }
    except Exception as e:
        return {"vix": 18, "vix_signal": "NORMAL", "error": str(e)}


def get_sector_sentiment(symbols: list[str]) -> dict:
    """获取主要标的的涨跌比例，判断广度"""
    try:
        tickers = yf.Tickers(" ".join(symbols[:10]))
        up = 0
        down = 0
        for sym in symbols[:10]:
            try:
                t = tickers.tickers.get(sym)
                if t and hasattr(t, 'fast_info'):
                    prev = getattr(t.fast_info, 'previousClose', 0) or \
                           getattr(t.fast_info, 'regularMarketPreviousClose', 0)
                    curr = getattr(t.fast_info, 'lastPrice', 0) or \
                           getattr(t.fast_info, 'regularMarketPrice', 0)
                    if prev and curr and curr > prev: up += 1
                    elif prev and curr: down += 1
            except Exception:
                pass

        total = max(up + down, 1)
        breadth = int(up / total * 100) if total > 0 else 50

        return {
            "breadth": breadth,
            "up_stocks": up,
            "down_stocks": down,
            "breadth_signal": "BROAD_RALLY" if breadth > 70 else
                              ("BROAD_DECLINE" if breadth < 30 else "MIXED"),
        }
    except Exception:
        return {"breadth": 50, "breadth_signal": "MIXED"}


def get_full_sentiment(universe_symbols: list[str] = None) -> dict:
    """
    完整市场情绪快照。
    可以直接喂给 AI 决策引擎。
    """
    if universe_symbols is None:
        universe_symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

    news = get_sp500_sentiment()
    vix = get_vix_sentiment()
    breadth = get_sector_sentiment(universe_symbols)

    # 综合情绪评分
    scores = {
        "FEARFUL": -2, "BEARISH": -1, "NEUTRAL": 0, "BULLISH": 1, "GREEDY": 2,
    }
    news_score = scores.get(news.get("sentiment", "NEUTRAL"), 0)

    vix_signal = vix.get("vix_signal", "NORMAL")
    vix_score = {"COMPLACENT": 1, "NORMAL": 0, "CAUTIOUS": -1, "FEARFUL": -2, "PANIC": -3}.get(vix_signal, 0)

    breadth_score = {"BROAD_RALLY": 2, "MIXED": 0, "BROAD_DECLINE": -2}.get(
        breadth.get("breadth_signal", "MIXED"), 0)

    composite = (news_score * 0.3 + vix_score * 0.4 + breadth_score * 0.3)
    if composite > 1:
        overall = "BULLISH"
    elif composite < -1:
        overall = "BEARISH"
    else:
        overall = "NEUTRAL"

    result = {
        "overall": overall,
        "composite_score": round(composite, 2),
        "fear_greed_index": news.get("fear_greed_index", 50),
        "vix": vix.get("vix", 18),
        "vix_signal": vix_signal,
        "breadth": breadth.get("breadth_signal", "MIXED"),
        "headlines": news.get("headlines", [])[:5],
    }

    logger.info(f"综合情绪: {overall} (score={composite:.1f})")
    return result
