"""
ATOS PRO — SMC (Smart Money Concepts) 因子模块
===============================================
核心概念（ICT/Inner Circle Trader 方法论）：

  1. Order Block (OB) — 机构建仓的K线区域
     Bullish OB: 上涨前最后一根大阴线
     Bearish OB: 下跌前最后一根大阳线

  2. Fair Value Gap (FVG) — 三根K线之间的未成交价格缺口
     价格会回补这个缺口

  3. Break of Structure (BOS) — 趋势突破
     价格突破前一个等高点（上升趋势延续）
     价格跌破前一个等低点（下降趋势延续）

  4. Change of Character (CHOCH) — 趋势转变
     上升趋势中出现更低的低点 = 趋势可能转空
     下降趋势中出现更高的高点 = 趋势可能转多

  5. Liquidity Pool (流动性池) — 等高点/等低点
     Buy Side Liquidity (BSL): 等高点上方，散户止损集中区
     Sell Side Liquidity (SSL): 等低点下方，散户止损集中区

  6. Stop Hunt — 价格先突破流动性池然后反转
     机构猎杀止损后再反向拉

用法:
  from atos.factors.smc import compute_smc_score
  smc_result = compute_smc_score(symbol, df)  # df 是日线K线
"""

import pandas as pd
import numpy as np
from typing import Optional


def find_order_blocks(df: pd.DataFrame, lookback: int = 30) -> dict:
    """识别最近的订单块 (Order Block)

    Bullish OB: 一波上涨行情开始前，最后一根跌幅>1%的阴线
    Bearish OB: 一波下跌行情开始前，最后一根涨幅>1%的阳线

    返回: {"bullish": {"price": float, "strength": float}, "bearish": {...}}
    """
    if df.empty or len(df) < 10:
        return {"bullish": None, "bearish": None}

    close = df["Close"].values
    open_p = df["Open"].values
    high = df["High"].values
    low = df["Low"].values
    n = len(close)
    result = {"bullish": None, "bearish": None}

    # Bullish OB: 找最近一波上涨前的最后一根大阴线
    # 从后往前找：价格从低到高的转折点前的那根大阴线
    for i in range(min(lookback, n - 5), 5, -1):
        # 检测上涨启动：当前价格比5根前高2%以上，且当前是上涨趋势起点
        if i < 5:
            break
        if close[i] < close[i-5] * 0.98:  # 5根前价格更高（在回调/底部）
            continue
        # 找上涨启动前的那根K线
        prev = i - 1
        if prev < 1:
            break
        body_pct = abs(close[prev] - open_p[prev]) / open_p[prev]
        is_bearish = close[prev] < open_p[prev]
        if is_bearish and body_pct > 0.01:  # 阴线且实体>1%
            # 确认之后确实涨了
            if i + 3 < n and close[i+3] > close[i] * 1.01:
                ob_price = (high[prev] + low[prev]) / 2
                strength = min(body_pct * 10, 1.0)  # 实体越大信号越强
                result["bullish"] = {"price": round(ob_price, 2), "strength": round(strength, 2)}
                break

    # Bearish OB: 找最近一波下跌前的最后一根大阳线
    for i in range(min(lookback, n - 5), 5, -1):
        if i < 5:
            break
        if close[i] > close[i-5] * 1.02:  # 5根前价格更低（在顶部/反弹）
            continue
        prev = i - 1
        if prev < 1:
            break
        body_pct = abs(close[prev] - open_p[prev]) / open_p[prev]
        is_bullish = close[prev] > open_p[prev]
        if is_bullish and body_pct > 0.01:
            if i + 3 < n and close[i+3] < close[i] * 0.99:
                ob_price = (high[prev] + low[prev]) / 2
                strength = min(body_pct * 10, 1.0)
                result["bearish"] = {"price": round(ob_price, 2), "strength": round(strength, 2)}
                break

    return result


