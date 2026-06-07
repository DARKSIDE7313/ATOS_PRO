"""
ATOS PRO v2 — 全市场主动扫描器
===============================
不被动等信号——主动扫描整个市场寻找机会。

策略：
  1. 涨幅榜 — 动量突破候选
  2. 跌幅榜 — 超跌反弹候选（Burry风格）
  3. 成交量异常 — 资金异动检测
  4. 52周新低 — 深度价值机会
  5. 突破均线 — 趋势确认信号
  6. 估值洼地 — PE/PB极低候选

数据源: yfinance (S&P 500 + NASDAQ 100 成分股)
"""

import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("market_scanner")

# 扩展标的池 — S&P 500 + NASDAQ 100 去重约 550 只
def get_broad_universe() -> list[str]:
    """获取广泛的候选标的池 — S&P 100 + 精选流动性股票"""
    # S&P 100 成分股 (最具流动性的100只)
    sp100 = [
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","BRK-B","JPM","V",
        "JNJ","WMT","PG","MA","UNH","HD","BAC","XOM","COST","NFLX",
        "ADBE","CRM","AMD","INTC","QCOM","TXN","AVGO","CSCO","ORCL","IBM",
        "ABT","TMO","DHR","ABBV","MRK","PFE","BMY","LLY","AMGN","GILD",
        "CVX","COP","EOG","SLB","OXY","XEL","DUK","SO","NEE",
        "DIS","NKE","SBUX","MCD","LOW","TJX","TGT","ROST","ORLY","AZO",
        "GS","MS","BLK","SCHW","C","AXP","BK","USB","PNC","TFC",
        "CAT","BA","GE","HON","LMT","RTX","UPS","FDX","DE","MMM",
        "T","VZ","CMCSA","CHTR","TMUS",
        "SPY","QQQ","IWM","DIA","TLT","GLD","SLV","USO","EEM","VWO",
        "XLF","XLE","XLK","XLV","XLI","XLP","XLU","XLB","XLY","XLRE",
    ]
    try:
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        wiki_symbols = sp500["Symbol"].tolist()
        symbols = list(set(sp100 + wiki_symbols))
    except Exception:
        symbols = sp100

    symbols = [s.replace(".", "-") for s in symbols]
    logger.info(f"扩展标的池: {len(symbols)} 只")
    return symbols


def scan_top_movers(symbols: list[str] = None, top_n: int = 15) -> dict:
    """扫描涨幅最大和跌幅最大的股票"""
    if symbols is None:
        symbols = get_broad_universe()[:100]  # 取样100只测速

    results = []
    for sym in symbols[:100]:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose", 0)
            if price <= 0 or prev_close <= 0:
                continue
            change_pct = (price - prev_close) / prev_close
            vol = info.get("volume", 0)
            avg_vol = info.get("averageVolume", 0)
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1

            if abs(change_pct) > 0.02 or vol_ratio > 2:  # 涨跌>2% 或 放量>2倍
                results.append({
                    "symbol": sym,
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 4),
                    "vol_ratio": round(vol_ratio, 1),
                    "market_cap": info.get("marketCap", 0),
                    "sector": info.get("sector", "?"),
                })
        except Exception:
            continue

    gainers = sorted([r for r in results if r["change_pct"] > 0],
                     key=lambda x: x["change_pct"], reverse=True)[:top_n]
    losers = sorted([r for r in results if r["change_pct"] < 0],
                    key=lambda x: x["change_pct"])[:top_n]

    logger.info(f"涨跌扫描: {len(gainers)}只领涨 + {len(losers)}只领跌")
    return {"gainers": gainers, "losers": losers}


def scan_52week_lows(symbols: list[str] = None, top_n: int = 20) -> list[dict]:
    """扫描接近52周低点的股票 — Burry 喜欢的"路杀"机会"""
    if symbols is None:
        symbols = get_broad_universe()[:150]

    near_lows = []
    for sym in symbols[:150]:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}
            price = info.get("currentPrice", 0)
            low_52w = info.get("fiftyTwoWeekLow", 0)
            high_52w = info.get("fiftyTwoWeekHigh", 0)

            if price <= 0 or low_52w <= 0:
                continue

            pct_from_low = (price - low_52w) / low_52w
            pct_from_high = (price - high_52w) / high_52w if high_52w > 0 else -1

            # 距52周低点 < 15% 且 距高点 > 30%（真跌不是假摔）
            if pct_from_low < 0.15 and pct_from_high < -0.30:
                pe = info.get("trailingPE") or info.get("forwardPE", 0)
                pb = info.get("priceToBook", 0)
                fcf = info.get("freeCashflow", 0)

                # 筛选有价值的机会（不是垃圾股）
                if pe > 0 or pb > 0:
                    near_lows.append({
                        "symbol": sym,
                        "price": round(price, 2),
                        "pct_from_low": round(pct_from_low, 4),
                        "pct_from_high": round(pct_from_high, 4),
                        "pe": round(pe, 1) if pe else None,
                        "pb": round(pb, 2) if pb else None,
                        "sector": info.get("sector", "?"),
                        "market_cap": info.get("marketCap", 0),
                    })
        except Exception:
            continue

    near_lows.sort(key=lambda x: x["pct_from_low"])
    logger.info(f"52周低点扫描: {len(near_lows)}只候选, Top:{near_lows[0]['symbol'] if near_lows else '无'}")

    return near_lows[:top_n]


