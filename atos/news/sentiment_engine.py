#!/usr/bin/env python3
"""
ATOS News Sentiment Engine
==========================
抓取免费财经新闻 → 关键词情绪分析 → 输出每股情绪分数
无需付费 API — 使用 RSS feeds + 关键词匹配

情绪分数: -1.0 (极度利空) 到 +1.0 (极度利好)
"""

import json
import os
import re
import time
import math
import hashlib
import threading
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError

from atos.core.logging import get_logger

logger = get_logger("news_sentiment")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_FILE = os.path.join(BASE, 'data', 'news_sentiment.json')
SEEN_FILE = os.path.join(BASE, 'data', 'news_seen_hashes.json')

# ── RSS 新闻源（免费，无需 API key）──
RSS_FEEDS = [
    # 综合财经
    ("https://feeds.marketwatch.com/marketwatch/topstories/", "MarketWatch"),
    ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "MW Pulse"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC"),
    ("https://www.cnbc.com/id/15839069/device/rss/rss.html", "CNBC Earnings"),
    ("https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "WSJ Markets"),
    ("https://feeds.bloomberg.com/markets/news.rss", "Bloomberg"),
    # 科技
    ("https://feeds.arstechnica.com/arstechnica/technology-lab", "ArsTechnica"),
    # 宏观
    ("https://feeds.marketwatch.com/marketwatch/economypolitics/", "MW Econ"),
]

# ── 情绪关键词词典 ──
BULLISH_WORDS = {
    # 强烈利好 (+3)
    'surge': 3, 'soar': 3, 'skyrocket': 3, 'breakout': 3, 'rally': 3,
    'record high': 3, 'all-time high': 3, 'beat expectations': 3, 'blowout': 3,
    'upgrade': 3, 'overweight': 3, 'strong buy': 3,
    # 中度利好 (+2)
    'gain': 2, 'rise': 2, 'jump': 2, 'climb': 2, 'boost': 2,
    'profit': 2, 'growth': 2, 'outperform': 2, 'bullish': 2,
    'buy': 2, 'positive': 2, 'optimistic': 2, 'recovery': 2,
    'exceeds': 2, 'tops estimates': 2, 'raises guidance': 2,
    # 轻度利好 (+1)
    'up': 1, 'higher': 1, 'improve': 1, 'advance': 1, 'stable': 1,
    'expand': 1, 'partnership': 1, 'innovation': 1, 'launch': 1,
    'dividend': 1, 'buyback': 1, 'repurchase': 1,
}

BEARISH_WORDS = {
    # 强烈利空 (-3)
    'crash': -3, 'plunge': -3, 'collapse': -3, 'tank': -3,
    'downgrade': -3, 'underweight': -3, 'sell-off': -3, 'selloff': -3,
    'bankruptcy': -3, 'default': -3, 'fraud': -3, 'investigation': -3,
    'recall': -3, 'halt': -3, 'suspend': -3,
    # 中度利空 (-2)
    'fall': -2, 'drop': -2, 'decline': -2, 'sink': -2, 'slide': -2,
    'loss': -2, 'miss': -2, 'weak': -2, 'bearish': -2,
    'warning': -2, 'layoff': -2, 'cut': -2, 'downsizing': -2,
    'lawsuit': -2, 'fine': -2, 'penalty': -2, 'sanction': -2,
    'misses estimates': -2, 'lowers guidance': -2, 'disappointing': -2,
    # 轻度利空 (-1)
    'down': -1, 'lower': -1, 'concern': -1, 'risk': -1, 'fear': -1,
    'uncertainty': -1, 'volatile': -1, 'pressure': -1, 'struggle': -1,
    'delay': -1, 'recall': -1, 'probe': -1, 'scrutiny': -1,
}

# ── 股票名称映射（新闻中常见名称 → 股票代码）──
STOCK_ALIASES = {
    'AAPL': ['apple', 'iphone', 'ipad', 'macbook', 'tim cook', 'app store', 'ios'],
    'MSFT': ['microsoft', 'azure', 'windows', 'satya nadella', 'office 365', 'copilot', 'openai'],
    'GOOGL': ['google', 'alphabet', 'sundar pichai', 'youtube', 'android', 'gemini', 'deepmind', 'search'],
    'AMZN': ['amazon', 'aws', 'andy jassy', 'prime', 'alexa', 'whole foods'],
    'NVDA': ['nvidia', 'jensen huang', 'gpu', 'cuda', 'ai chip', 'geforce', 'rtx'],
    'META': ['meta', 'facebook', 'instagram', 'whatsapp', 'mark zuckerberg', 'threads', 'oculus'],
    'TSLA': ['tesla', 'elon musk', 'model 3', 'model y', 'cybertruck', 'fsd', 'autopilot', 'spacex'],
    'JPM': ['jpmorgan', 'jp morgan', 'jamie dimon', 'chase bank'],
    'V': ['visa', 'payment card'],
    'MA': ['mastercard'],
    'JNJ': ['johnson & johnson', 'j&j', 'janssen'],
    'UNH': ['unitedhealth', 'united healthcare', 'optum'],
    'MRK': ['merck', 'keytruda'],
    'PFE': ['pfizer', 'paxlovid'],
    'ABBV': ['abbvie', 'humira', 'skyrizi'],
    'XOM': ['exxon', 'exxonmobil', 'exxon mobil'],
    'CVX': ['chevron'],
    'WMT': ['walmart', 'wal-mart', 'sam\'s club'],
    'COST': ['costco'],
    'HD': ['home depot'],
    'MCD': ['mcdonald', 'mcdonald\'s', 'big mac'],
    'SBUX': ['starbucks', 'frappuccino'],
    'NKE': ['nike', 'air jordan', 'just do it'],
    'DIS': ['disney', 'disney+', 'espn', 'marvel', 'star wars', 'pixar'],
    'NFLX': ['netflix'],
    'AMD': ['amd', 'ryzen', 'radeon', 'epyc', 'lisa su'],
    'INTC': ['intel', 'core i9', 'xeon'],
    'CRM': ['salesforce', 'marc benioff'],
    'AVGO': ['broadcom'],
    'QCOM': ['qualcomm', 'snapdragon'],
    'TXN': ['texas instruments'],
    'HON': ['honeywell'],
    'GS': ['goldman sachs', 'goldman'],
    'MS': ['morgan stanley'],
    'BAC': ['bank of america', 'bofa'],
    'WFC': ['wells fargo'],
    'C': ['citigroup', 'citi', 'citibank'],
    'AXP': ['american express', 'amex'],
    'BRK.B': ['berkshire', 'warren buffett', 'berkshire hathaway'],
    'LLY': ['eli lilly', 'mounjaro', 'zepbound'],
    'TMO': ['thermo fisher'],
    'ABT': ['abbott'],
    'DHR': ['danaher'],
    'BMY': ['bristol-myers', 'bristol myers'],
    'AMGN': ['amgen'],
    'GILD': ['gilead'],
    'ISRG': ['intuitive surgical', 'da vinci'],
    'BA': ['boeing', '737 max', '787 dreamliner'],
    'CAT': ['caterpillar'],
    'GE': ['general electric', 'ge aerospace'],
    'MMM': ['3m'],
    'LMT': ['lockheed martin', 'lockheed', 'f-35'],
    'RTX': ['raytheon', 'rtx corp'],
    'SPY': ['s&p 500', 'sp500', 's&p500', 'spy etf'],
    'QQQ': ['nasdaq 100', 'nasdaq100', 'qqq etf'],
    'IWM': ['russell 2000', 'russell2000', 'small cap'],
    'GLD': ['gold etf', 'gold price'],
    'TLT': ['treasury bond', '20 year treasury', 'bond yield'],
}

# ── 宏观关键词（影响整体市场）──
MACRO_KEYWORDS = {
    'fed': 0, 'federal reserve': 0, 'interest rate': 0, 'rate cut': 3, 'rate hike': -3,
    'inflation': -1, 'cpi': 0, 'gdp': 0, 'recession': -3, 'soft landing': 2,
    'unemployment': -1, 'jobs report': 0, 'nonfarm': 0,
    'tariff': -2, 'trade war': -2, 'sanctions': -1,
    'qe': 2, 'quantitative easing': 2, 'qt': -2, 'quantitative tightening': -2,
    'powell': 0, 'fomc': 0, 'dot plot': 0,
}


class NewsSentimentEngine:
    """新闻情绪引擎 — 抓取 RSS + 关键词分析 + 每股情绪评分"""

    def __init__(self):
        self.sentiment_cache = {}  # {symbol: {"score": float, "articles": int, "updated": str}}
        self.macro_sentiment = 0.0  # 整体市场情绪
        self.seen_hashes = self._load_seen()
        self._lock = threading.Lock()
        self._load_cache()

    def _load_seen(self) -> set:
        """加载已处理的新闻 hash，避免重复分析"""
        try:
            if os.path.exists(SEEN_FILE):
                with open(SEEN_FILE) as f:
                    return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
        return set()

    def _save_seen(self):
        """保存已处理新闻 hash（保留最近 5000 条）"""
        try:
            os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
            hashes = list(self.seen_hashes)[-5000:]
            with open(SEEN_FILE, 'w') as f:
                json.dump(hashes, f)
        except IOError:
            pass

    def _load_cache(self):
        """加载上次情绪缓存"""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                self.sentiment_cache = data.get('stocks', {})
                self.macro_sentiment = data.get('macro', 0.0)
                logger.info(f"📰 加载情绪缓存: {len(self.sentiment_cache)} 只股票, macro={self.macro_sentiment:.2f}")
        except (json.JSONDecodeError, IOError):
            pass

    def _save_cache(self):
        """保存情绪缓存"""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            data = {
                'stocks': self.sentiment_cache,
                'macro': round(self.macro_sentiment, 3),
                'updated': datetime.now().isoformat(),
            }
            with open(CACHE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    def _fetch_rss(self, url: str, source: str, timeout: int = 10) -> list:
        """抓取单个 RSS feed，返回 [{title, description, pub_date, source}]"""
        articles = []
        try:
            req = Request(url, headers={'User-Agent': 'ATOS/1.0'})
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            root = ET.fromstring(data)

            # RSS 2.0
            for item in root.iter('item'):
                title = item.findtext('title', '')
                desc = item.findtext('description', '')
                pub = item.findtext('pubDate', '')
                if title:
                    articles.append({
                        'title': title.strip(),
                        'description': (desc or '')[:500].strip(),
                        'pub_date': pub,
                        'source': source,
                    })

            # Atom
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            for entry in root.iter('{http://www.w3.org/2005/Atom}entry'):
                title = entry.findtext('atom:title', '', ns)
                summary = entry.findtext('atom:summary', '', ns)
                pub = entry.findtext('atom:published', '', ns) or entry.findtext('atom:updated', '', ns)
                if title:
                    articles.append({
                        'title': title.strip(),
                        'description': (summary or '')[:500].strip(),
                        'pub_date': pub,
                        'source': source,
                    })
        except (URLError, ET.ParseError, TimeoutError, OSError) as e:
            logger.debug(f"RSS 抓取失败 {source}: {e}")
        return articles

    def _hash_article(self, article: dict) -> str:
        """生成文章唯一 hash"""
        text = f"{article['title']}|{article['source']}"
        return hashlib.md5(text.encode()).hexdigest()[:16]

    def _analyze_text(self, text: str) -> float:
        """关键词情绪分析 — 返回 -1.0 到 +1.0"""
        text_lower = text.lower()
        score = 0.0
        hits = 0

        for word, weight in BULLISH_WORDS.items():
            if word in text_lower:
                score += weight
                hits += 1

        for word, weight in BEARISH_WORDS.items():
            if word in text_lower:
                score += weight  # weight is already negative
                hits += 1

        if hits == 0:
            return 0.0

        # 归一化到 [-1, 1]
        normalized = max(-1.0, min(1.0, score / max(hits * 2, 1)))
        return round(normalized, 3)

    def _extract_stock_mentions(self, text: str) -> dict:
        """从文本中提取提到的股票，返回 {symbol: relevance}"""
        text_lower = text.lower()
        mentions = {}

        for symbol, aliases in STOCK_ALIASES.items():
            for alias in aliases:
                if alias in text_lower:
                    # 主名称出现 = 高相关性，别名 = 中相关性
                    relevance = 1.0 if alias == aliases[0] else 0.7
                    mentions[symbol] = max(mentions.get(symbol, 0), relevance)
                    break  # 一个 symbol 只计一次

        return mentions

    def _analyze_macro(self, text: str) -> float:
        """分析宏观情绪"""
        text_lower = text.lower()
        score = 0.0
        hits = 0
        for keyword, weight in MACRO_KEYWORDS.items():
            if keyword in text_lower:
                score += weight
                hits += 1
        return score / max(hits, 1) if hits > 0 else 0.0

    def fetch_and_analyze(self) -> dict:
        """主入口：抓取所有 RSS → 分析情绪 → 更新缓存"""
        all_articles = []
        new_count = 0

        for url, source in RSS_FEEDS:
            articles = self._fetch_rss(url, source)
            all_articles.extend(articles)

        if not all_articles:
            logger.warning("📰 无新闻可分析")
            return self.sentiment_cache

        # 过滤已处理的文章
        fresh = []
        for art in all_articles:
            h = self._hash_article(art)
            if h not in self.seen_hashes:
                self.seen_hashes.add(h)
                fresh.append(art)
                new_count += 1

        if not fresh:
            logger.info(f"📰 无新文章（已处理 {len(all_articles)} 篇）")
            return self.sentiment_cache

        # 分析每篇文章
        stock_scores = {}  # {symbol: [(score, relevance)]}
        macro_scores = []

        for art in fresh:
            full_text = f"{art['title']} {art['description']}"
            sentiment = self._analyze_text(full_text)
            mentions = self._extract_stock_mentions(full_text)
            macro = self._analyze_macro(full_text)

            if macro != 0:
                macro_scores.append(macro)

            for symbol, relevance in mentions.items():
                if symbol not in stock_scores:
                    stock_scores[symbol] = []
                stock_scores[symbol].append((sentiment, relevance, art['title'][:80]))

        # 更新每股情绪（指数衰减旧分数，融合新分数）
        decay = 0.7  # 旧分数保留 70%
        with self._lock:
            for symbol, scores in stock_scores.items():
                weighted = sum(s * r for s, r, _ in scores)
                total_rel = sum(r for _, r, _ in scores)
                new_score = weighted / total_rel if total_rel > 0 else 0

                old = self.sentiment_cache.get(symbol, {})
                old_score = old.get('score', 0)
                blended = old_score * decay + new_score * (1 - decay)

                self.sentiment_cache[symbol] = {
                    'score': round(blended, 3),
                    'articles': old.get('articles', 0) + len(scores),
                    'latest_headline': scores[0][2] if scores else '',
                    'updated': datetime.now().isoformat(),
                }

            # 更新宏观情绪
            if macro_scores:
                new_macro = sum(macro_scores) / len(macro_scores)
                self.macro_sentiment = self.macro_sentiment * decay + new_macro * (1 - decay)

        self._save_cache()
        self._save_seen()

        logger.info(
            f"📰 新闻分析完成: {new_count} 篇新文章, "
            f"{len(stock_scores)} 只股票有情绪变化, "
            f"macro={self.macro_sentiment:.2f}"
        )
        return self.sentiment_cache

    def get_sentiment(self, symbol: str) -> float:
        """获取单只股票的情绪分数 (-1 到 +1)"""
        entry = self.sentiment_cache.get(symbol, {})
        return entry.get('score', 0.0)

    def get_macro_sentiment(self) -> float:
        """获取整体市场情绪"""
        return self.macro_sentiment

    def get_top_sentiment(self, n: int = 10) -> list:
        """返回情绪最好的 N 只股票"""
        sorted_stocks = sorted(
            self.sentiment_cache.items(),
            key=lambda x: x[1].get('score', 0),
            reverse=True
        )
        return sorted_stocks[:n]

    def get_worst_sentiment(self, n: int = 10) -> list:
        """返回情绪最差的 N 只股票"""
        sorted_stocks = sorted(
            self.sentiment_cache.items(),
            key=lambda x: x[1].get('score', 0)
        )
        return sorted_stocks[:n]

    def get_summary(self) -> dict:
        """返回情绪摘要（给 Dashboard 用）"""
        top = self.get_top_sentiment(5)
        worst = self.get_worst_sentiment(5)
        return {
            'macro_sentiment': round(self.macro_sentiment, 3),
            'total_stocks_tracked': len(self.sentiment_cache),
            'top_bullish': [
                {'symbol': s, 'score': d['score'], 'headline': d.get('latest_headline', '')}
                for s, d in top if d['score'] > 0
            ],
            'top_bearish': [
                {'symbol': s, 'score': d['score'], 'headline': d.get('latest_headline', '')}
                for s, d in worst if d['score'] < 0
            ],
            'updated': datetime.now().isoformat(),
        }


# ── 全局单例 ──
_engine = None

def get_engine() -> NewsSentimentEngine:
    global _engine
    if _engine is None:
        _engine = NewsSentimentEngine()
    return _engine

def get_sentiment(symbol: str) -> float:
    """快捷函数：获取股票情绪分数"""
    return get_engine().get_sentiment(symbol)

def get_macro_sentiment() -> float:
    """快捷函数：获取宏观情绪"""
    return get_engine().get_macro_sentiment()

def refresh_news():
    """快捷函数：抓取并分析新闻"""
    return get_engine().fetch_and_analyze()


if __name__ == '__main__':
    print("=" * 50)
    print("📰 ATOS News Sentiment Engine")
    print("=" * 50)
    engine = NewsSentimentEngine()
    result = engine.fetch_and_analyze()
    summary = engine.get_summary()
    print(f"\n宏观情绪: {summary['macro_sentiment']}")
    print(f"追踪股票: {summary['total_stocks_tracked']}")
    print(f"\n🟢 利好 Top 5:")
    for s in summary['top_bullish']:
        print(f"  {s['symbol']}: {s['score']:+.3f} — {s['headline'][:60]}")
    print(f"\n🔴 利空 Top 5:")
    for s in summary['top_bearish']:
        print(f"  {s['symbol']}: {s['score']:+.3f} — {s['headline'][:60]}")