def find_fvg(df: pd.DataFrame, lookback: int = 30) -> dict:
    """识别 Fair Value Gap (FVG) — 未成交价格缺口

    三根连续K线，中间K线的高低点与两侧有缺口。
    看涨FVG: 中间K线的低点 > 左侧K线的高点（向上跳空未回补）
    看跌FVG: 中间K线的高点 < 左侧K线的低点（向下跳空未回补）

    返回: {"bullish": {"price_high": float, "price_low": float, "unfilled": bool}, "bearish": {...}}
    """
    if df.empty or len(df) < 5:
        return {"bullish": None, "bearish": None}

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(close)
    result = {"bullish": None, "bearish": None}

    for i in range(min(lookback, n - 3), 2, -1):
        # 看涨FVG: candle[i-1]的高点 < candle[i+1]的低点
        if high[i-1] < low[i+1]:
            gap_high = low[i+1]
            gap_low = high[i-1]
            # 检查是否已被回补
            unfilled = True
            for j in range(i+2, min(i+10, n)):
                if low[j] <= gap_high and high[j] >= gap_low:
                    unfilled = False
                    break
            result["bullish"] = {
                "gap_high": round(gap_high, 2),
                "gap_low": round(gap_low, 2),
                "unfilled": unfilled,
                "size_pct": round((gap_high - gap_low) / gap_low * 100, 2),
            }
            break

    for i in range(min(lookback, n - 3), 2, -1):
        # 看跌FVG: candle[i-1]的低点 > candle[i+1]的高点
        if low[i-1] > high[i+1]:
            gap_high = low[i-1]
            gap_low = high[i+1]
            unfilled = True
            for j in range(i+2, min(i+10, n)):
                if low[j] <= gap_high and high[j] >= gap_low:
                    unfilled = False
                    break
            result["bearish"] = {
                "gap_high": round(gap_high, 2),
                "gap_low": round(gap_low, 2),
                "unfilled": unfilled,
                "size_pct": round((gap_high - gap_low) / gap_low * 100, 2),
            }
            break

    return result


def find_bos_choch(df: pd.DataFrame, lookback: int = 30) -> dict:
    """识别 BOS (趋势突破) 和 CHOCH (趋势转变)

    BOS (上升): 价格突破前一个等高点
    BOS (下降): 价格跌破前一个等低点
    CHOCH (牛转熊): 上升趋势中出现更低的低点
    CHOCH (熊转牛): 下降趋势中出现更高的高点

    返回: {"bos_up": bool, "bos_down": bool, "choch_bullish": bool, "choch_bearish": bool}
    """
    if df.empty or len(df) < lookback:
        return {"bos_up": False, "bos_down": False, "choch_bullish": False, "choch_bearish": False}

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(close)

    # 找过去20根的等高点 (EQL) 和等低点 (EQH)
    window = min(20, n - 1)
    recent_highs = high[-window:]
    recent_lows = low[-window:]

    # 等高点：找至少出现2次的接近高点（1%容忍度）
    eq_highs = []
    eq_lows = []
    for i in range(len(recent_highs)):
        for j in range(i + 1, len(recent_highs)):
            if abs(recent_highs[i] - recent_highs[j]) / recent_highs[i] < 0.01:
                eq_highs.append((recent_highs[i] + recent_highs[j]) / 2)
            if abs(recent_lows[i] - recent_lows[j]) / recent_lows[i] < 0.01:
                eq_lows.append((recent_lows[i] + recent_lows[j]) / 2)

    current_price = close[-1]
    last_high = max(high[-5:]) if n >= 5 else high[-1]
    last_low = min(low[-5:]) if n >= 5 else low[-1]

    # BOS
    bos_up = False
    bos_down = False
    if eq_highs:
        highest_eql = max(eq_highs)
        if current_price > highest_eql * 1.005:  # 突破等高点0.5%以上
            bos_up = True
    if eq_lows:
        lowest_eqh = min(eq_lows)
        if current_price < lowest_eqh * 0.995:  # 跌破等低点0.5%以上
            bos_down = True

    # CHOCH (简化版本：看最近10根的高低点关系)
    choch_bullish = False  # 熊转牛
    choch_bearish = False  # 牛转熊

    if n >= 20:
        old_highs = high[-20:-10]
        new_highs = high[-10:]
        old_lows = low[-20:-10]
        new_lows = low[-10:]

        if max(new_highs) > max(old_highs) and min(new_lows) < min(old_lows):
            choch_bearish = True  # 更高的高+更低的低 = 牛转熊（宽幅震荡/派发）
        elif max(new_highs) < max(old_highs) and min(new_lows) > min(old_lows):
            choch_bullish = True  # 更低的高+更高的低 = 熊转牛（积累）

    return {
        "bos_up": bos_up,
        "bos_down": bos_down,
        "choch_bullish": choch_bullish,
        "choch_bearish": choch_bearish,
    }


