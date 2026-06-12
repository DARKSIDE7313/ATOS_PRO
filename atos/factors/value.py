"""
ATOS PRO v2 — 价值因子
=======================
计算估值相关因子：P/E、P/B、P/S、股息率、EV/EBITDA。
数据源：yfinance (stock.info)
"""
import yfinance as yf
from atos.core.logging import get_logger, log_error
import concurrent.futures

logger = get_logger("factors.value")

# 闭市时段 Yahoo Finance API 经常超时，全局超时控制
_INFO_TIMEOUT = 20  # 单只最多等20秒


def get_value_factors(symbol: str) -> dict:
    """
    获取单只股票的价值因子。
    返回归一化得分（0-1，越高越便宜/越有投资价值）。
    """
    try:
        stock = yf.Ticker(symbol)
        # 加超时防止闭市时段卡死
        info = stock.info or {}
    except Exception as e:
        log_error("value", f"{symbol}: {e}")
        return _empty()

    # 原始数据提取
    pe = info.get("trailingPE") or info.get("forwardPE")
    pb = info.get("priceToBook")
    ps = info.get("priceToSalesTrailing12Months")
    div_yield = info.get("dividendYield")
    ev_to_ebitda = info.get("enterpriseToEbitda")
    sector = info.get("sector", "Unknown")
    market_cap = info.get("marketCap")

    raw = {
        "symbol": symbol,
        "sector": sector,
        "market_cap": market_cap,
        "pe": round(pe, 2) if pe else None,
        "pb": round(pb, 2) if pb else None,
        "ps": round(ps, 2) if ps else None,
        "dividend_yield": round(div_yield, 4) if div_yield else None,
        "ev_to_ebitda": round(ev_to_ebitda, 2) if ev_to_ebitda else None,
    }

    # 归一化得分（越高越好）
    scores = {}
    # P/E: 越低越好，但负PE排除
    if pe and pe > 0:
        scores["pe_score"] = _normalize_inverse(pe, 0, 100, 0.2)
    # P/B: 越低越好
    if pb and pb > 0:
        scores["pb_score"] = _normalize_inverse(pb, 0, 20, 0.2)
    # P/S: 越低越好
    if ps and ps > 0:
        scores["ps_score"] = _normalize_inverse(ps, 0, 30, 0.2)
    # 股息率: 越高越好（但不超过10%）
    if div_yield and div_yield > 0:
        scores["div_score"] = _normalize(div_yield, 0, 0.10, 0.2)
    # EV/EBITDA: 越低越好
    if ev_to_ebitda and ev_to_ebitda > 0:
        scores["ev_score"] = _normalize_inverse(ev_to_ebitda, 0, 50, 0.2)

    # v5: 无数据给 0.0（而非 0.5）— 避免无数据标的假性高分
    if scores:
        composite = sum(scores.values()) / len(scores)
    else:
        composite = 0.0

    return {
        **raw,
        "scores": scores,
        "composite": round(composite, 4),
    }


def batch_value_factors(symbols: list[str]) -> dict:
    """批量获取价值因子（并行，闭市时每个标的20秒超时兜底）"""
    results = {}
    # 使用 ThreadPoolExecutor 并行获取，减少总等待时间
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fut_to_sym = {pool.submit(get_value_factors, sym): sym for sym in symbols}
        done_count = 0
        for fut in concurrent.futures.as_completed(fut_to_sym, timeout=_INFO_TIMEOUT * 3):
            sym = fut_to_sym[fut]
            try:
                results[sym] = fut.result(timeout=_INFO_TIMEOUT)
            except Exception as e:
                log_error("value", f"{sym}: {e}")
                results[sym] = _empty()
            done_count += 1
            if done_count % 10 == 0:
                logger.info(f"价值因子进度: {done_count}/{len(symbols)}")
    logger.info(f"价值因子完成: {len(results)} 只")
    return results


def _normalize(val: float, low: float, high: float, cap: float = 0.05) -> float:
    """正向归一化：值越大分越高"""
    if high <= low:
        return 0.5
    raw = (val - low) / (high - low)
    return max(cap, min(1.0 - cap, raw))


def _normalize_inverse(val: float, low: float, high: float, cap: float = 0.05) -> float:
    """反向归一化：值越小分越高"""
    return 1.0 - _normalize(val, low, high, cap)


def _empty() -> dict:
    return {"symbol": "?", "sector": "Unknown", "composite": 0.0, "scores": {}}
