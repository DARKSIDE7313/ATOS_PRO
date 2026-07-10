"""
ATOS PRO v3 — Futu OpenD 数据提供层
======================================
替换 yfinance 作为主数据源。Futu OpenD 提供：

  ✅ 实时行情 (last_price, open, high, low, volume)
  ✅ 基本面快照 (PE, PB, EPS, 股息率, 总市值, 流通市值)
  ✅ 历史K线 (日线/周线/月线, 最多1000根)
  ✅ 股票基本信息 (行业, 上市日期)

  ⚠️ 深度基本面 (ROE/负债率/毛利率/FCF) — Futu 不直接提供，用 yfinance 作为补充

用法:
  from atos.data.futu_provider import FutuProvider
  fp = FutuProvider()

  # 实时报价
  quote = fp.get_quote("AAPL")     # → {price, pe, pb, div_yield, market_cap, ...}

  # 批量快照
  snapshots = fp.get_snapshots(["AAPL","MSFT","NVDA"])

  # K线
  kline = fp.get_kline("AAPL", days=200)

  # 基本面（深度）
  fundamentals = fp.get_fundamentals("AAPL")  # PE/PB/EPS/股息 + yfinance ROE/负债
"""

import datetime
import time
from atos.core.logging import get_logger

logger = get_logger("data.futu")

# Futu snapshot fields we care about
_SNAPSHOT_FIELDS = [
    "last_price", "open_price", "high_price", "low_price", "prev_close_price",
    "volume", "turnover",
    "pe_ratio", "pe_ttm_ratio", "pb_ratio",
    "earning_per_share", "net_asset_per_share",
    "dividend_ttm", "dividend_ratio_ttm", "dividend_lfy", "dividend_lfy_ratio",
    "total_market_val", "circular_market_val",
    "suspension", "stock_owner",
]


