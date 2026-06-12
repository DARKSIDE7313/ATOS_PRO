"""
ATOS PRO v2 — 信号引擎
======================
技术指标计算：RSI、MACD、布林带、ATR、量比、趋势判断。
覆盖 50 只精选美股（从 atos.core.universe 导入）。

🆕 实时数据支持:
  - 默认使用 yfinance 获取历史数据（MA50, MA200, RSI 等指标）
  - 可通过 use_realtime=True 参数使用 FutuRealtimeFeed 获取当前价格
  - 实时数据源延迟 < 1 秒（需 FutuOpenD 运行）
"""
import os
import time
import threading
import pandas as pd
import yfinance as yf
from functools import lru_cache
from datetime import datetime, timedelta
from atos.core.universe import ALL_SYMBOLS, LONG_TERM_SYMBOLS, SHORT_TERM_SYMBOLS
from atos.core.logging import get_logger, log_signal, log_error

logger = get_logger("signal_engine")

# 🆕 实时数据源导入（降级友好 — 如果出错不影响历史信号）
_REALTIME_AVAILABLE = False
try:
    from atos.live.realtime_feeds import get_feed, FutuRealtimeFeed
    _REALTIME_AVAILABLE = True
except ImportError:
    pass

# Bug #10: yfinance 缓存层 — 仅在交易日内缓存，周末不缓存
_cache = {}  # {symbol: (timestamp, dataframe)}

