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

# Bug #10: yfinance 缓存层 — 5 分钟内不重复下载
_cache = {}  # {symbol: (timestamp, dataframe)}
_CACHE_TTL = timedelta(minutes=5)

def _get_cached_data(symbol: str, period: str = "1y", interval: str = "1d"):
    """带缓存的 yfinance 下载，同一标的 5 分钟内只下载一次"""
    key = f"{symbol}:{period}:{interval}"
    now = datetime.now()  # naïvedatetime — 仅用于缓存TTL比较，不涉及时区转换，安全无歧义
    if key in _cache:
        ts, df = _cache[key]
        if now - ts < _CACHE_TTL:
            return df
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    _cache[key] = (datetime.now(), df)
    return df

def clear_cache():
    """强制清空缓存（手动更新用）"""
    _cache.clear()

def _is_edt() -> bool:
    """Check if US Eastern time is currently in EDT (Daylight Saving).
    Uses system timezone database via time.daylight flag."""
    # Save current TZ, set to US Eastern, check DST
    old_tz = os.environ.get('TZ', '')
    os.environ['TZ'] = 'America/New_York'
    try:
        time.tzset()
        return time.daylight != 0
    finally:
        if old_tz:
            os.environ['TZ'] = old_tz
        else:
            os.environ.pop('TZ', None)
        time.tzset()


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
            ma50 = _scalar(close.rolling(50).mean())
            ma200 = _scalar(close.rolling(200).mean()) if len(df) >= 200 else ma50
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

            results[sym] = {
                "price":        round(price, 2),
                "ma50":         round(ma50, 2),
                "ma200":        round(ma200, 2),
                "rsi":          round(rsi_val, 1),
                "macd_hist":    round(macd_hist, 4),
                "trend":        trend,
                "volume_ratio": round(vol_today / vol_avg, 2) if vol_avg > 0 else 1.0,
                "atr":          round(atr_val, 2),
                "bollinger":    boll,
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
