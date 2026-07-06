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
import math
import threading
import socket
import pandas as pd
import yfinance as yf
from functools import lru_cache
from datetime import datetime, timedelta
from atos.core.universe import ALL_SYMBOLS, LONG_TERM_SYMBOLS, SHORT_TERM_SYMBOLS
from atos.core.logging import get_logger, log_signal, log_error

# 🆕 全局 socket 超时 — 防止 yfinance HTTP 请求永久卡死
socket.setdefaulttimeout(30)

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

# v11: 信号缓存 — 防止 yfinance/Futu 全部宕机时系统完全停摆
# 文件持久化, 重启也能恢复
import os as _os
_SIGNAL_CACHE_FILE = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "data", "signal_cache.json")
_signal_cache: dict = {}       # 上周期完整信号
_signal_cache_ts = None        # 缓存时间
_SIGNAL_CACHE_TTL = 30 * 60    # 30分钟有效期
_MAX_CACHE_CYCLES = 5          # 最多用缓存跑5个周期

def _load_signal_cache():
    global _signal_cache, _signal_cache_ts
    try:
        if _os.path.exists(_SIGNAL_CACHE_FILE):
            with open(_SIGNAL_CACHE_FILE) as f:
                data = json.load(f)
            _signal_cache = data.get("signals", {})
            ts = data.get("timestamp")
            if ts:
                _signal_cache_ts = datetime.fromisoformat(ts)
            if _signal_cache:
                logger.info(f"📦 信号缓存恢复: {len(_signal_cache)} 只标的")
    except Exception:
        pass

def _save_signal_cache():
    try:
        _os.makedirs(_os.path.dirname(_SIGNAL_CACHE_FILE), exist_ok=True)
        with open(_SIGNAL_CACHE_FILE, "w") as f:
            json.dump({"signals": _signal_cache, "timestamp": datetime.now().isoformat()}, f)
    except Exception:
        pass

# 模块加载时恢复缓存
_load_signal_cache()

# 🔒 yfinance 全局线程锁 — 防止多线程并发写损坏 SQLite 缓存
# 2026-06-23 深度审计修复：yfinance SQLite 在多线程下频繁 disk I/O error
_yf_lock = threading.Lock()

# 自愈: yfinance SQLite 缓存修复
def _repair_yfinance_cache():
    """清除 yfinance SQLite 缓存的 WAL/SHM 残留文件。

    仅清除 1 小时以上的残留（异常退出后留下的）。正常的 WAL 文件
    是 SQLite WAL 模式的正常工作文件，不应该被删除。"""
    import glob
    cache_dir = os.path.expanduser('~/Library/Caches/py-yfinance')
    os.makedirs(cache_dir, exist_ok=True)

    now = time.time()
    cleaned = 0
    for pattern in ('*.db-wal', '*.db-shm'):
        for f in glob.glob(os.path.join(cache_dir, pattern)):
            try:
                mtime = os.path.getmtime(f)
                # 只清除 1 小时以上的残留（当前运行不会被误删）
                if now - mtime > 3600:
                    os.remove(f)
                    cleaned += 1
                    logger.debug(f"自愈: 已清除 yfinance 缓存残留 {os.path.basename(f)} (age={now-mtime:.0f}s)")
            except OSError:
                pass
    if cleaned:
        logger.info(f"自愈: 共清除 {cleaned} 个过期 WAL/SHM 残留文件")

# 启动时修复一次
_repair_yfinance_cache()
_CACHE_TTL = timedelta(minutes=15)  # 15分钟缓存 — 批量下载后单只不再重试下载
_batch_success = False  # 批量下载成功标记 — 跳过单只重试

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
    """带缓存的 yfinance 下载，同一标的 15 分钟内只下载一次。
    批量下载成功后跳过单只重试。"""
    global _batch_success
    key = f"{symbol}:{period}:{interval}"
    now = datetime.now()
    if key in _cache:
        ts, df = _cache[key]
        if now - ts < _CACHE_TTL:
            return df

    # 批量下载成功 → 单只下载没必要，直接用实时数据兜底
    if _batch_success:
        logger.debug(f"{symbol} 批量缓存未命中，使用空数据兜底（Futu实时价格可用）")
        _cache[key] = (datetime.now(), pd.DataFrame())
        return pd.DataFrame()

    # 多轮重试
    max_attempts = 2
    last_error = None
    for attempt in range(max_attempts):
        try:
            with _yf_lock:
                try:
                    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
                except Exception:
                    ticker = yf.Ticker(symbol)
                    df = ticker.history(period=period, interval=interval, auto_adjust=True)
            if df is not None and not df.empty:
                _cache[key] = (datetime.now(), df)
                return df
            last_error = f"empty dataframe (attempt {attempt+1})"
        except Exception as e:
            last_error = str(e)
        if attempt < max_attempts - 1:
            time.sleep(1.0 * (attempt + 1))

    logger.debug(f"{symbol}: yfinance单只下载失败 — {last_error}")
    _cache[key] = (datetime.now(), pd.DataFrame())
    return pd.DataFrame()


# v11: 批量下载熔断 — 如果>50%失败, 标记周末/宕机, 跳过后续单只下载
_batch_fail_count = 0
_batch_circuit_open = False