# 自愈: yfinance SQLite 缓存修复
def _repair_yfinance_cache():
    """yfinance 的 SQLite 缓存 (tkr-tz.db/cookies.db) 有时会在异常退出后留下 .db-wal/.db-shm 
    残留文件，导致后续下载失败 'unable to open database file'。
    同时也修复 'no such table: _tz_kv' 错误——yfinance 懒创建表时多线程竞争会失败。
    
    BUGFIX 2026-06-11: 表名 _tz_kv 是错的，yfinance 实际用的是 tkr-tz 表。
                      同时添加完整表创建和权限修复。"""
    import glob
    import sqlite3
    import stat
    cache_dir = os.path.expanduser('~/Library/Caches/py-yfinance')
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
        return
    
    # 1. 清除 WAL/SHM 残留
    for pattern in ('*.db-wal', '*.db-shm'):
        for f in glob.glob(os.path.join(cache_dir, pattern)):
            try:
                os.remove(f)
                logger.warning(f"自愈: 已清除 yfinance 缓存残留 {os.path.basename(f)}")
            except OSError:
                pass
    
    # 2. 预创建 yfinance 需要的表（表名必须匹配 yfinance 内部使用）
    #    yfinance 使用 tkr-tz 表名（不是 _tz_kv！）和多线程竞争崩溃修复
    for db_name in ('tkr-tz.db', 'cookies.db'):
        db_path = os.path.join(cache_dir, db_name)
        try:
            conn = sqlite3.connect(db_path)
            # 启用 WAL 模式 — 多线程读写更安全
            conn.execute("PRAGMA journal_mode=WAL")
            # yfinance 实际用的表是 tkr-tz，不是 _tz_kv
            conn.execute("CREATE TABLE IF NOT EXISTS 'tkr-tz' (key TEXT PRIMARY KEY, value TEXT)")
            # yfinance 1.4.1 需要额外列和新表
            for col in ['strategy', 'exchange']:
                try:
                    conn.execute(f"ALTER TABLE 'tkr-tz' ADD COLUMN {col} TEXT DEFAULT ''")
                except Exception:
                    pass  # 列已存在
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS cookie (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS '_cookieschema' (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS _tz_kv (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            conn.close()
            # 修复权限（防止其他进程不能写）
            try:
                os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
            except Exception:
                pass
            logger.info(f"自愈: 已重建 yfinance 缓存 {db_name} (含正确表名 tkr-tz)")
        except Exception as e:
            logger.warning(f"自愈: 缓存 {db_name} 创建失败: {e}")

# 启动时修复一次
_repair_yfinance_cache()
_CACHE_TTL = timedelta(minutes=3)  # 3 分钟短缓存（比原来的5分钟更短）

def _should_skip_cache() -> bool:
    """如果是非交易日或盘后，跳过缓存使用实时数据"""
    try:
        from atos.live.realtime_feeds import get_feed
        feed = get_feed()
        if feed and feed.is_connected():
            return True  # 有实时数据 → 跳过 yfinance 缓存
    except Exception:
        pass
    return False

def _get_cached_data(symbol: str, period: str = "1y", interval: str = "1d"):
    """带缓存的 yfinance 下载，同一标的 3 分钟内只下载一次。
    有实时数据时跳过缓存直接下载最新。"""
    key = f"{symbol}:{period}:{interval}"
    now = datetime.now()
    if key in _cache:
        ts, df = _cache[key]
        if now - ts < _CACHE_TTL:
            return df
    
    # 多轮重试：yfinance 在缓存重建后首次下载可能失败
    max_attempts = 3
    last_error = None
    for attempt in range(max_attempts):
        try:
            try:
                df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
            except Exception:
                # BUGFIX 2026-06-12: yfinance 1.4.1 的 download() 有 'no such column: t1.strategy' bug
                # 回退到 Ticker.history() 方式
                ticker = yf.Ticker(symbol)
                df = ticker.history(period=period, interval=interval, auto_adjust=True)
            if df is not None and not df.empty:
                _cache[key] = (datetime.now(), df)
                return df
            last_error = f"empty dataframe (attempt {attempt+1})"
        except Exception as e:
            last_error = str(e)
        if attempt < max_attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    
    logger.warning(f"yfinance 下载失败 ({max_attempts}次重试): {symbol} — {last_error}")
    _cache[key] = (datetime.now(), pd.DataFrame())
    return pd.DataFrame()

def clear_cache():
    """强制清空缓存（手动更新用）"""
    _cache.clear()

def _is_edt() -> bool:
    """Check if US Eastern time is currently in EDT (Daylight Saving).
    Thread-safe via zoneinfo (no os.environ mutation)."""
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo("America/New_York")
        now_ny = datetime.now(tz)
        # EDT is UTC-4, EST is UTC-5. If utc_offset == -4 hours → EDT.
        return now_ny.utcoffset().total_seconds() / 3600 == -4
    except Exception:
        # Fallback: use time.daylight (not thread-safe but only reached on zoneinfo failure)
        try:
            return time.daylight != 0
        except Exception:
            return False


def is_nasdaq_open() -> bool:
    """判断当前时间是否在纳斯达克交易时段内（9:30AM-4:00PM ET，含DST自动侦测）。

    规则：
      - 周末（周六/周日）全天休市
      - EDT (夏令时) = UTC-4，9:30 ET = 13:30 UTC
      - EST (冬令时) = UTC-5，9:30 ET = 14:30 UTC
      - 收盘时间对应 UTC: 20:00 (EDT) / 21:00 (EST)
    """
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)

    # 周末
    if now_utc.weekday() >= 5:
        return False

    is_edt = _is_edt()
    if is_edt:
        open_utc = 13  # 9:30 AM EDT = 13:30 UTC
        close_utc = 20  # 4:00 PM EDT = 20:00 UTC
    else:
        open_utc = 14  # 9:30 AM EST = 14:30 UTC
        close_utc = 21  # 4:00 PM EST = 21:00 UTC

    hour, minute = now_utc.hour, now_utc.minute
    if hour < open_utc or (hour == open_utc and minute < 30):
        return False
    if hour >= close_utc:
        return False
    return True

# 兼容旧代码的导出
UNIVERSE = {
    "long_term":  LONG_TERM_SYMBOLS,
    "short_term": SHORT_TERM_SYMBOLS,
}


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """相对强弱指数"""
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """平均真实波幅"""
    high, low, close = df["High"], df["Low"], df["Close"]
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1]) if len(tr) >= period else 0.0