def scan_volume_breakout(symbols: list[str] = None, top_n: int = 15) -> list[dict]:
    """扫描成交量异常放大的股票 — 资金异动"""
    if symbols is None:
        symbols = get_broad_universe()[:100]

    breakouts = []
    for sym in symbols[:100]:
        try:
            df = yf.download(sym, period="5d", interval="1d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 3:
                continue

            vol = df["Volume"].squeeze()
            today_vol = float(vol.iloc[-1])
            avg_vol_5d = float(vol.iloc[:-1].mean()) if len(vol) > 1 else today_vol
            vol_ratio = today_vol / avg_vol_5d if avg_vol_5d > 0 else 1

            close = df["Close"].squeeze()
            today_px = float(close.iloc[-1])
            prev_px = float(close.iloc[-2]) if len(close) > 1 else today_px
            change_pct = (today_px - prev_px) / prev_px if prev_px > 0 else 0

            if vol_ratio > 3:  # 放量3倍以上 = 异常
                breakouts.append({
                    "symbol": sym,
                    "price": round(today_px, 2),
                    "change_pct": round(change_pct, 4),
                    "vol_ratio": round(vol_ratio, 1),
                    "direction": "BULLISH" if change_pct > 0 else "BEARISH",
                })
        except Exception:
            continue

    breakouts.sort(key=lambda x: x["vol_ratio"], reverse=True)
    logger.info(f"放量异常: {len(breakouts)}只, Top:{breakouts[0]['symbol'] if breakouts else '无'}")
    return breakouts[:top_n]


def scan_value_deep(symbols: list[str] = None, top_n: int = 20) -> list[dict]:
    """深度价值扫描 — PE<10, PB<1, 正FCF"""
    if symbols is None:
        symbols = get_broad_universe()[:200]

    deep_value = []
    for sym in symbols[:200]:
        try:
            stock = yf.Ticker(sym)
            info = stock.info or {}

            pe = info.get("trailingPE") or info.get("forwardPE", 999)
            pb = info.get("priceToBook", 999)
            fcf = info.get("freeCashflow", 0)
            div_yield = info.get("dividendYield", 0) or 0
            debt_eq = info.get("debtToEquity", 100) or 100

            if pe and pb and 0 < pe < 12 and 0 < pb < 1.2 and fcf > 0:
                deep_value.append({
                    "symbol": sym,
                    "pe": round(pe, 1),
                    "pb": round(pb, 2),
                    "div_yield": round(div_yield, 3) if div_yield else None,
                    "debt_equity": round(debt_eq, 1) if debt_eq else None,
                    "sector": info.get("sector", "?"),
                    "market_cap": info.get("marketCap", 0),
                })
        except Exception:
            continue

    deep_value.sort(key=lambda x: x["pe"])
    logger.info(f"深度价值: {len(deep_value)}只, Top:{deep_value[0]['symbol'] if deep_value else '无'} PE={deep_value[0]['pe'] if deep_value else '?'}")
    return deep_value[:top_n]


def full_market_scan() -> dict:
    """全市场综合扫描 — 一键发现所有机会"""
    logger.info("=" * 50)
    logger.info("全市场主动扫描启动")
    logger.info("=" * 50)

    universe = get_broad_universe()

    results = {
        "universe_size": len(universe),
        "top_gainers": scan_top_movers(universe[:100]).get("gainers", [])[:8],
        "top_losers": scan_top_movers(universe[:100]).get("losers", [])[:8],
        "near_52w_lows": scan_52week_lows(universe[:150])[:8],
        "volume_breakouts": scan_volume_breakout(universe[:100])[:8],
        "deep_value": scan_value_deep(universe[:200])[:10],
    }

    logger.info(f"全市场扫描完成: {len(universe)}只标的池")
    return results


def get_actionable_opportunities(scan_results: dict) -> list[dict]:
    """从扫描结果中提取可操作的机会（合并去重）"""
    seen = set()
    opportunities = []

    for r in scan_results.get("top_gainers", [])[:3]:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            opportunities.append({**r, "signal": "MOMENTUM", "score": 7})

    for r in scan_results.get("near_52w_lows", [])[:3]:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            opportunities.append({**r, "signal": "DEEP_VALUE", "score": 8})

    for r in scan_results.get("volume_breakouts", [])[:3]:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            opportunities.append({**r, "signal": "VOLUME_BREAKOUT", "score": 6})

    for r in scan_results.get("deep_value", [])[:3]:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            opportunities.append({**r, "signal": "VALUE_PLAY", "score": 9})

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities
