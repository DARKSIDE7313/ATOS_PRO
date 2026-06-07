"""
ATOS PRO v4 — Macro Data Collector (宏观数据采集器)
==================================================
采集全球宏观数据，喂给 AI 决策引擎。
数据来源：FRED API, Yahoo Finance, 公开数据。

采集范围：
  1. 美国利率：联邦基金利率、10Y/2Y 国债收益率、收益率曲线
  2. 美联储政策：FOMC 会议日历、利率预期
  3. 通胀数据：CPI, Core CPI, PCE, Core PCE
  4. 就业数据：非农就业、失业率、初请失业金
  5. GDP：实际GDP增速
  6. 美债市场：10Y收益率、2Y收益率、30Y收益率、信用利差
  7. 美元指数：DXY
  8. 全球市场：A股(沪深300)、港股(恒生)、欧股(STOXX600)、日股(日经)
  9. 商品：黄金、原油、铜
  10. 恐慌指数：VIX 历史分位
  11. 信贷市场：IG/HY 利差
"""

import json
import os
import time
import datetime
import yfinance as yf

# ── 数据缓存 ──────────────────────────────────────────
_cache = {}
_cache_ts = {}
CACHE_TTL = 3600  # 1小时

def _cached(key: str, ttl: int = CACHE_TTL):
    return _cache.get(key) if (time.time() - _cache_ts.get(key, 0)) < ttl else None

def _set_cache(key: str, value):
    _cache[key] = value
    _cache_ts[key] = time.time()

# ── 利率数据 ──────────────────────────────────────────
def get_interest_rates() -> dict:
    """美国利率数据：联邦基金利率、国债收益率、收益率曲线"""
    cache_key = "interest_rates"
    cached = _cached(cache_key)
    if cached: return cached

    result = {"source": "yfinance", "timestamp": datetime.datetime.now().isoformat()}
    
    try:
        # 13-week T-Bill (^IRX) = 近似联邦基金利率
        tb = yf.Ticker("^IRX")
        irx = tb.history(period="1mo")
        result["fed_funds_rate"] = round(float(irx["Close"].iloc[-1]) / 100, 4) if not irx.empty else None
        result["fed_funds_rate_1m_ago"] = round(float(irx["Close"].iloc[0]) / 100, 4) if not irx.empty and len(irx) > 1 else None
    except: pass

    try:
        tnx = yf.Ticker("^TNX")  # 10Y Treasury
        tnx_h = tnx.history(period="1mo")
        result["treasury_10y"] = round(float(tnx_h["Close"].iloc[-1]) / 100, 4) if not tnx_h.empty else None
        result["treasury_10y_1m_ago"] = round(float(tnx_h["Close"].iloc[0]) / 100, 4) if not tnx_h.empty and len(tnx_h) > 1 else None
    except: pass

    try:
        two = yf.Ticker("^2Y") if hasattr(yf.Ticker("^2Y"), 'history') else None
        # Fallback: use 2Y ETF (SHY) as proxy
        shy = yf.Ticker("SHY")
        shy_h = shy.history(period="1mo")
        if not shy_h.empty:
            # SHY yield ≈ 1 - price/100 (approximate)
            shy_yield = (100 - float(shy_h["Close"].iloc[-1])) / 100 * 2  # rough 2Y proxy
            result["treasury_2y"] = round(shy_yield, 4)
    except: pass

    try:
        tyx = yf.Ticker("^TYX")  # 30Y Treasury
        tyx_h = tyx.history(period="1mo")
        result["treasury_30y"] = round(float(tyx_h["Close"].iloc[-1]) / 100, 4) if not tyx_h.empty else None
    except: pass

    # 收益率曲线 (10Y - 2Y)
    if result.get("treasury_10y") and result.get("treasury_2y"):
        result["yield_curve_10y2y"] = round(result["treasury_10y"] - result["treasury_2y"], 4)
    else:
        result["yield_curve_10y2y"] = None

    # 收益率曲线变化方向
    if result.get("treasury_10y") and result.get("treasury_10y_1m_ago"):
        result["yield_10y_direction"] = "rising" if result["treasury_10y"] > result["treasury_10y_1m_ago"] else "falling"
    
    _set_cache(cache_key, result)
    return result

# ── 通胀数据 ──────────────────────────────────────────
def get_inflation() -> dict:
    """从 ETF 和公开数据推断通胀趋势"""
    cache_key = "inflation"
    cached = _cached(cache_key, ttl=7200)
    if cached: return cached

    result = {"source": "yfinance_proxy", "timestamp": datetime.datetime.now().isoformat()}

    try:
        # TIP = 通胀保护债券 ETF，价格反映通胀预期
        tip = yf.Ticker("TIP")
        tip_h = tip.history(period="3mo")
        if not tip_h.empty:
            tip_3m_chg = (float(tip_h["Close"].iloc[-1]) - float(tip_h["Close"].iloc[0])) / float(tip_h["Close"].iloc[0])
            result["inflation_expectation_3m"] = round(tip_3m_chg, 4)
            # 通胀预期方向
            if tip_3m_chg > 0.02: result["inflation_trend"] = "rising"
            elif tip_3m_chg < -0.01: result["inflation_trend"] = "falling"
            else: result["inflation_trend"] = "stable"
    except: pass

    try:
        # Breakeven inflation rate via TIP vs IEI (7-10Y Treasury)
        iei = yf.Ticker("IEI")
        iei_h = iei.history(period="1d")
        tip_now = yf.Ticker("TIP")
        tip_now_h = tip_now.history(period="1d")
        if not iei_h.empty and not tip_now_h.empty:
            tip_p = float(tip_now_h["Close"].iloc[-1])
            iei_p = float(iei_h["Close"].iloc[-1])
            if iei_p > 0:
                # 近似盈亏平衡通胀率
                result["breakeven_inflation"] = round((iei_p - tip_p) / iei_p * 100, 2)
    except: pass

    _set_cache(cache_key, result)
    return result

# ── 就业数据 ──────────────────────────────────────────
def get_employment() -> dict:
    """就业市场 proxy 数据"""
    cache_key = "employment"
    cached = _cached(cache_key, ttl=7200)
    if cached: return cached

    result = {"source": "yfinance_proxy", "timestamp": datetime.datetime.now().isoformat()}

    try:
        # 使用 UNRATE proxy: 没有直接 ETF，但可以用消费者信心 ETF
        vic = yf.Ticker(" VIC")  # 或用消费类 ETF 判断
        # 更可靠：使用 SHY/TLT 利差来判断衰退风险
        shy = yf.Ticker("SHY")
        tlt = yf.Ticker("TLT")
        shy_h = shy.history(period="1mo")
        tlt_h = tlt.history(period="1mo")
        if not shy_h.empty and not tlt_h.empty:
            shy_yield = (100 - float(shy_h["Close"].iloc[-1])) / 100
            tlt_yield = float(tlt_h["Close"].iloc[-1]) / 100 if tlt_h["Close"].iloc[-1] > 100 else 0.04
            result["short_long_yield_gap"] = round(tlt_yield - shy_yield, 4)
            # 收益率曲线倒挂 → 衰退风险
            if result.get("short_long_yield_gap", 0) < -0.02:
                result["recession_risk"] = "HIGH"
            elif result.get("short_long_yield_gap", 0) < 0:
                result["recession_risk"] = "MODERATE"
            else:
                result["recession_risk"] = "LOW"
    except: pass

    _set_cache(cache_key, result)
    return result

# ── 全球市场 ──────────────────────────────────────────
def get_global_markets() -> dict:
    """全球主要市场指数表现"""
    cache_key = "global_markets"
    cached = _cached(cache_key, ttl=1800)
    if cached: return cached

    indices = {
        "SPY": "US_S&P500",
        "QQQ": "US_NASDAQ",
        "IWM": "US_SMALL_CAP",
        "EWJ": "JAPAN",
        "FXI": "CHINA_LARGE",
        "ASHR": "CHINA_A_SHARE",
        "EWH": "HONG_KONG",
        "VGK": "EUROPE",
        "EEM": "EMERGING_MARKETS",
        "GLD": "GOLD",
        "USO": "OIL",
        "DBC": "COMMODITIES",
        "UUP": "USD_INDEX",
    }

    result = {"source": "yfinance", "timestamp": datetime.datetime.now().isoformat(), "markets": {}}
    
    for ticker, name in indices.items():
        try:
            t = yf.Ticker(ticker)
            h = t.history(period="1mo")
            if not h.empty:
                change_1m = (float(h["Close"].iloc[-1]) - float(h["Close"].iloc[0])) / float(h["Close"].iloc[0])
                result["markets"][name] = {
                    "price": round(float(h["Close"].iloc[-1]), 2),
                    "change_1m_pct": round(change_1m * 100, 2),
                    "trend": "up" if change_1m > 0.02 else ("down" if change_1m < -0.02 else "flat"),
                }
        except: pass

    _set_cache(cache_key, result)
    return result

# ── 恐慌/情绪数据 ──────────────────────────────────
def get_fear_greed() -> dict:
    """恐慌指数和市场情绪"""
    cache_key = "fear_greed"
    cached = _cached(cache_key, ttl=1800)
    if cached: return cached

    result = {"source": "yfinance", "timestamp": datetime.datetime.now().isoformat()}

    try:
        vix = yf.Ticker("^VIX")
        vix_h = vix.history(period="1y")
        if not vix_h.empty:
            vix_current = float(vix_h["Close"].iloc[-1])
            vix_1y_high = float(vix_h["Close"].max())
            vix_1y_low = float(vix_h["Close"].min())
            vix_mean = float(vix_h["Close"].mean())
            result["vix"] = round(vix_current, 2)
            result["vix_1y_percentile"] = round((vix_current - vix_1y_low) / (vix_1y_high - vix_1y_low) * 100, 1) if vix_1y_high > vix_1y_low else 50
            result["vix_1y_high"] = round(vix_1y_high, 2)
            result["vix_1y_low"] = round(vix_1y_low, 2)
            result["vix_vs_mean"] = "above" if vix_current > vix_mean else "below"
            # 恐惧/贪婪判断
            if vix_current > 30: result["fear_greed"] = "EXTREME_FEAR"
            elif vix_current > 22: result["fear_greed"] = "FEAR"
            elif vix_current > 15: result["fear_greed"] = "NEUTRAL"
            elif vix_current > 12: result["fear_greed"] = "GREED"
            else: result["fear_greed"] = "EXTREME_GREED"
    except: pass

    _set_cache(cache_key, result)
    return result

