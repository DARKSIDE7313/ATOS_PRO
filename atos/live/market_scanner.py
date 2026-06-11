"""
ATOS PRO v4 — Market Scanner (主动扫描模块)
================================================
不被动持有，主动扫描全市场寻找可乘之机。


信号类型：
  GAP     — 跳空缺口（回补概率 78-94%）
  RSI     — RSI 超卖反弹
  VOL     — 异常成交量
  BB      — 布林带收窄突破
  MOM     — 动量突破
  VALUE   — 深度价值（低P/E+P/B）
  EARN    — 财报前催化剂
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
from atos.core.logging import get_logger

logger = get_logger("phoenix.scanner")

# 扫描股票池（标普500代表性股票 + 高流动性ETF）
SCAN_UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA",
    "JPM","BAC","GS","V","MA","BLK",
    "JNJ","UNH","PFE","ABBV","MRK","LLY",
    "XOM","CVX","COP",
    "COST","WMT","HD","NKE","MCD","SBUX","DIS",
    "CAT","BA","GE","HON","UPS",
    "AMD","INTC","AVGO","QCOM","TXN","MU","AMAT",
    "ADBE","CRM","NFLX","PYPL","UBER",
    "SPY","QQQ","IWM","TLT","GLD","SLV",
    "KO","PEP","PG","CL","KMB",
    "TMO","DHR","ISRG","SYK","BSX",
    "PLTR","SNOW","CRWD","ZS","DDOG",
]


class MarketScanner:
    """市场扫描器：寻找被低估/被忽视的机会"""

    def __init__(self):
        self._price_cache = {}
        self._cache_ts = 0

    def _cached_price(self, symbol: str) -> float:
        """缓存价格（5分钟）"""
        now = datetime.datetime.now().timestamp()
        if (now - self._cache_ts) < 300 and symbol in self._price_cache:
            return self._price_cache[symbol]
        try:
            info = yf.Ticker(symbol).info or {}
            price = info.get("currentPrice", 0) or info.get("regularMarketPrice", 0)
            if price <= 0:
                hist = yf.Ticker(symbol).history(period="1d")
                price = float(hist["Close"].iloc[-1]) if not hist.empty else 0
            self._price_cache[symbol] = price
            self._cache_ts = now
            return price
        except Exception:
            return 0

    def update_all_prices(self, symbols: list[str]) -> dict:
        """批量更新价格（用于持仓P&L）"""
        prices = {}
        for sym in symbols:
            prices[sym] = self._cached_price(sym)
        return prices

    def scan_gap(self) -> list[dict]:
        """扫描跳空缺口"""
        results = []
        for sym in SCAN_UNIVERSE[:30]:
            try:
                hist = yf.Ticker(sym).history(period="5d", interval="1d")
                if hist.empty or len(hist) < 2: continue
                close = hist["Close"].squeeze()
                open_p = hist["Open"].squeeze()
                yc = float(close.iloc[-2])
                to = float(open_p.iloc[-1])
                gap = (to - yc) / yc
                if abs(gap) < 0.01: continue
                gap_filled = (float(close.iloc[-1]) < yc) if gap > 0 else (float(close.iloc[-1]) > yc)
                if gap_filled:
                    results.append({
                        "symbol": sym, "signal": "GAP_FILLED",
                        "gap_pct": round(gap*100,2),
                        "confidence": 85, "price": self._cached_price(sym),
                        "desc": f"跳空{abs(gap)*100:.1f}%已回补"
                    })
            except Exception: pass
        return sorted(results, key=lambda x: -x["confidence"])

    def scan_rsi_extremes(self) -> list[dict]:
        """扫描 RSI 超卖/超买"""
        results = []
        for sym in SCAN_UNIVERSE[:30]:
            try:
                hist = yf.Ticker(sym).history(period="3mo", interval="1d")
                if hist.empty or len(hist) < 14: continue
                close = hist["Close"].squeeze()
                delta = close.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, 1e-9)
                rsi = 100 - (100 / (1 + rs))
                rsi_val = float(rsi.iloc[-1])
                price = self._cached_price(sym)
                if rsi_val < 30:
                    results.append({
                        "symbol": sym, "signal": "RSI_OVERSOLD",
                        "rsi": round(rsi_val,1), "confidence": 65,
                        "price": price, "desc": f"RSI={rsi_val:.0f}超卖反弹信号"
                    })
                elif rsi_val > 75:
                    results.append({
                        "symbol": sym, "signal": "RSI_OVERBOUGHT",
                        "rsi": round(rsi_val,1), "confidence": 55,
                        "price": price, "desc": f"RSI={rsi_val:.0f}注意回调"
                    })
            except Exception: pass
        return sorted(results, key=lambda x: -x["confidence"])

    def scan_volume_spike(self) -> list[dict]:
        """扫描异常成交量"""
        results = []
        for sym in SCAN_UNIVERSE[:20]:
            try:
                hist = yf.Ticker(sym).history(period="3mo", interval="1d")
                if hist.empty or len(hist) < 50: continue
                vol = hist["Volume"].squeeze()
                close = hist["Close"].squeeze()
                avg20 = float(vol.rolling(20).mean().iloc[-1])
                curr = float(vol.iloc[-1])
                ratio = curr / avg20 if avg20 > 0 else 0
                if ratio > 2.0:
                    chg = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close)>1 else 0
                    results.append({
                        "symbol": sym, "signal": "VOL_SPIKE",
                        "vol_ratio": round(ratio,1), "confidence": min(80, 50+int(ratio*10)),
                        "price": self._cached_price(sym),
                        "desc": f"量比{ratio:.1f}x 价格变化{chg*100:+.1f}%"
                    })
            except Exception: pass
        return sorted(results, key=lambda x: -x["confidence"])

    def scan_value_deep(self) -> list[dict]:
        """扫描深度价值（用已有引擎）"""
        results = []
        try:
            from atos.longterm.engine import magic_formula_rank
            ranking = magic_formula_rank(SCAN_UNIVERSE[:40])
            for r in ranking[:10]:
                results.append({
                    "symbol": r["symbol"], "signal": "DEEP_VALUE",
                    "ey": round(r.get("earnings_yield",0)*100,1),
                    "roc": round(r.get("roc",0)*100,1),
                    "confidence": 60 + int(min(25, r.get("earnings_yield",0)*800)),
                    "price": self._cached_price(r["symbol"]),
                    "desc": f"盈利收益率{r['earnings_yield']*100:.1f}% ROC{r.get('roc',0)*100:.0f}%"
                })
        except Exception: pass
        return sorted(results, key=lambda x: -x["confidence"])

    def full_scan(self) -> dict:
        """全量扫描，返回所有信号"""
        logger.info("开始全市场扫描...")
        results = {
            "gap": self.scan_gap(),
            "rsi": self.scan_rsi_extremes(),
            "volume": self.scan_volume_spike(),
            "value": self.scan_value_deep(),
            "timestamp": datetime.datetime.now().isoformat(),
        }
        # 合并所有扫描结果
        all_scans = results["gap"] + results["rsi"] + results["volume"] + results["value"]
        all_scans.sort(key=lambda x: -x["confidence"])
        results["all"] = all_scans[:15]  # Top 15 signals
        logger.info(f"扫描完成: {len(all_scans)} 个信号, 选出 Top {len(results['all'])}")
        return results


_scanner: MarketScanner = None

def get_scanner() -> MarketScanner:
    global _scanner
    if _scanner is None:
        _scanner = MarketScanner()
    return _scanner

def run_full_scan() -> dict:
    return get_scanner().full_scan()

def update_position_prices(symbols: list[str]) -> dict:
    return get_scanner().update_all_prices(symbols)