def _bollinger(series: pd.Series, period: int = 20, std: float = 2.0) -> dict:
    """布林带"""
    ma = series.rolling(period).mean()
    std_dev = series.rolling(period).std()
    upper = ma + std * std_dev
    lower = ma - std * std_dev
    last_price = float(series.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    last_ma = float(ma.iloc[-1])
    # band width 和 %B
    band_width = (last_upper - last_lower) / last_ma if last_ma > 0 else 0.0
    pct_b = (last_price - last_lower) / (last_upper - last_lower) if last_upper != last_lower else 0.5
    return {
        "upper": round(last_upper, 2),
        "middle": round(last_ma, 2),
        "lower": round(last_lower, 2),
        "band_width": round(band_width, 4),
        "pct_b": round(pct_b, 2),
    }


def _scalar(val):
    """强制转为 Python 原生 float"""
    if hasattr(val, 'iloc'):
        val = val.iloc[-1]
    if hasattr(val, 'item'):
        return float(val.item())
    return float(val)


# 🆕 新闻催化剂分数 — 从Yahoo Finance RSS获取标题，关键词匹配加分
_NEWS_CACHE = {}
_NEWS_TTL = timedelta(hours=2)

NEWS_KEYWORDS = {
    'surge': 0.10, 'rally': 0.12, 'upgrade': 0.15, 'beat': 0.10,
    'positive': 0.08, 'breakthrough': 0.15, 'AI': 0.05, 'growth': 0.05,
    'record': 0.08, 'bullish': 0.12, 'outperform': 0.12,
    'buy': 0.05, 'strong': 0.05, 'up': 0.03, 'raise': 0.08,
    'partner': 0.08, 'launch': 0.06,
}


def _calc_news_score(symbol: str) -> float:
    """基于新闻标题关键词匹配计算催化剂分数 (0.0 - 0.20)
    限制 NEWS_CACHE 最大200条防止内存泄露。
    此函数失败不会影响主流程——永远返回0.0而不是抛异常。
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    now = datetime.now()
    if symbol in _NEWS_CACHE:
        ts, score = _NEWS_CACHE[symbol]
        if now - ts < _NEWS_TTL:
            return score

    if len(_NEWS_CACHE) > 200:
        # 溢出保护：删最旧的100条
        old_keys = sorted(_NEWS_CACHE.keys(), key=lambda k: _NEWS_CACHE[k][0])[:100]
        for k in old_keys:
            del _NEWS_CACHE[k]

    score = 0.0
    try:
        url = f"https://finance.yahoo.com/rss/headline?s={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            tree = ET.parse(resp)
            root = tree.getroot()
            titles = []
            for item in root.iter('item'):
                title_el = item.find('title')
                if title_el is not None and title_el.text:
                    titles.append(title_el.text.lower())

        if not titles:
            _NEWS_CACHE[symbol] = (now, 0.0)
            return 0.0

        matches = 0
        for title in titles[:5]:
            for keyword, boost in NEWS_KEYWORDS.items():
                if keyword.lower() in title:
                    score += boost
                    matches += 1
                    break

        if matches >= 3:
            score += 0.05

        score = min(score, 0.20)
        _NEWS_CACHE[symbol] = (now, score)
        return round(score, 3)

    except Exception:
        _NEWS_CACHE[symbol] = (now, 0.0)
        return 0.0  # 新闻抓取失败不报错，返回0分


# 🆕 SMC 聪明钱分数 — 缓存版本（避免每个标的都重算K线数据）
_SMC_CACHE = {}
_SMC_TTL = timedelta(hours=1)


def _calc_smc_score(symbol: str, df: pd.DataFrame) -> dict:
    """计算SMC聪明钱分数，带缓存（1小时有效）
    
    缓存最多100条防止内存泄露。失败时返回空结构不影响主流程。
    """
    now = datetime.now()
    if symbol in _SMC_CACHE:
        ts, score = _SMC_CACHE[symbol]
        if now - ts < _SMC_TTL:
            return score
    
    if len(_SMC_CACHE) > 100:
        old_keys = sorted(_SMC_CACHE.keys(), key=lambda k: _SMC_CACHE[k][0])[:50]
        for k in old_keys:
            del _SMC_CACHE[k]
    
    empty_result = {"smc_score": 0.0, "smc_breakdown": {}, "ob": {}, "fvg": {},
                    "structure": {}, "liquidity": {}, "stop_hunt": {}}
    try:
        from atos.factors.smc import compute_smc_score
        result = compute_smc_score(symbol, df)
        _SMC_CACHE[symbol] = (now, result)
        return result
    except Exception as e:
        logger.debug(f"SMC计算失败 {symbol}: {e}")
        _SMC_CACHE[symbol] = (now, empty_result)
        return empty_result


def get_signals(symbols: list[str] = None) -> dict:
    """
    计算所有标的的技术信号。
    返回 {symbol: {price, ma50, ma200, rsi, macd_hist, trend, volume_ratio, atr, bollinger}}
    """
    if symbols is None:
        symbols = ALL_SYMBOLS

    results = {}
    total = len(symbols)

    for i, sym in enumerate(symbols):
        try:
            df = _get_cached_data(sym, period="1y", interval="1d")
            if df.empty or len(df) < 50:
                logger.debug(f"跳过 {sym}: 数据不足 ({len(df)}行)")
                continue

            # 处理 MultiIndex columns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            close = df["Close"].squeeze()
            vol = df["Volume"].squeeze()
            price = _scalar(close)
            # BUGFIX: 数据不足50行的已被跳过，但MA50/MA200仍需防御nan
            ma50_series = close.rolling(50).mean()
            ma50 = _scalar(ma50_series) if not ma50_series.empty and len(close) >= 50 else price
            if len(df) >= 200:
                ma200 = _scalar(close.rolling(200).mean())
            else:
                ma200 = ma50  # 不足200天用MA50替代，但后续判断会谨慎
            rsi_val = _scalar(_rsi(close))
            macd_line = close.ewm(span=12).mean() - close.ewm(span=26).mean()
            signal_line = macd_line.ewm(span=9).mean()
            macd_hist = _scalar(macd_line - signal_line)
            vol_avg = _scalar(vol.rolling(20).mean())
            vol_today = _scalar(vol)
            atr_val = _atr(df)
            boll = _bollinger(close)

            # 趋势判断（更精细的规则）
            if price > ma50 > ma200:
                trend = "UP"
            elif price < ma50 < ma200:
                trend = "DOWN"
            elif price > ma50:
                trend = "WEAK_UP"
            elif price < ma50:
                trend = "WEAK_DOWN"
            else:
                trend = "NEUTRAL"

            # 防御 nan：price 不能是 nan 或 0
            safe_price = price if not (isinstance(price, float) and str(price) == "nan") else 0
            if safe_price <= 0 and len(close) > 1:
                safe_price = _scalar(close.iloc[-2])  # 用前一天收盘价

            results[sym] = {
                "price":        round(safe_price, 2),
                "ma50":         round(ma50, 2),
                "ma200":        round(ma200, 2),
                "rsi":          round(rsi_val, 1),
                "macd_hist":    round(macd_hist, 4),
                "trend":        trend,
                "volume_ratio": round(vol_today / vol_avg, 2) if vol_avg > 0 else 1.0,
                "atr":          round(atr_val, 2),
                "bollinger":    boll,
                "news_score":   _calc_news_score(sym),  # 🆕 新闻催化剂分数
                "smc_score":    _calc_smc_score(sym, df),  # 🆕 SMC聪明钱分数
            }
            log_signal(sym, results[sym])

            # 进度提示（每10只输出一次）
            if (i + 1) % 10 == 0:
                logger.info(f"信号进度: {i+1}/{total}")

        except Exception as e:
            log_error("signal_engine", f"{sym}: {e}")

    logger.info(f"信号计算完成: {len(results)}/{total} 只标的")
    return results


# ============================================================
# 🆕 实时信号（使用 FutuRealtimeFeed 获取当前价格）
# ============================================================

def get_realtime_signals(symbols: list[str] = None,
                         use_realtime: bool = True) -> dict:
    """
    计算所有标的的技术信号，使用实时价格覆盖当前价格字段。
    
    与 get_signals() 的区别:
      - 历史指标 (MA50, MA200, RSI, MACD) 仍从 yfinance 日线计算
      - 但 `price` 字段使用 FutuRealtimeFeed 的实时价格（< 1 秒延迟）
      - `rsi`, `macd_hist` 等指标仍基于历史收盘价，不受实时价格影响
      - 新增 `realtime_price` 和 `data_source` 字段标识数据源
    
    参数:
        symbols: 标的列表，默认 ALL_SYMBOLS
        use_realtime: 是否尝试使用实时数据源（默认 True）
    
    返回:
        {symbol: {price, ma50, ma200, rsi, ..., realtime_price, data_source}}
    """
    # 1. 先用 yfinance 计算历史指标
    signals = get_signals(symbols)
    if not signals:
        return signals

    # 2. 如果不使用实时数据源，直接返回
    if not use_realtime or not _REALTIME_AVAILABLE:
        for sym in signals:
            signals[sym]["realtime_price"] = signals[sym]["price"]
            signals[sym]["data_source"] = "yfinance (历史)"
        return signals

    # 3. 尝试获取实时价格
    try:
        feed = get_feed()
        feed.subscribe(list(signals.keys()))
        realtime_prices = feed.get_all_prices()
        source = feed.get_data_source()

        updated_count = 0
        for sym in signals:
            rp = realtime_prices.get(sym)
            if rp is not None and rp > 0:
                # 覆盖价格字段为实时价格
                signals[sym]["realtime_price"] = round(float(rp), 2)
                signals[sym]["data_source"] = source
                # 更新 price 字段为实时价格（用于交易决策）
                signals[sym]["price"] = round(float(rp), 2)
                updated_count += 1
            else:
                signals[sym]["realtime_price"] = signals[sym]["price"]
                signals[sym]["data_source"] = "yfinance (未获取到实时)"

        if updated_count > 0:
            logger.info(f"实时价格更新: {updated_count}/{len(signals)} 只标的 | 数据源: {source}")
        else:
            logger.warning("未获取到任何实时价格，全部使用 yfinance 数据")

    except Exception as e:
        logger.warning(f"实时数据源不可用: {e}，使用 yfinance 历史价格")
        for sym in signals:
            signals[sym]["realtime_price"] = signals[sym]["price"]
            signals[sym]["data_source"] = f"yfinance (实时失败: {e})"

    return signals
