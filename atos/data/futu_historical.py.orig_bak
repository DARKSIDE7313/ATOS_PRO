"""
ATOS Data — Futu历史数据替代yfinance（中国大陆优化）
=====================================================
在中国大陆，yfinance经常被墙/限流。FutuOpenD本地连接正常，
所以用Futu的历史K线API替代yfinance获取日线数据。

用法:
  from atos.data.futu_historical import get_history, get_batch_history
  df = get_history("AAPL", days=252)  # 获取最近252个交易日

缓存: 本地SQLite缓存，同一标的5分钟内不重复请求
"""

import os, time, threading, json, sqlite3
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("data.futu_historical")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
CACHE_DB = os.path.join(BASE_DIR, "data", "futu_hist_cache.db")

_FUTU_AVAILABLE = False
_quote_ctx = None
_FUTU_FAIL_TS = 0.0
_lock = threading.Lock()


def _init_futu():
    """初始化Futu连接（线程安全，只连一次 + 失败冷却）"""
    global _FUTU_AVAILABLE, _quote_ctx, _FUTU_FAIL_TS
    if _FUTU_AVAILABLE and _quote_ctx is not None:
        return True
    # v28k: 验证码/登录过期时每周期重试只会拖慢周期(10s×N)并泄漏线程 → 15分钟冷却
    if time.time() - _FUTU_FAIL_TS < 900:
        return False

    with _lock:
        if _FUTU_AVAILABLE and _quote_ctx is not None:
            return True
        try:
            from futu import OpenQuoteContext, RET_OK, KLType, AuType
            from atos.live.realtime_feeds import open_quote_context_with_timeout
            _quote_ctx = open_quote_context_with_timeout(host='127.0.0.1', port=11111, timeout=10.0)
            if _quote_ctx is None:
                logger.warning("Futu初始化超时（可能需手动过验证码）→ 回退 yfinance")
                return False
            ret, data = _quote_ctx.get_market_state(['US.SPY'])
            if ret == RET_OK:
                _FUTU_AVAILABLE = True
                logger.info("✅ Futu历史数据源连接成功")
                return True
            else:
                logger.warning(f"Futu连接失败: {data}")
                _quote_ctx.close()
                _quote_ctx = None
                return False
        except Exception as e:
            logger.warning(f"Futu初始化失败: {e}")
            _FUTU_AVAILABLE = False
            _FUTU_FAIL_TS = time.time()
            return False