class FutuProvider:
    """
    Futu OpenD 数据提供层。

    所有行情和K线数据从 Futu 获取。
    深度基本面（ROE/负债/毛利率/FCF）用 yfinance 作为补充。
    内置限流保护（每秒最多5次API调用，兼容 Futu 免费版限制）。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self.host = host
        self.port = port
        self._quote_ctx = None
        self._call_times = []
        self._cache = {}
        self._cache_ttl = datetime.timedelta(minutes=15)       # 快照缓存15分钟
        self._fundamental_cache = {}
        self._fundamental_cache_ttl = datetime.timedelta(hours=1)  # 基本面缓存1小时

    # ═══════════════════════════════════════════
    # 连接管理
    # ═══════════════════════════════════════════

    def _get_quote_ctx(self):
        """延迟创建连接（复用）"""
        if self._quote_ctx is None:
            from futu import OpenQuoteContext
            self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
            logger.info(f"Futu连接: {self.host}:{self.port}")
        return self._quote_ctx

    def close(self):
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None

    # ═══════════════════════════════════════════
    # 限流
    # ═══════════════════════════════════════════

    def _rate_limit(self):
        """每秒最多5次调用，超出则等待"""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 1.0]
        if len(self._call_times) >= 5:
            time.sleep(1.0 - (now - self._call_times[0]) + 0.05)
        self._call_times.append(time.time())

    # ═══════════════════════════════════════════
    # 缓存
    # ═══════════════════════════════════════════

    def _cache_key(self, *parts) -> str:
        return ":".join(str(p) for p in parts)

    def _cache_get(self, key: str):
        if key in self._cache:
            ts, val = self._cache[key]
            if datetime.datetime.now() - ts < self._cache_ttl:
                return val
        return None

    def _cache_set(self, key: str, val):
        self._cache[key] = (datetime.datetime.now(), val)

    # ═══════════════════════════════════════════
    # 1. 单只快照 → 标准报价
    # ═══════════════════════════════════════════

    def get_quote(self, ticker: str) -> dict:
        """
        获取单只股票实时报价 + 基本面快照。

        返回:
          {symbol, price, pe, pb, eps, div_yield, div_payout,
           market_cap, volume, open, high, low, prev_close,
           change_pct, valid: bool}
        """
        snap = self.get_snapshots([ticker])
        if snap and ticker in snap:
            return snap[ticker]
        return {"symbol": ticker, "valid": False, "price": 0}

    def get_snapshots(self, tickers: list[str]) -> dict[str, dict]:
        """
        批量获取快照。

        返回: {ticker: {symbol, price, pe, pb, ...}}
        """
        result = {}
        uncached = []

        # 先查缓存
        for t in tickers:
            cached = self._cache_get(f"snap:{t}")
            if cached:
                result[t] = cached
            else:
                uncached.append(t)

        if not uncached:
            return result

        # 分批获取（Futu 限制每批300只，我们保守50只）
        batch_size = 50
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i:i + batch_size]
            codes = [f"US.{t}" if not t.startswith("US.") and not t.startswith("HK.") else t
                     for t in batch]

            try:
                self._rate_limit()
                from futu import RET_OK
                ctx = self._get_quote_ctx()
                ret, data = ctx.get_market_snapshot(codes)

                if ret != RET_OK or data.empty:
                    for t in batch:
                        result[t] = {"symbol": t, "valid": False, "error": str(data) if ret != RET_OK else "no_data"}
                    continue

                for _, row in data.iterrows():
                    code = row.get("code", "")
                    ticker = code.replace("US.", "").replace("HK.", "")
                    parsed = self._parse_snapshot_row(ticker, row)
                    result[ticker] = parsed
                    self._cache_set(f"snap:{ticker}", parsed)

                # 未返回的标为无效
                got = {r["symbol"] for r in result.values() if r.get("valid")}
                for t in batch:
                    if t not in got and t not in result:
                        result[t] = {"symbol": t, "valid": False, "error": "no_response"}

            except ImportError:
                logger.error("futu-api 未安装")
                for t in batch:
                    result[t] = {"symbol": t, "valid": False, "error": "futu_api_not_installed"}
            except Exception as e:
                logger.error(f"快照获取失败 [{batch[0]}..{batch[-1]}]: {e}")
                for t in batch:
                    result[t] = {"symbol": t, "valid": False, "error": str(e)}

        return result

    def _parse_snapshot_row(self, ticker: str, row) -> dict:
        """解析 Futu 快照行 → 标准报价字典"""
        try:
            price = float(row.get("last_price", 0) or 0)
            prev = float(row.get("prev_close_price", 0) or 0)
            change = (price - prev) / prev if prev > 0 else 0

            pe = float(row.get("pe_ratio", 0) or row.get("pe_ttm_ratio", 0) or 0)
            pb = float(row.get("pb_ratio", 0) or 0)
            div_yield = float(row.get("dividend_ratio_ttm", 0) or row.get("dividend_lfy_ratio", 0) or 0)
            div_ttm = float(row.get("dividend_ttm", 0) or row.get("dividend_lfy", 0) or 0)
            eps = float(row.get("earning_per_share", 0) or 0)
            book = float(row.get("net_asset_per_share", 0) or 0)
            market_cap = float(row.get("total_market_val", 0) or 0)
            volume = float(row.get("volume", 0) or 0)

            # 股息率 Futu 给的是百分比数值（0.35 表示 0.35%），转为小数
            if div_yield > 0.1:      # >10% 不可能是股息率，说明是百分比格式
                div_yield = div_yield / 100
            elif 0 < div_yield <= 0.1:  # 0.35 → 0.0035
                div_yield = div_yield / 100

            # 推断派息率
            div_payout = 0.0
            if eps > 0 and div_ttm > 0:
                div_payout = div_ttm / eps

            return {
                "symbol": ticker,
                "valid": True,
                "price": round(price, 2),
                "open": round(float(row.get("open_price", 0) or 0), 2),
                "high": round(float(row.get("high_price", 0) or 0), 2),
                "low": round(float(row.get("low_price", 0) or 0), 2),
                "prev_close": round(prev, 2),
                "change_pct": round(change * 100, 2),
                "volume": int(volume),
                "pe": round(pe, 2) if pe > 0 else None,
                "pb": round(pb, 2) if pb > 0 else None,
                "eps": round(eps, 4) if eps > 0 else None,
                "book_per_share": round(book, 4) if book > 0 else None,
                "div_yield": round(div_yield, 4),
                "div_ttm": round(div_ttm, 4),
                "div_payout": round(div_payout, 4),
                "market_cap": market_cap,
                "suspended": bool(row.get("suspension", False)),
            }
        except Exception as e:
            return {"symbol": ticker, "valid": False, "error": str(e)}

    # ═══════════════════════════════════════════
    # 2. 基本面深度数据（Futu + yfinance 补充）
    # ═══════════════════════════════════════════

    def get_fundamentals(self, ticker: str) -> dict:
        """
        获取完整基本面。Futu 提供 PE/PB/股息/市值，yfinance 补充 ROE/负债/毛利率/FCF。

        返回标准化字典，合并两个数据源。
        """
        # 先从 Futu 获取快照
        quote = self.get_quote(ticker)
        if not quote.get("valid"):
            return self._empty_fundamentals(ticker)

        # 再用 yfinance 补充深度数据
        deep = self._yfinance_fundamentals(ticker)

        return {
            "symbol": ticker,
            "valid": True,
            # 价格（Futu）
            "price": quote["price"],
            "change_pct": quote.get("change_pct", 0),
            "volume": quote.get("volume", 0),
            # 估值（Futu）
            "pe": quote.get("pe"),
            "pb": quote.get("pb"),
            "eps": quote.get("eps"),
            "book_per_share": quote.get("book_per_share"),
            # 股息（Futu）
            "div_yield": quote.get("div_yield", 0),
            "div_ttm": quote.get("div_ttm", 0),
            "div_payout": quote.get("div_payout", 0),
            # 市值（Futu）
            "market_cap": quote.get("market_cap", 0),
            # 深度（yfinance 补充）
            "roe": deep.get("roe"),
            "debt_to_equity": deep.get("debt_to_equity"),
            "gross_margin": deep.get("gross_margin"),
            "operating_margin": deep.get("operating_margin"),
            "profit_margin": deep.get("profit_margin"),
            "revenue_growth": deep.get("revenue_growth"),
            "earnings_growth": deep.get("earnings_growth"),
            "current_ratio": deep.get("current_ratio"),
            "free_cashflow": deep.get("free_cashflow"),
            "beta": deep.get("beta"),
            "sector": deep.get("sector", ""),
            "industry": deep.get("industry", ""),
        }

    def _yfinance_fundamentals(self, ticker: str) -> dict:
        """从 yfinance 获取深度基本面（带1小时缓存）"""
        # 检查缓存
        now = datetime.datetime.now()
        if ticker in self._fundamental_cache:
            ts, data = self._fundamental_cache[ticker]
            if now - ts < self._fundamental_cache_ttl:
                return data

        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info or {}

            def _norm(val, is_pct: bool = False):
                """归一化：yfinance 有时返回小数有时百分比"""
                if val is None:
                    return None
                v = float(val)
                if is_pct and abs(v) > 10:  # >1000% 不可能
                    v = v / 100
                elif not is_pct and abs(v) > 10:  # ROE 1.41 → 正确，但 141 不对
                    v = v / 100
                return round(v, 4)

            result = {
                "roe": _norm(info.get("returnOnEquity"), True),
                "debt_to_equity": info.get("debtToEquity"),
                "gross_margin": _norm(info.get("grossMargins"), True),
                "operating_margin": _norm(info.get("operatingMargins"), True),
                "profit_margin": _norm(info.get("profitMargins"), True),
                "revenue_growth": _norm(info.get("revenueGrowth"), True),
                "earnings_growth": _norm(info.get("earningsGrowth"), True),
                "current_ratio": info.get("currentRatio"),
                "free_cashflow": info.get("freeCashflow"),
                "beta": info.get("beta"),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
            }
            self._fundamental_cache[ticker] = (datetime.datetime.now(), result)
            return result
        except Exception:
            return {}

    def _empty_fundamentals(self, ticker: str) -> dict:
        return {"symbol": ticker, "valid": False}

    # ═══════════════════════════════════════════
    # 3. 历史K线
    # ═══════════════════════════════════════════

    def get_kline(self, ticker: str, days: int = 200,
                  ktype: str = "K_DAY") -> list[dict]:
        """
        获取历史K线数据。

        Args:
            ticker: 股票代码
            days: 回溯天数
            ktype: K线类型 K_DAY/K_WEEK/K_MON/K_1M/K_5M

        返回: [{date, open, high, low, close, volume}, ...]
        """
        cache_key = f"kline:{ticker}:{days}:{ktype}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        try:
            code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
            self._rate_limit()
            from futu import RET_OK, KLType

            ktype_map = {
                "K_DAY": KLType.K_DAY, "K_WEEK": KLType.K_WEEK,
                "K_MON": KLType.K_MON, "K_1M": KLType.K_1M,
                "K_5M": KLType.K_5M,
            }
            kt = ktype_map.get(ktype, KLType.K_DAY)

            ctx = self._get_quote_ctx()
            ret, data, _ = ctx.request_history_kline(
                code, ktype=kt, max_count=min(days, 1000)
            )

            if ret != RET_OK or data.empty:
                return []

            result = []
            for _, row in data.iterrows():
                result.append({
                    "date": str(row.get("time_key", ""))[:10],
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": int(row.get("volume", 0)),
                })

            self._cache_set(cache_key, result)
            return result

        except Exception as e:
            logger.error(f"K线获取失败 {ticker}: {e}")
            return []

    def get_kline_df(self, ticker: str, days: int = 200) -> "pd.DataFrame":
        """获取K线数据为 DataFrame（兼容旧版回测接口）"""
        import pandas as pd
        rows = self.get_kline(ticker, days)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        return df

    # ═══════════════════════════════════════════
    # 4. 市场指标
    # ═══════════════════════════════════════════

    def get_vix(self) -> float:
        """获取 VIX 恐慌指数"""
        try:
            snap = self.get_snapshots(["VIX"])
            if snap.get("VIX", {}).get("valid"):
                return snap["VIX"]["price"]
        except Exception:
            pass

        # 后备：yfinance
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="5d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return 20.0

    def get_sp500_ma200_pct(self) -> float:
        """标普500相对200日均线的偏移百分比"""
        kline = self.get_kline("SPY", days=250)
        if len(kline) < 200:
            return 0.0
        closes = [k["close"] for k in kline]
        ma200 = sum(closes[-200:]) / 200
        current = closes[-1]
        return (current - ma200) / ma200 if ma200 > 0 else 0.0

    def get_sp500_pe(self) -> float:
        """获取标普500 PE（从 SPY 快照）"""
        snap = self.get_snapshots(["SPY"])
        if snap.get("SPY", {}).get("valid"):
            pe = snap["SPY"].get("pe")
            if pe and pe > 0:
                return pe
        # Fallback
        try:
            import yfinance as yf
            return float(yf.Ticker("SPY").info.get("trailingPE", 20) or 20)
        except Exception:
            return 20.0

    # ═══════════════════════════════════════════
    # 5. 股票池信息
    # ═══════════════════════════════════════════

    def get_stock_info(self, ticker: str) -> dict:
        """获取股票基本信息（行业、上市日期）"""
        try:
            self._rate_limit()
            from futu import RET_OK
            ctx = self._get_quote_ctx()
            code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
            ret, data = ctx.get_stock_basicinfo(code)
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                return {
                    "symbol": ticker,
                    "name": str(row.get("name", "")),
                    "sector": str(row.get("sector", "")),
                    "market_cap": float(row.get("market_val", 0) or 0),
                    "listing_date": str(row.get("listing_date", "")),
                }
        except Exception:
            pass
        return {"symbol": ticker}


    # ═══════════════════════════════════════════
    # 6. 统一接口 — 兼容旧 yfinance 调用模式
    # ═══════════════════════════════════════════

    def get_stock_data(self, ticker: str) -> dict:
        """
        获取单只股票的所有数据（Futu + yfinance 补充）。
        返回格式兼容旧 yfinance .info 调用模式。

        这是 Phoenix 各模块的主要数据入口。
        """
        fund = self.get_fundamentals(ticker)
        if not fund.get("valid"):
            return {}

        # 构建兼容 yfinance .info 的字典
        return {
            # 价格
            "currentPrice": fund.get("price"),
            "regularMarketPrice": fund.get("price"),
            "open": fund.get("price"),  # 近似
            "previousClose": fund.get("prev_close", fund.get("price")),
            # 估值
            "trailingPE": fund.get("pe"),
            "forwardPE": fund.get("pe"),
            "priceToBook": fund.get("pb"),
            # 盈利
            "returnOnEquity": fund.get("roe"),
            "earningPerShare": fund.get("eps"),
            # 股息
            "dividendYield": fund.get("div_yield"),
            "dividendRate": fund.get("div_ttm"),
            "payoutRatio": fund.get("div_payout"),
            # 财务
            "debtToEquity": fund.get("debt_to_equity"),
            "grossMargins": fund.get("gross_margin"),
            "operatingMargins": fund.get("operating_margin"),
            "profitMargins": fund.get("profit_margin"),
            "revenueGrowth": fund.get("revenue_growth"),
            "earningsGrowth": fund.get("earnings_growth"),
            "currentRatio": fund.get("current_ratio"),
            "freeCashflow": fund.get("free_cashflow"),
            # 市值
            "marketCap": fund.get("market_cap"),
            "enterpriseValue": None,  # Futu 不提供
            # 其他
            "beta": fund.get("beta"),
            "sector": fund.get("sector"),
            "industry": fund.get("industry"),
            "bookValue": fund.get("book_per_share"),
            "volume": fund.get("volume"),
            # 质量标记
            "_source": "futu+yfinance",
            "_valid": True,
        }

    def get_stock_data_batch(self, tickers: list[str]) -> dict[str, dict]:
        """批量获取股票数据"""
        snaps = self.get_snapshots(tickers)
        result = {}
        for t in tickers:
            if snaps.get(t, {}).get("valid"):
                result[t] = self.get_stock_data(t)
        return result


# ─── 全局单例 ───

_futu_instance: FutuProvider = None


def get_futu() -> FutuProvider:
    global _futu_instance
    if _futu_instance is None:
        _futu_instance = FutuProvider()
    return _futu_instance


# ═══════════════════════════════════════════
# 便捷函数 — 直接替代 yfinance 调用
# ═══════════════════════════════════════════

def get_stock_info(ticker: str) -> dict:
    """
    获取股票数据，Futu优先，yfinance后备。
    这是 `yf.Ticker(ticker).info` 的直接替代品。

    用法:
      from atos.data.futu_provider import get_stock_info
      info = get_stock_info("AAPL")
      price = info["currentPrice"]
      pe = info["trailingPE"]
    """
    provider = get_futu()
    data = provider.get_stock_data(ticker)
    if data and data.get("_valid"):
        return data

    # Fallback to yfinance
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info or {}
        info["_source"] = "yfinance_fallback"
        info["_valid"] = True
        return info
    except Exception:
        return {"_valid": False, "currentPrice": 0}


def get_quote(ticker: str) -> dict:
    return get_futu().get_quote(ticker)


def get_snapshots(tickers: list[str]) -> dict[str, dict]:
    return get_futu().get_snapshots(tickers)


def get_fundamentals(ticker: str) -> dict:
    return get_futu().get_fundamentals(ticker)


def get_kline(ticker: str, days: int = 200) -> list[dict]:
    return get_futu().get_kline(ticker, days)