def find_liquidity_pools(df: pd.DataFrame, lookback: int = 30) -> dict:
    """识别流动性池 (Liquidity Pools)

    BSL (Buy Side Liquidity): 等高点上方 — 多头止损集中区（也是空头目标）
    SSL (Sell Side Liquidity): 等低点下方 — 空头止损集中区（也是多头目标）

    返回: {"bsl": float, "ssl": float, "near_bsl": bool, "near_ssl": bool}
    """
    if df.empty or len(df) < lookback:
        return {"bsl": None, "ssl": None, "near_bsl": False, "near_ssl": False}

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(close)
    current_price = close[-1]

    # 找过去20根的等高/等低
    window = min(20, n - 1)
    recent_highs = high[-window:]
    recent_lows = low[-window:]

    eq_highs = []
    eq_lows = []
    for i in range(len(recent_highs)):
        for j in range(i + 1, len(recent_highs)):
            if abs(recent_highs[i] - recent_highs[j]) / recent_highs[i] < 0.01:
                eq_highs.append((recent_highs[i] + recent_highs[j]) / 2)
            if abs(recent_lows[i] - recent_lows[j]) / recent_lows[i] < 0.01:
                eq_lows.append((recent_lows[i] + recent_lows[j]) / 2)

    bsl = max(eq_highs) if eq_highs else None
    ssl = min(eq_lows) if eq_lows else None

    # 判断价格是否在流动性池附近（1%以内）
    near_bsl = False
    near_ssl = False
    if bsl:
        near_bsl = abs(current_price - bsl) / bsl < 0.01
    if ssl:
        near_ssl = abs(current_price - ssl) / ssl < 0.01

    return {
        "bsl": round(bsl, 2) if bsl else None,
        "ssl": round(ssl, 2) if ssl else None,
        "near_bsl": near_bsl,
        "near_ssl": near_ssl,
    }


def detect_stop_hunt(df: pd.DataFrame, lookback: int = 30) -> dict:
    """检测 Stop Hunt (流动性猎杀)

    模式：价格先突破等高点（猎杀BSL），然后回到订单块
         或者价格先跌破等低点（猎杀SSL），然后回到订单块

    返回: {"bsl_hunted": bool, "ssl_hunted": bool, "hunt_strength": float}
    """
    if df.empty or len(df) < lookback:
        return {"bsl_hunted": False, "ssl_hunted": False, "hunt_strength": 0.0}

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(close)

    if n < 10:
        return {"bsl_hunted": False, "ssl_hunted": False, "hunt_strength": 0.0}

    # 取最近10根K线
    recent = df.iloc[-10:]
    mid_idx = len(recent) // 2

    pre_highs = recent["High"].iloc[:mid_idx].values
    pre_lows = recent["Low"].iloc[:mid_idx].values
    post_highs = recent["High"].iloc[mid_idx:].values
    post_lows = recent["Low"].iloc[mid_idx:].values
    post_close = recent["Close"].iloc[mid_idx:].values
    pre_close = recent["Close"].iloc[:mid_idx].values

    bsl_hunted = False
    ssl_hunted = False

    if len(pre_highs) > 0 and len(post_highs) > 0:
        pre_max = max(pre_highs)
        post_min_after = min(post_lows)
        # BSL hunt: 前半段突破前高等点，后半段跌回
        if any(h > pre_max * 1.002 for h in post_highs[:2]) and post_close[-1] < pre_max * 0.998:
            bsl_hunted = True

    if len(pre_lows) > 0 and len(post_lows) > 0:
        pre_min = min(pre_lows)
        # SSL hunt: 前半段跌破前低点，后半段涨回
        if any(l < pre_min * 0.998 for l in post_lows[:2]) and post_close[-1] > pre_min * 1.002:
            ssl_hunted = True

    hunt_strength = 0.0
    if bsl_hunted:
        hunt_strength += 0.5
    if ssl_hunted:
        hunt_strength += 0.5

    return {
        "bsl_hunted": bsl_hunted,
        "ssl_hunted": ssl_hunted,
        "hunt_strength": hunt_strength,
    }