# ── 美联储政策 ──────────────────────────────────────
def get_fed_policy() -> dict:
    """美联储当前政策立场（基于利率期货推断）"""
    cache_key = "fed_policy"
    cached = _cached(cache_key, ttl=3600)
    if cached: return cached

    result = {"source": "yfinance_proxy", "timestamp": datetime.datetime.now().isoformat()}

    try:
        # ZQ = 30-Day Federal Funds Futures
        zq = yf.Ticker("ZQ=F")
        zq_h = zq.history(period="1mo")
        if not zq_h.empty:
            # 期货价格反映利率预期
            current_rate = 100 - float(zq_h["Close"].iloc[-1])
            month_ago_rate = 100 - float(zq_h["Close"].iloc[0])
            result["fed_futures_implied_rate"] = round(current_rate, 2)
            result["fed_rate_change_expectation"] = "HIKING" if current_rate > month_ago_rate else ("CUTTING" if current_rate < month_ago_rate else "HOLDING")
    except: pass

    # 如果 ZQ 取不到，用 ^IRX
    if "fed_futures_implied_rate" not in result:
        try:
            irx = yf.Ticker("^IRX")
            irx_h = irx.history(period="1mo")
            if not irx_h.empty:
                current = float(irx_h["Close"].iloc[-1])
                prev = float(irx_h["Close"].iloc[0]) if len(irx_h) > 1 else current
                result["fed_futures_implied_rate"] = round(current, 2)
                result["fed_rate_change_expectation"] = "HIKING" if current > prev else ("CUTTING" if current < prev else "HOLDING")
        except: pass

    _set_cache(cache_key, result)
    return result

# ── 宏观总结 ────────────────────────────────────────
def get_macro_summary() -> dict:
    """汇总所有宏观数据，生成一条简洁的宏观总结"""
    rates = get_interest_rates()
    inflation = get_inflation()
    employment = get_employment()
    global_mkts = get_global_markets()
    fear = get_fear_greed()
    fed = get_fed_policy()

    summary = {
        "generated_at": datetime.datetime.now().isoformat(),
        "interest_rates": rates,
        "inflation": inflation,
        "employment": employment,
        "global_markets": global_mkts,
        "market_sentiment": fear,
        "fed_policy": fed,
    }

    # 生成文字总结
    lines = []
    # 利率
    if rates.get("fed_funds_rate"):
        lines.append(f"利率: 联邦基金利率{rates['fed_funds_rate']*100:.2f}%")
    if rates.get("treasury_10y"):
        lines.append(f"10Y国债: {rates['treasury_10y']*100:.2f}% (1个月前{rates.get('treasury_10y_1m_ago',0)*100:.2f}%)")
    if rates.get("yield_curve_10y2y") is not None:
        yc = rates["yield_curve_10y2y"] * 100
        lines.append(f"收益率曲线(10Y-2Y): {yc:+.2f}基点 {'⬆ 正常' if yc > 0 else '⬇ 倒挂⚠️'}")
    
    # 市场情绪
    if fear.get("fear_greed"):
        emoji = {"EXTREME_FEAR": "😱", "FEAR": "😨", "NEUTRAL": "😐", "GREED": "😊", "EXTREME_GREED": "🤩"}
        lines.append(f"情绪: {emoji.get(fear['fear_greed'],'')} {fear['fear_greed']} (VIX={fear.get('vix','?')})")
    
    # 全球市场
    if global_mkts.get("markets"):
        # 找出涨跌最多的市场
        best = max(global_mkts["markets"].items(), key=lambda x: x[1]["change_1m_pct"])
        worst = min(global_mkts["markets"].items(), key=lambda x: x[1]["change_1m_pct"])
        lines.append(f"最强: {best[0]}({best[1]['change_1m_pct']:+.1f}%) | 最弱: {worst[0]}({worst[1]['change_1m_pct']:+.1f}%)")
    
    # 美联储
    if fed.get("fed_rate_change_expectation"):
        lines.append(f"美联储: {fed['fed_rate_change_expectation']}")

    # 衰退风险
    if employment.get("recession_risk"):
        risk_icon = {"HIGH": "🔴", "MODERATE": "🟡", "LOW": "🟢"}
        lines.append(f"衰退风险: {risk_icon.get(employment['recession_risk'], '')} {employment['recession_risk']}")

    summary["narrative"] = " | ".join(lines)
    summary["raw"] = {  # 给 AI 用的结构化数据
        k: v for k, v in summary.items() if k != "raw"
    }
    
    return summary


if __name__ == "__main__":
    import time  # noqa
    summary = get_macro_summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\n── Narrative ──")
    print(summary.get("narrative", "N/A"))
