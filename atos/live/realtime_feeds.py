"""
ATOS PRO v2 — 实时数据源
=========================
替代 yfinance 的 15-20 分钟延迟数据。
支持 FutuOpenD WebSocket 推送 + 缓存 + 降级到 yfinance。

核心设计：
  - FutuRealtimeFeed: 连接 FutuOpenD 获取毫秒级实时报价
  - RealtimePriceCache: 线程安全的 TTL 缓存
  - 自动降级: FutuOpenD 不可用时回退到 yfinance（带延迟警告）

使用方法：
    from atos.live.realtime_feeds import get_feed

    feed = get_feed()
    feed.subscribe(["AAPL", "MSFT", "GOOGL"])
    price = feed.get_price("AAPL")       # < 1 秒延迟
    quote = feed.get_quote("AAPL")       # 完整行情 (bid, ask, volume, ...)
    all_prices = feed.get_all_prices()   # 所有缓存价格
"""

import time
import json
import threading
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import math

def safe_float(val, default=0.0) -> float:
    """安全转换 float，防止 NaN 污染数据"""
    try:
        v = float(val)
        if isinstance(v, float) and math.isnan(v):
            return float(default)
        return v
    except (TypeError, ValueError):
        return float(default)


from atos.core.logging import get_logger

logger = get_logger(__name__)

_LAST_FUTU_FAIL_TS = 0.0  # v28k: 失败冷却时间戳（15分钟）


# v28k: OpenQuoteContext 内部在「需要图形验证码/登录过期」时无限重试，
# 阻塞调用线程（Pattern 92 变体）。用线程超时兜底，超时即降级 yfinance。
def open_quote_context_with_timeout(host: str = "127.0.0.1", port: int = 11111,
                                    timeout: float = 10.0):
    """在 worker 线程中构造 OpenQuoteContext，超时返回 None。

    futu-api 的 OpenQuoteContext 构造函数内部有无限重试循环（登录验证码、
    网络错误时每 6 秒重试），主线程直接调用会被永久阻塞。
    ⚠️ 不能用 `with ThreadPoolExecutor` — 其 __exit__ 会 shutdown(wait=True)，
    等待永不结束的 worker，等效于没有超时。
    """
    import concurrent.futures as _cf
    import time as _time

    global _LAST_FUTU_FAIL_TS
    # 失败冷却 15 分钟 — 验证码期间每次调用都会泄漏一个无限重试线程
    if _time.time() - _LAST_FUTU_FAIL_TS < 900:
        return None

    def _build():
        from futu import OpenQuoteContext
        return OpenQuoteContext(host=host, port=port)

    ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(_build)
    try:
        return fut.result(timeout=timeout)
    except Exception as e:
        _LAST_FUTU_FAIL_TS = _time.time()
        logger.warning(f"FutuOpenD 连接超时/失败 ({e}) — 需在 OpenD GUI 手动登录/过验证码")
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # 不等待卡死的 worker

# ============================================================
# 1. RealtimePriceCache — 线程安全 TTL 缓存
# ============================================================

class RealtimePriceCache:
    """
    线程安全的实时价格缓存。
    
    特性:
      - TTL 每条目 5 秒（可通过 ttl 参数调整）
      - 自动清理过期条目
      - 批量更新接口
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls, ttl_seconds: int = 1):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, ttl_seconds: int = 1):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._ttl = timedelta(seconds=ttl_seconds)
        self._store: dict[str, dict] = {}
        self._timestamps: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def update(self, symbol: str, quote: dict) -> None:
        """更新单个标的的最新报价"""
        with self._lock:
            self._store[symbol] = quote
            self._timestamps[symbol] = datetime.now()

    def batch_update(self, quotes: dict[str, dict]) -> None:
        """批量更新报价 {symbol: quote_dict}"""
        now = datetime.now()
        with self._lock:
            for sym, q in quotes.items():
                self._store[sym] = q
                self._timestamps[sym] = now

    def get(self, symbol: str) -> Optional[dict]:
        """获取报价，过期返回 None"""
        with self._lock:
            ts = self._timestamps.get(symbol)
            if ts is None:
                return None
            if datetime.now() - ts > self._ttl:
                # 过期 — 清除
                del self._store[symbol]
                del self._timestamps[symbol]
                return None
            return self._store.get(symbol)

    def get_price(self, symbol: str) -> Optional[float]:
        """获取最新价格，过期返回 None"""
        quote = self.get(symbol)
        if quote is None:
            return None
        # 优先 last_price，其次 price，最后 close
        # 注意：必须显式检查 NaN，因为 float('nan') 在 Python 中是 truthy
        lp = quote.get("last_price")
        if lp is not None and isinstance(lp, (int, float)) and not (isinstance(lp, float) and math.isnan(lp)):
            return float(lp)
        p = quote.get("price")
        if p is not None and isinstance(p, (int, float)) and not (isinstance(p, float) and math.isnan(p)):
            return float(p)
        c = quote.get("close")
        if c is not None and isinstance(c, (int, float)) and not (isinstance(c, float) and math.isnan(c)):
            return float(c)
        return None
    def get_all(self) -> dict[str, dict]:
        """获取所有未过期的报价"""
        now = datetime.now()
        with self._lock:
            result = {}
            expired = []
            for sym, ts in self._timestamps.items():
                if now - ts > self._ttl:
                    expired.append(sym)
                else:
                    result[sym] = self._store.get(sym)
            for sym in expired:
                del self._store[sym]
                del self._timestamps[sym]
            return result

    def get_all_prices(self) -> dict[str, float]:
        """获取所有未过期价格 {symbol: price}"""
        quotes = self.get_all()
        result = {}
        for sym, q in quotes.items():
            p = q.get("last_price") if q.get("last_price") is not None else (q.get("price") if q.get("price") is not None else q.get("close"))
            if p is not None and isinstance(p, (int, float)) and not (isinstance(p, float) and math.isnan(p)):
                result[sym] = float(p)
        return result

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._store.clear()
            self._timestamps.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    @property
    def stats(self) -> dict:
        """缓存统计"""
        with self._lock:
            now = datetime.now()
            ages = {}
            for sym, ts in self._timestamps.items():
                ages[sym] = (now - ts).total_seconds()
            return {
                "entries": len(self._store),
                "avg_age_sec": sum(ages.values()) / len(ages) if ages else 0,
                "max_age_sec": max(ages.values()) if ages else 0,
                "ttl_sec": self._ttl.total_seconds(),
            }


# ============================================================
# 2. FutuRealtimeFeed — FutuOpenD 实时报价推送
# ============================================================

class FutuRealtimeFeed:
    """
    FutuOpenD 实时报价数据源。
    
    通过 WebSocket 连接 FutuOpenD (默认端口 11111)，
    订阅标的的实时推送，缓存最新报价。
    如果 FutuOpenD 不可用，自动降级到 yfinance 轮询。
    
    用法:
        feed = FutuRealtimeFeed()
        feed.subscribe(["AAPL", "MSFT"])
        price = feed.get_price("AAPL")  # < 1 秒延迟
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111,
                 api_version: str = "v2"):
        self.host = host
        self.port = port
        self.api_version = api_version
        self.cache = RealtimePriceCache(ttl_seconds=1)
        self._subscribed: set[str] = set()
        self._connected = False
        self._fallback = False  # True = 使用 yfinance 降级
        self._futu_ctx = None   # OpenQuoteContext 实例
        self._lock = threading.RLock()
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()
        self._ws_handler_set = False
        self._reconnect_attempts = 0   # v5: 重连退避计数
        self._last_reconnect_time = 0  # v5: 上次重连时间
        self._futu_disabled = False    # v28j: 永久禁用标记

        # v28j: 快速 TCP 检查 — 如果端口不通直接禁用 FutuOpenD
        import socket as _sock
        try:
            _s = _sock.create_connection((host, port), timeout=2)
            _s.close()
        except Exception:
            logger.warning(f"FutuOpenD 端口 {host}:{port} 不可达 — 永久禁用，使用 yfinance")
            self._futu_disabled = True
            self._fallback = True
            return

        # 启动时尝试连接
        self._connect()

    # ----- 公开接口 -----

    def subscribe(self, symbols: list[str]) -> bool:
        """
        订阅实时报价。
        
        如果 FutuOpenD 已连接，通过 WebSocket 订阅。
        否则，标记为延迟订阅（yfinance 降级时按需获取）。
        """
        if not symbols:
            return False

        with self._lock:
            new_symbols = [s for s in symbols if s not in self._subscribed]
            if not new_symbols:
                return True
            self._subscribed.update(new_symbols)

        if self._connected and not self._fallback:
            return self._futu_subscribe(new_symbols)
        else:
            logger.info(f"[降级] 标记 {len(new_symbols)} 只标的为延迟订阅 (yfinance 轮询): {new_symbols}")
            return True

    def get_price(self, symbol: str) -> Optional[float]:
        """获取最新价格（< 1 秒延迟）。过期或无效返回 None。"""
        # 1. 检查缓存
        price = self.cache.get_price(symbol)
        if price is not None:
            return float(price)

        # 2. 如果已连接 Futu，尝试立即拉取
        if self._connected and not self._fallback:
            try:
                quote = self._futu_fetch_quote(symbol)
                if quote:
                    p = quote.get("last_price") or quote.get("price")
                    if p:
                        self.cache.update(symbol, quote)
                        return float(p)
            except Exception as e:
                logger.debug(f"Futu 拉取 {symbol} 失败: {e}")

        # 3. 降级到 yfinance
        return self._yfinance_get_price(symbol)

    def get_all_prices(self) -> dict[str, float]:
        """获取所有已订阅标的的最新价格"""
        prices = self.cache.get_all_prices()

        # 对缓存中没有的标的从 yfinance 补全
        with self._lock:
            missing = [s for s in self._subscribed if s not in prices]
        if missing:
            yf_prices = self._yfinance_batch_prices(missing)
            prices.update(yf_prices)

        return prices

    def get_quote(self, symbol: str) -> Optional[dict]:
        """获取完整的实时行情（含 bid, ask, volume, open, high, low 等）"""
        # 1. 尝试缓存
        cached = self.cache.get(symbol)
        if cached is not None:
            return cached

        # 2. Futu 实时拉取
        if self._connected and not self._fallback:
            try:
                quote = self._futu_fetch_quote(symbol)
                if quote:
                    self.cache.update(symbol, quote)
                    return quote
            except Exception as e:
                logger.debug(f"Futu 行情 {symbol} 失败: {e}")

        # 3. 降级 — yfinance 没有 bid/ask，用 close 近似
        return self._yfinance_get_quote(symbol)

    def is_connected(self) -> bool:
        """检查是否连接到 FutuOpenD"""
        return self._connected and not self._fallback

    def is_fallback(self) -> bool:
        """是否在使用 yfinance 降级模式"""
        return self._fallback

    def get_data_source(self) -> str:
        """返回当前数据源描述"""
        if self._connected and not self._fallback:
            return "FutuOpenD (实时)"
        return "yfinance (15-20 分钟延迟) ⚠️"

    def _ensure_connected(self) -> bool:
        """确保 FutuOpenD 连接可用。不可用时自动重连一次。"""
        if self._connected and not self._fallback and self._futu_ctx is not None:
            # Quick health check: try to ping
            try:
                ret, _ = self._futu_ctx.get_global_state()
                if ret == 0:  # RET_OK
                    return True
            except Exception:
                pass
            # Connection dead — mark disconnected
            logger.warning("FutuOpenD 健康检查失败 — 连接已断开")
            self._connected = False

        # Try reconnection once
        if not self._connected or self._fallback:
            import time as _time
            self._reconnect_attempts += 1
            backoff = min(60, 2 * (2 ** min(self._reconnect_attempts, 5)))
            if _time.time() - self._last_reconnect_time < backoff:
                return False  # Still in backoff
            self._last_reconnect_time = _time.time()
            logger.info(f"🔄 尝试重连FutuOpenD (第{self._reconnect_attempts}次)...")
            self._disconnect()
            self._connect()
            if self._connected and not self._fallback:
                self._reconnect_attempts = 0
                # Re-subscribe
                with self._lock:
                    symbols = list(self._subscribed)
                if symbols:
                    self._futu_subscribe(symbols)
                logger.info("✅ FutuOpenD 重连成功!")
                return True
            else:
                logger.warning(f"❌ FutuOpenD 重连失败 — 继续降级到yfinance")
                return False

        return self._connected and not self._fallback

    def reconnect(self) -> bool:
        """手动触发重连"""
        self._reconnect_attempts = 0
        self._disconnect()
        self._connect()
        if self._connected and not self._fallback:
            with self._lock:
                symbols = list(self._subscribed)
            if symbols:
                self._futu_subscribe(symbols)
            return True
        return False

    def shutdown(self):
        """关闭连接，释放资源"""
        self._keepalive_stop.set()
        self._disconnect()

    # ----- 内部 FutuOpenD 连接 -----

    def _connect(self):
        """
        尝试连接 FutuOpenD。
        
        连接流程:
          1. TCP 端口检查 (host:port)
          2. 创建 OpenQuoteContext
          3. 获取市场状态验证连接可用性
          4. 启动 keepalive 线程
        """
        # TCP 检查
        import socket
        try:
            s = socket.create_connection((self.host, self.port), timeout=3)
            s.close()
            logger.info(f"FutuOpenD 端口 {self.host}:{self.port} 可达")
        except Exception as e:
            logger.warning(f"FutuOpenD 不可达: {e} → 降级到 yfinance")
            self._fallback = True
            return

        # 尝试导入 futu-api
        try:
            from futu import OpenQuoteContext, RET_OK, SubType
        except ImportError:
            logger.warning("futu-api 未安装 (pip install futu-api) → 降级到 yfinance")
            self._fallback = True
            return

        # 创建 QuoteContext（v28k: 线程超时兜底，验证码/登录过期时不再无限阻塞）
        try:
            ctx = open_quote_context_with_timeout(host=self.host, port=self.port, timeout=10.0)
            if ctx is None:
                logger.warning("FutuOpenD 连接超时（可能需手动过验证码）→ 降级到 yfinance")
                self._fallback = True
                return
            # 验证连接 — 获取市场状态
            ret, data = ctx.get_global_state()
            if ret != RET_OK:
                logger.warning(f"FutuOpenD 连接验证失败 (ret={ret}) → 降级到 yfinance")
                ctx.close()
                self._fallback = True
                return

            self._futu_ctx = ctx
            self._connected = True
            self._fallback = False
            logger.info("✅ FutuOpenD 实时行情连接成功！延迟 < 1 秒")

            # 设置 WebSocket 推送处理器
            self._setup_push_handler()

            # 启动 keepalive 线程
            self._keepalive_stop.clear()
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                daemon=True,
                name="futu-keepalive",
            )
            self._keepalive_thread.start()

        except Exception as e:
            logger.warning(f"FutuOpenD 连接失败: {e} → 降级到 yfinance")
            if self._futu_ctx:
                try:
                    self._futu_ctx.close()
                except Exception:
                    pass
                self._futu_ctx = None
            self._connected = False
            self._fallback = True

    def _disconnect(self):
        """断开 FutuOpenD 连接"""
        with self._lock:
            if self._futu_ctx:
                try:
                    self._futu_ctx.close()
                except Exception:
                    pass
                self._futu_ctx = None
            self._connected = False

    def _on_quote(self, code: str, data):
        """WebSocket 推送回调 — 报价更新"""
        try:
            # data 包含: code, last_price, open_price, high_price,
            # low_price, volume, turnover, bid_price, ask_price, ...
            symbol = code
            if symbol and data:
                quote = {
                    "symbol": symbol,
                    "last_price": safe_float(data.get("last_price", 0)),
                    "open": safe_float(data.get("open_price", 0)),
                    "high": safe_float(data.get("high_price", 0)),
                    "low": safe_float(data.get("low_price", 0)),
                    "volume": int(data.get("volume", 0)),
                    "turnover": safe_float(data.get("turnover", 0)),
                    "bid_price": safe_float(data.get("bid_price", 0)),
                    "ask_price": safe_float(data.get("ask_price", 0)),
                    "bid_size": int(data.get("bid_size", 0)),
                    "ask_size": int(data.get("ask_size", 0)),
                    "timestamp": time.time(),
                    "source": "futu_websocket",
                }
                self.cache.update(symbol, quote)
                logger.debug(f"[实时] {symbol} = ${quote['last_price']:.2f}")
        except Exception as e:
            logger.debug(f"WebSocket 回调异常: {e}")

    def _setup_push_handler(self):
        """
        设置 WebSocket 推送处理器。
        
        FutuOpenD 通过 set_handler 注册回调函数，
        当订阅标的有新报价时会自动推送。
        """
        if not self._futu_ctx or self._ws_handler_set:
            return

        try:
            from futu import RTDataHandlerBase

            class _QuoteHandler(RTDataHandlerBase):
                def __init__(self, on_quote_cb):
                    super().__init__()
                    self._cb = on_quote_cb
                def on_rt_data(self, code, data):
                    self._cb(code, data)

            # 注册自定义回调处理器
            self._futu_ctx.set_handler(_QuoteHandler(self._on_quote))

            self._ws_handler_set = True
            logger.debug("FutuOpenD WebSocket 推送处理器已注册")

        except Exception as e:
            logger.warning(f"WebSocket 推送处理器注册失败: {e}")
            self._ws_handler_set = False

    def _futu_subscribe(self, symbols: list[str]) -> bool:
        """通过 FutuOpenD API 订阅实时报价（含自动重连）"""
        if not self._futu_ctx or not self._connected:
            if not self._ensure_connected():
                return False

        try:
            from futu import SubType, RET_OK

            # 将符号转换为 Futu 格式: "US.AAPL"
            futu_symbols = [f"US.{s}" if not s.startswith("US.") else s
                            for s in symbols]

            ret, data = self._futu_ctx.subscribe(
                futu_symbols,
                [SubType.QUOTE],  # 订阅报价类型
            )
            if ret == RET_OK:
                logger.info(f"FutuOpenD 订阅成功: {len(futu_symbols)} 只标的: "
                           f"{[s.replace('US.', '') for s in futu_symbols]}")
                return True
            else:
                logger.warning(f"FutuOpenD 订阅失败 (ret={ret}): {data}")
                return False

        except Exception as e:
            logger.warning(f"FutuOpenD 订阅异常: {e}")
            return False

    def _futu_fetch_quote(self, symbol: str) -> Optional[dict]:
        """
        通过 FutuOpenD 拉取单个标的即时报价。
        
        用于 WebSocket 推送不及时或丢失的兜底。
        """
        if not self._futu_ctx or not self._connected:
            return None

        try:
            from futu import RET_OK
            futu_sym = f"US.{symbol}" if not symbol.startswith("US.") else symbol

            ret, data = self._futu_ctx.get_market_snapshot([futu_sym])
            if ret != RET_OK or data is None or data.empty:
                return None

            row = data.iloc[0]
            return {
                "symbol": symbol,
                "last_price": safe_float(row.get("last_price", 0)),
                "open": safe_float(row.get("open_price", 0)),
                "high": safe_float(row.get("high_price", 0)),
                "low": safe_float(row.get("low_price", 0)),
                "volume": int(row.get("volume", 0)),
                "turnover": safe_float(row.get("turnover", 0)),
                "bid_price": safe_float(row.get("bid_price", 0)),
                "ask_price": safe_float(row.get("ask_price", 0)),
                "bid_size": int(row.get("bid_size", 0)),
                "ask_size": int(row.get("ask_size", 0)),
                "timestamp": time.time(),
                "source": "futu_snapshot",
            }

        except Exception as e:
            logger.debug(f"Futu snapshot {symbol} 失败: {e}")
            return None

    def _keepalive_loop(self):
        """
        Keepalive 线程 v5 — 智能退避重连。

        - 周末/闭市时段跳过重连（节省资源）
        - 重连失败后指数退避（5s→10s→20s→...最長5分钟）
        - 避免频繁创建 OpenQuoteContext 打爆 FutuOpenD 连接数
        """
        import time as _time
        logger.debug("FutuOpenD keepalive 线程启动 (v5 智能退避)")
        while not self._keepalive_stop.is_set():
            try:
                # 每 30 秒检查一次
                if self._keepalive_stop.wait(30):
                    break

                if not self._connected or self._futu_ctx is None:
                    # v5: 周末跳过重连
                    from datetime import datetime, timezone
                    now_utc = datetime.now(timezone.utc)
                    if now_utc.weekday() >= 5:  # 周六日
                        continue

                    # v5: 指数退避
                    self._reconnect_attempts += 1
                    backoff = min(300, 5 * (2 ** min(self._reconnect_attempts, 6)))
                    if _time.time() - self._last_reconnect_time < backoff:
                        continue  # 还没到退避时间

                    self._last_reconnect_time = _time.time()
                    logger.warning(f"FutuOpenD 连接断开，尝试重连... (尝试#{self._reconnect_attempts}, 退避{backoff}s)")
                    self._disconnect()
                    self._connect()
                    if self._connected and not self._fallback:
                        self._reconnect_attempts = 0  # 重置计数
                        with self._lock:
                            symbols = list(self._subscribed)
                        if symbols:
                            self._futu_subscribe(symbols)

            except Exception as e:
                logger.error(f"Keepalive 异常: {e}")

        logger.debug("FutuOpenD keepalive 线程结束")

    # ----- yfinance 降级实现 -----

    def _yfinance_get_price(self, symbol: str) -> Optional[float]:
        """通过 yfinance 获取最新价格（15-20 分钟延迟）"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            # 尝试 fast_info (更快)
            if hasattr(ticker, "fast_info"):
                try:
                    price = getattr(ticker.fast_info, 'lastPrice', None) or getattr(ticker.fast_info, 'regularMarketPrice', None) or getattr(ticker.fast_info, 'previousClose', None)
                    if price:
                        safe_p = safe_float(price)
                        if safe_p > 0:
                            self.cache.update(symbol, {
                                "last_price": safe_p,
                                "source": "yfinance",
                                "timestamp": time.time(),
                            })
                            return safe_p
                except Exception:
                    pass

            # 兜底 — 下载日线取最新收盘
            df = yf.download(symbol, period="2d", interval="1d",
                           progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            if not df.empty:
                price = float(df["Close"].iloc[-1])
                self.cache.update(symbol, {
                    "last_price": price,
                    "source": "yfinance",
                    "timestamp": time.time(),
                })
                return price

            return None
        except Exception as e:
            logger.debug(f"yfinance {symbol} 失败: {e}")
            return None

    def _yfinance_batch_prices(self, symbols: list[str]) -> dict[str, float]:
        """批量获取 yfinance 价格"""
        result = {}
        try:
            import yfinance as yf
            for sym in symbols:
                p = self._yfinance_get_price(sym)
                if p is not None:
                    result[sym] = p
        except Exception as e:
            logger.debug(f"yfinance 批量拉取失败: {e}")
        return result

    def _yfinance_get_quote(self, symbol: str) -> Optional[dict]:
        """通过 yfinance 获取近似行情（没有 bid/ask）"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.info if hasattr(ticker, "info") else {}

            price = info.get("currentPrice") or info.get("regularMarketPrice") or \
                    info.get("previousClose") or 0
            quote = {
                "symbol": symbol,
                "last_price": float(price),
                "open": float(info.get("regularMarketOpen", 0)),
                "high": float(info.get("regularMarketDayHigh", 0)),
                "low": float(info.get("regularMarketDayLow", 0)),
                "volume": int(info.get("regularMarketVolume", 0)),
                "bid_price": float(info.get("bid", 0)),
                "ask_price": float(info.get("ask", 0)),
                "bid_size": int(info.get("bidSize", 0)),
                "ask_size": int(info.get("askSize", 0)),
                "source": "yfinance",
                "timestamp": time.time(),
                "warning": "yfinance 数据延迟 15-20 分钟",
            }
            self.cache.update(symbol, quote)
            return quote
        except Exception as e:
            logger.debug(f"yfinance quote {symbol} 失败: {e}")
            return None


# ============================================================
# 3. 全局单例访问
# ============================================================

_feed_instance: Optional[FutuRealtimeFeed] = None
_feed_lock = threading.Lock()


def get_feed() -> FutuRealtimeFeed:
    """
    获取全局 FutuRealtimeFeed 单例。
    
    首次调用时自动尝试连接 FutuOpenD。
    """
    global _feed_instance
    if _feed_instance is None:
        with _feed_lock:
            if _feed_instance is None:
                _feed_instance = FutuRealtimeFeed()
    return _feed_instance


def reset_feed():
    """
    重置全局 feed（重连用）。
    
    调用后下次 get_feed() 会重新创建连接。
    """
    global _feed_instance
    with _feed_lock:
        if _feed_instance:
            _feed_instance.shutdown()
            _feed_instance = None


# ============================================================
# 4. 快捷函数
# ============================================================

def get_real_price(symbol: str) -> Optional[float]:
    """快捷获取单个标的实时价格"""
    return get_feed().get_price(symbol)


def get_real_prices(symbols: list[str]) -> dict[str, float]:
    """
    快捷批量获取实时价格。
    
    自动订阅未订阅的标的。
    """
    feed = get_feed()
    feed.subscribe(symbols)
    return feed.get_all_prices()


# ============================================================
# 5. 单元测试 / 自检
# ============================================================

