"""
ATOS Intel — 多源新闻聚合引擎
=============================
从多个数据源实时抓取财经新闻，智能去重排序。

数据源优先级:
  1. Yahoo Finance RSS (免费, 实时)
  2. Finnhub News API (免费套餐, 60次/分钟)
  3. MarketAux API (免费, 100次/天)

缓存策略:
  - 新闻: 5分钟 TTL
  - 情绪数据: 15分钟 TTL
  - 经济数据: 1小时 TTL
"""

import os, json, time, hashlib, urllib.request, xml.etree.ElementTree as ET
import datetime as dt
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from atos.core.logging import get_logger

logger = get_logger("intel.news")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
CACHE_DIR = os.path.join(BASE_DIR, "data", "intel_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Finnhub API Key (免费: https://finnhub.io/)
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

# 重要关键词（影响股价的新闻优先）
HIGH_IMPACT_KEYWORDS = [
    "earnings", "revenue", "profit", "loss", "guidance", "forecast",
    "acquisition", "merger", "takeover", "buyout", "deal",
    "FDA", "approval", "approved", "clinical trial",
    "layoff", "restructuring", "bankruptcy", "chapter 11",
    "upgrade", "downgrade", "outperform", "underperform",
    "beat", "miss", "estimate", "consensus",
    "dividend", "buyback", "split", "offering",
    "CEO", "CFO", "executive", "resign", "appoint",
    "lawsuit", "investigation", "SEC", "DOJ", "fine",
    "cyber", "hack", "breach", "data leak",
    "AI", "artificial intelligence", "breakthrough",
    "rate", "Fed", "inflation", "CPI", "GDP", "jobs",
]

MEDIUM_IMPACT_KEYWORDS = [
    "partnership", "collaboration", "contract", "expansion",
    "launch", "release", "new product", "update",
    "growth", "decline", "outlook", "target",
    "bullish", "bearish", "rally", "sell-off", "correction",
    "China", "trade war", "tariff", "sanction",
    "OPEC", "oil", "energy", "supply chain",
]


def _cache_get(key: str, ttl_seconds: int = 300) -> Optional[dict]:
    """从缓存读取"""
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                data = json.load(f)
            age = time.time() - data.get("_cached_at", 0)
            if age < ttl_seconds:
                return data
        except Exception:
            pass
    return None


def _cache_set(key: str, data: dict):
    """写入缓存"""
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    data["_cached_at"] = time.time()
    try:
        with open(cache_file, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """通用 HTTP GET"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ATOS-PRO/3.0 (Market Intelligence Engine)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"获取失败 {url[:60]}: {e}")
        return None


# ═══════════════════════════════════════════════
# Source 1: Yahoo Finance RSS
# ═══════════════════════════════════════════════

def fetch_yahoo_news(symbols: List[str] = None, max_per_symbol: int = 5) -> List[dict]:
    """从 Yahoo Finance RSS 获取新闻"""
    if symbols is None:
        symbols = ["SPY", "QQQ", "^GSPC"]  # 市场整体新闻

    all_news = []
    for sym in symbols[:5]:  # 限制请求数
        cache_key = f"yahoo_news_{sym}"
        cached = _cache_get(cache_key, ttl_seconds=300)
        if cached:
            all_news.extend(cached.get("items", []))
            continue

        try:
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
            content = _fetch_url(url, timeout=10)
            if not content:
                continue

            root = ET.fromstring(content)
            items = []
            for item in root.iter("item"):
                title = item.find("title")
                link = item.find("link")
                pub_date = item.find("pubDate")
                desc = item.find("description")

                title_text = title.text if title is not None else ""
                items.append({
                    "title": title_text,
                    "link": link.text if link is not None else "",
                    "published": pub_date.text if pub_date is not None else "",
                    "description": (desc.text or "")[:300] if desc is not None else "",
                    "source": "yahoo_finance",
                    "symbol": sym,
                })

            _cache_set(cache_key, {"items": items})
            all_news.extend(items[:max_per_symbol])

        except Exception as e:
            logger.debug(f"Yahoo RSS {sym}: {e}")

    return _deduplicate_and_score(all_news)


# ═══════════════════════════════════════════════
# Source 2: Finnhub News
# ═══════════════════════════════════════════════

def fetch_finnhub_news(category: str = "general", limit: int = 20) -> List[dict]:
    """从 Finnhub 获取最新财经新闻"""
    if not FINNHUB_KEY:
        return []

    cache_key = f"finnhub_news_{category}"
    cached = _cache_get(cache_key, ttl_seconds=300)
    if cached:
        return cached.get("items", [])[:limit]

    try:
        url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_KEY}"
        content = _fetch_url(url, timeout=10)
        if not content:
            return []

        data = json.loads(content)
        items = []
        for article in data[:limit]:
            items.append({
                "title": article.get("headline", ""),
                "link": article.get("url", ""),
                "published": dt.datetime.fromtimestamp(
                    article.get("datetime", 0)
                ).isoformat() if article.get("datetime") else "",
                "description": article.get("summary", "")[:300],
                "source": "finnhub",
                "category": article.get("category", ""),
                "related_symbols": article.get("related", ""),
            })

        _cache_set(cache_key, {"items": items})
        return items

    except Exception as e:
        logger.debug(f"Finnhub news: {e}")
        return []


# ═══════════════════════════════════════════════
# Source 3: Finnhub Insider Trading
# ═══════════════════════════════════════════════

def fetch_insider_trades(symbol: str = None, limit: int = 10) -> List[dict]:
    """获取内部人交易数据"""
    if not FINNHUB_KEY:
        return []

    cache_key = f"insider_{symbol or 'all'}"
    cached = _cache_get(cache_key, ttl_seconds=900)
    if cached:
        return cached.get("items", [])[:limit]

    try:
        if symbol:
            url = f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}"
        else:
            url = f"https://finnhub.io/api/v1/stock/market-insider-sentiment?token={FINNHUB_KEY}"

        content = _fetch_url(url, timeout=10)
        if not content:
            return []

        data = json.loads(content)
        items = []

        if symbol and "data" in data:
            for trade in data["data"][:limit]:
                change = trade.get("change", 0)
                items.append({
                    "symbol": symbol,
                    "name": trade.get("name", ""),
                    "share_price": trade.get("transactionPrice", 0),
                    "change": change,
                    "type": "BUY" if change > 0 else "SELL",
                    "source": "finnhub_insider",
                })
        elif "data" in data:
            for entry in data["data"][:limit]:
                items.append({
                    "month": entry.get("month"),
                    "change": entry.get("change", 0),
                    "mspr": entry.get("mspr", 0),  # Month Share Purchase Ratio
                    "source": "finnhub_insider",
                })

        _cache_set(cache_key, {"items": items})
        return items

    except Exception as e:
        logger.debug(f"Insider trades: {e}")
        return []


# ═══════════════════════════════════════════════
# Source 4: Finnhub Market News (General)
# ═══════════════════════════════════════════════

def fetch_market_news(limit: int = 30) -> List[dict]:
    """获取综合市场新闻（Yahoo + Finnhub）"""
    news = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(fetch_yahoo_news, ["SPY", "QQQ", "^GSPC", "AAPL", "NVDA"]): "yahoo",
            executor.submit(fetch_finnhub_news, "general", limit): "finnhub",
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                news.extend(result)
            except Exception as e:
                logger.debug(f"新闻源 {futures[future]}: {e}")

    return _deduplicate_and_score(news)[:limit]


# ═══════════════════════════════════════════════
# News Scoring & Deduplication
# ═══════════════════════════════════════════════

def _score_news_impact(title: str, description: str = "") -> float:
    """评估新闻对股价的影响分数 (0.0 - 1.0)"""
    text = f"{title} {description}".lower()
    score = 0.0

    for keyword in HIGH_IMPACT_KEYWORDS:
        if keyword.lower() in text:
            score += 0.15

    for keyword in MEDIUM_IMPACT_KEYWORDS:
        if keyword.lower() in text:
            score += 0.05

    return min(score, 1.0)


def _deduplicate_and_score(news_items: List[dict]) -> List[dict]:
    """去重 + 评分 + 排序"""
    seen_titles = set()
    unique = []

    for item in news_items:
        title_hash = hashlib.md5(
            item.get("title", "")[:100].lower().encode()
        ).hexdigest()

        if title_hash in seen_titles:
            continue
        seen_titles.add(title_hash)

        item["impact_score"] = round(
            _score_news_impact(item.get("title", ""), item.get("description", "")), 2
        )
        unique.append(item)

    # 按影响分数 + 时间排序
    unique.sort(key=lambda x: (-x.get("impact_score", 0), x.get("published", "")))
    return unique


# ═══════════════════════════════════════════════
# Stock-specific News
# ═══════════════════════════════════════════════

def fetch_stock_news(symbols: List[str]) -> Dict[str, List[dict]]:
    """获取多只股票的新闻，返回 {symbol: [news_items]}"""
    result = {}
    for sym in symbols[:10]:
        news = fetch_yahoo_news([sym], max_per_symbol=3)
        if news:
            result[sym] = news
    return result