def _prefetch_batch(symbols: list[str]) -> None:
    """🚀 批量预下载所有股票数据到缓存（一次性调用，快 10-60 倍）
    v11: 如果批量下载全部失败 → 标记熔断, 不浪费时间去逐个下载"""
    global _batch_fail_count, _batch_circuit_open

    need_fetch = []
    now = datetime.now()
    for sym in symbols:
        key = f"{sym}:1y:1d"
        if key in _cache:
            ts, _ = _cache[key]
            if now - ts < _CACHE_TTL:
                continue
        need_fetch.append(sym)

    if len(need_fetch) < 5:
        return  # 太少不值得批量

    # v11: 熔断检查
    if _batch_circuit_open:
        logger.warning(f"🔌 批量下载已熔断 — 跳过 {len(need_fetch)} 只标的下载")
        return

    try:
        ticker_str = " ".join(need_fetch)
        logger.info(f"🚀 批量下载 {len(need_fetch)} 只股票...")
        with _yf_lock:  # 🔒 串行化 yfinance 下载，防止 SQLite 并发损坏
            df_all = yf.download(ticker_str, period="1y", interval="1d",
                                progress=False, auto_adjust=True, group_by="ticker")
        for sym in need_fetch:
            try:
                if isinstance(df_all.columns, pd.MultiIndex):
                    if sym in df_all.columns.levels[0]:
                        df_sym = df_all[sym].copy()
                    else:
                        continue
                else:
                    continue
                if not df_sym.empty and len(df_sym) >= 10:
                    key = f"{sym}:1y:1d"
                    _cache[key] = (now, df_sym)
            except Exception:
                pass
        # v11: 检查批量下载成功率
        fetched = sum(1 for sym in need_fetch if f"{sym}:1y:1d" in _cache)
        success_rate = fetched / len(need_fetch) if need_fetch else 0
        if success_rate < 0.5 and len(need_fetch) > 20:
            _batch_fail_count += 1
            if _batch_fail_count >= 2:
                _batch_circuit_open = True
                logger.critical(f"🔌 批量下载连续失败 → 熔断! (成功率={success_rate:.0%})")
        else:
            _batch_fail_count = 0
            _batch_circuit_open = False
        logger.info(f"✅ 批量预下载完成 ({fetched}/{len(need_fetch)} 成功)")
        global _batch_success
        _batch_success = fetched >= len(need_fetch) * 0.8  # 80%以上算成功
    except Exception as e:
        _batch_fail_count += 1
        if _batch_fail_count >= 2:
            _batch_circuit_open = True
            logger.critical(f"🔌 批量下载异常 → 熔断! ({e})")
        logger.warning(f"批量下载失败（将逐个下载）: {e}")

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

    # 🚀 批量预下载（只发一次网络请求）
    _prefetch_batch(symbols)

    results = {}
    total = len(symbols)

    for i, sym in enumerate(symbols):
        try:
            df = _get_cached_data(sym, period="1y", interval="1d")
            # 基础 NaN 防御: 前向填充 + 后向填充，清除所有 NaN 污染
            df = df.ffill().bfill()
            # 检查是否所有列都是 NaN（数据完全不可用）
            if df.empty or df.isna().all(axis=None):
                logger.warning(f"{sym} 数据完全不可用，跳过")
                continue
            if df.empty or len(df) < 50:
                logger.debug(f"跳过 {sym}: 数据不足 ({len(df)}行)")
                continue

            # 处理 MultiIndex columns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            close = df["Close"].squeeze()
            vol = df["Volume"].squeeze()
            price = _scalar(close)
            # NaN 检查: 用 math.isnan() 替代脆弱字符串检测
            if price is None or (isinstance(price, float) and math.isnan(price)):
                logger.debug(f"{sym} price 是 NaN，用 .iloc[-1] 兜底")
                price = _scalar(close.ffill().iloc[-1]) if not close.empty and len(close) > 0 else 0.0
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
            safe_price = price if not (isinstance(price, float) and math.isnan(price)) else 0
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

    skipped = total - len(results)
    if skipped > 0:
        logger.warning(f"信号计算完成: {len(results)}/{total} 只标的 ({skipped}只跳过, yfinance数据不可用)")
    else:
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

    # v11: 如果信号为空，使用上周期缓存（防止 yfinance 全部超时时系统停摆）
    global _signal_cache, _signal_cache_ts
    if not signals and _signal_cache:
        age = (datetime.now() - _signal_cache_ts).total_seconds() if _signal_cache_ts else 999
        cache_cycles = getattr(get_realtime_signals, '_cache_cycles_used', 0)
        if age < _SIGNAL_CACHE_TTL and cache_cycles < _MAX_CACHE_CYCLES:
            logger.warning(f"⚠️ 信号引擎返回空 → 使用缓存 (已{cache_cycles+1}次, 缓存{age:.0f}s前)")
            signals = dict(_signal_cache)  # 浅拷贝
            for sym in signals:
                signals[sym]["data_source"] = "CACHED (yfinance降级)"
                signals[sym]["realtime_price"] = signals[sym].get("price", 0)
            get_realtime_signals._cache_cycles_used = cache_cycles + 1
            return signals
        elif cache_cycles >= _MAX_CACHE_CYCLES:
            logger.error(f"❌ 信号缓存已用{cache_cycles}次(上限{_MAX_CACHE_CYCLES}) → 放弃, 等数据恢复")
            return {}

    if not signals:
        return signals

    # v11: 更新缓存 (内存+文件)
    _signal_cache = dict(signals)
    _signal_cache_ts = datetime.now()
    get_realtime_signals._cache_cycles_used = 0
    _save_signal_cache()  # 持久化到磁盘

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