def _init_cache_db():
    """初始化本地缓存数据库"""
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hist_cache (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            fetched_at REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_date ON hist_cache(symbol, date)")
    conn.commit()
    return conn


def _cache_get(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """从缓存读取历史数据"""
    conn = sqlite3.connect(CACHE_DB)
    cutoff = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    try:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM hist_cache "
            "WHERE symbol = ? AND date >= ? ORDER BY date",
            conn, params=(symbol, cutoff)
        )
        if len(df) >= min(days, 50):
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
            # Check freshness: most recent entry within 1 day
            newest_fetch = pd.read_sql_query(
                "SELECT MAX(fetched_at) FROM hist_cache WHERE symbol = ?",
                conn, params=(symbol,)
            ).iloc[0, 0]
            if newest_fetch and (time.time() - newest_fetch) < 86400:
                conn.close()
                return df
        conn.close()
        return None
    except Exception:
        conn.close()
        return None


def _cache_set(symbol: str, df: pd.DataFrame):
    """写入缓存"""
    conn = sqlite3.connect(CACHE_DB)
    now = time.time()
    try:
        for idx, row in df.iterrows():
            date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, 'strftime') else str(idx)[:10]
            conn.execute("""
                INSERT OR REPLACE INTO hist_cache (symbol, date, open, high, low, close, volume, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, date_str,
                  float(row.get('Open', row.get('open', 0))),
                  float(row.get('High', row.get('high', 0))),
                  float(row.get('Low', row.get('low', 0))),
                  float(row.get('Close', row.get('close', 0))),
                  float(row.get('Volume', row.get('volume', 0))),
                  now))
        conn.commit()
    except Exception as e:
        logger.debug(f"缓存写入失败: {e}")
    finally:
        conn.close()


def get_history(symbol: str, days: int = 252, use_cache: bool = True) -> pd.DataFrame:
    """
    获取股票历史日线数据（Futu优先，缓存加速）。

    Args:
        symbol: 美股代码 (如 AAPL, SPY)
        days: 需要多少天的数据（默认252，约1年）
        use_cache: 是否使用本地缓存

    Returns:
        DataFrame with columns: Open, High, Low, Close, Volume, indexed by date
    """
    # 1. Try cache first
    if use_cache:
        cached = _cache_get(symbol, days)
        if cached is not None and len(cached) >= 20:
            return cached[-days:]

    # 2. Try Futu
    if _init_futu():
        try:
            from futu import KLType, AuType, RET_OK
            # US stocks need "US." prefix
            futu_symbol = f"US.{symbol}" if not symbol.startswith("US.") else symbol

            ret, data, _ = _quote_ctx.request_history_kline(
                futu_symbol, start=None, end=None,
                ktype=KLType.K_DAY, autype=AuType.QFQ,
                max_count=min(days + 20, 500)
            )

            if ret == RET_OK and not data.empty:
                df = data[['time_key', 'open', 'high', 'low', 'close', 'volume']].copy()
                df.columns = ['date', 'Open', 'High', 'Low', 'Close', 'Volume']
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
                df = df.sort_index()

                # Cache it
                if use_cache:
                    _cache_set(symbol, df)

                logger.debug(f"Futu: {symbol} {len(df)}条日线 OK")
                return df[-days:]

        except Exception as e:
            logger.debug(f"Futu历史数据 {symbol}: {e}")

    # 3. Fallback to yfinance (last resort)
    try:
        import yfinance as yf
        df = yf.download(symbol, period="1y", interval="1d",
                        progress=False, auto_adjust=True, timeout=15)
        if not df.empty:
            logger.debug(f"yfinance fallback: {symbol} {len(df)}条 OK")
            if use_cache and len(df) >= 10:
                _cache_set(symbol, df)
            return df
    except Exception as e:
        logger.debug(f"yfinance fallback {symbol}: {e}")

    # 4. All failed
    logger.warning(f"❌ {symbol}: 所有数据源失败")
    return pd.DataFrame()


def get_batch_history(symbols: list, days: int = 252) -> dict:
    """
    批量获取历史数据（逐个请求，缓存优化）。

    Returns:
        {symbol: DataFrame}
    """
    results = {}
    for sym in symbols:
        try:
            df = get_history(sym, days)
            if not df.empty and len(df) >= 20:
                results[sym] = df
        except Exception:
            pass
    return results


def get_spy_vix_data() -> tuple:
    """获取SPY和VIX数据（市场体制判断用）"""
    spy = get_history("SPY", days=252)
    vix = get_history("VIX", days=252)

    # VIX symbol in Futu: US..VIX or US.VIX
    # If VIX failed, try alternative symbols
    if vix.empty:
        for alt in ["VIX", "VIXM", "VXX"]:
            vix = get_history(alt, days=252)
            if not vix.empty:
                break

    # If VIX still empty, try yfinance with short timeout
    if vix.empty:
        try:
            import yfinance as yf
            vix = yf.download("^VIX", period="1y", interval="1d",
                            progress=False, auto_adjust=True, timeout=8)
        except Exception:
            pass

    # v19: Final fallback — use cached VIX or hardcoded default
    # VIX has been in 12-20 range for most of 2026, default 16 is reasonable
    if vix.empty:
        # Try loading from cache file
        import json
        vix_cache_file = os.path.join(BASE_DIR, "data", "vix_cache.json")
        try:
            if os.path.exists(vix_cache_file):
                with open(vix_cache_file) as f:
                    cached = json.load(f)
                import pandas as pd
                vix = pd.DataFrame({"Close": [cached.get("vix", 16.0)]})
                logger.info(f"📦 VIX使用缓存值: {cached.get('vix', 16.0)}")
        except Exception:
            pass

    # Save VIX to cache for next time
    if not vix.empty:
        try:
            import json
            vix_val = float(vix["Close"].squeeze().iloc[-1]) if len(vix) > 0 else 16.0
            vix_cache_file = os.path.join(BASE_DIR, "data", "vix_cache.json")
            with open(vix_cache_file, "w") as f:
                json.dump({"vix": vix_val, "ts": str(datetime.now())}, f)
        except Exception:
            pass

    return spy, vix


def clear_cache():
    """清除所有缓存"""
    try:
        if os.path.exists(CACHE_DB):
            os.remove(CACHE_DB)
            logger.info("🗑️ Futu历史缓存已清除")
    except Exception as e:
        logger.warning(f"缓存清除失败: {e}")