def self_test():
    """
    自检：验证模块可正常加载和连接。
    
    返回诊断信息字典。
    """
    import sys
    import yfinance as yf

    result = {
        "module": "realtime_feeds",
        "status": "OK",
        "python_version": sys.version,
        "yfinance_available": True,
        "futu_api_available": False,
        "futu_connected": False,
        "data_source": "",
        "cache_stats": {},
        "errors": [],
    }

    # 检查 futu-api
    try:
        import futu
        result["futu_api_available"] = True
    except ImportError:
        pass

    # 检查 yfinance
    try:
        _ = yf.download("AAPL", period="1d", interval="1d", progress=False, auto_adjust=True)
    except Exception as e:
        result["yfinance_available"] = False
        result["errors"].append(f"yfinance 测试失败: {e}")

    # 尝试连接
    try:
        feed = get_feed()
        result["futu_connected"] = feed.is_connected()
        result["data_source"] = feed.get_data_source()
        result["cache_stats"] = feed.cache.stats
    except Exception as e:
        result["errors"].append(f"连接测试失败: {e}")

    return result


if __name__ == "__main__":
    import json
    diag = self_test()
    print(json.dumps(diag, indent=2, ensure_ascii=False))
    print(f"\n数据源: {diag['data_source']}")
    print(f"yfinance: {'✅' if diag['yfinance_available'] else '❌'} | "
          f"futu-api: {'✅' if diag['futu_api_available'] else '❌'} | "
          f"FutuOpenD: {'✅' if diag['futu_connected'] else '❌'}")