def compute_smc_score(symbol: str, df: pd.DataFrame) -> dict:
    """计算 SMC 综合得分，合并到信号引擎的 signals dict

    参数:
        symbol: 标的代码（仅用于日志）
        df: 日线K线 DataFrame (需含 Open, High, Low, Close, Volume)

    返回:
        {
            "smc_score": float,        # 综合SMC得分 (0.0 - 0.60)
            "smc_breakdown": {          # 分解
                "ob_score": float,      # 订单块得分
                "fvg_score": float,     # 价格缺口得分
                "bos_score": float,     # 趋势结构得分
                "liquidity_score": float,  # 流动性得分
                "stop_hunt_score": float,  # Stop Hunt得分
                "choch_score": float,   # 趋势转变得分
            },
            "ob": dict,                 # 原始OB数据
            "fvg": dict,                # 原始FVG数据
            "structure": dict,          # 原始BOS/CHOCH数据
            "liquidity": dict,          # 原始流动性数据
            "stop_hunt": dict,          # 原始Stop Hunt数据
        }
    """
    ob = find_order_blocks(df)
    fvg = find_fvg(df)
    structure = find_bos_choch(df)
    liquidity = find_liquidity_pools(df)
    stop_hunt = detect_stop_hunt(df)

    # 计算各项得分（封顶防止过度加权）
    ob_score = 0.0
    if ob.get("bullish") and ob["bullish"].get("strength", 0) > 0.5:
        ob_score += 0.10  # 强势Bullish OB
    if ob.get("bearish") and ob["bearish"].get("strength", 0) > 0.5:
        ob_score += 0.10  # 强势Bearish OB

    fvg_score = 0.0
    if fvg.get("bullish") and fvg["bullish"].get("unfilled", False):
        fvg_score += 0.08  # 看涨FVG未回补 → 价格会往上回补
    elif fvg.get("bullish"):
        fvg_score -= 0.05  # FVG已回补 → 利好已兑现
    if fvg.get("bearish") and fvg["bearish"].get("unfilled", False):
        fvg_score -= 0.08  # 看跌FVG未回补 → 价格会往下回补

    bos_score = 0.0
    if structure.get("bos_up"):
        bos_score += 0.10  # 上升趋势突破 → 做多信号
    if structure.get("bos_down"):
        bos_score -= 0.10  # 下降趋势突破 → 做空信号

    choch_score = 0.0
    if structure.get("choch_bullish"):
        choch_score += 0.15  # 熊转牛 → 最强调转信号
    if structure.get("choch_bearish"):
        choch_score -= 0.15  # 牛转熊

    liquidity_score = 0.0
    if liquidity.get("near_bsl"):
        liquidity_score -= 0.08  # 价格在BSL附近 → 可能被猎杀做空
    if liquidity.get("near_ssl"):
        liquidity_score += 0.08  # 价格在SSL附近 → 可能被猎杀做多

    stop_hunt_score = 0.0
    if stop_hunt.get("bsl_hunted"):
        stop_hunt_score += 0.10  # BSL刚被猎杀 → 做空机会
    if stop_hunt.get("ssl_hunted"):
        stop_hunt_score += 0.08  # SSL刚被猎杀 → 做多机会

    # 综合SMC得分（封顶±0.60）
    smc_score = ob_score + fvg_score + bos_score + choch_score + liquidity_score + stop_hunt_score
    smc_score = max(-0.60, min(0.60, smc_score))

    return {
        "smc_score": round(smc_score, 3),
        "smc_breakdown": {
            "ob_score": round(ob_score, 3),
            "fvg_score": round(fvg_score, 3),
            "bos_score": round(bos_score, 3),
            "choch_score": round(choch_score, 3),
            "liquidity_score": round(liquidity_score, 3),
            "stop_hunt_score": round(stop_hunt_score, 3),
        },
        "ob": ob,
        "fvg": fvg,
        "structure": structure,
        "liquidity": liquidity,
        "stop_hunt": stop_hunt,
    }
