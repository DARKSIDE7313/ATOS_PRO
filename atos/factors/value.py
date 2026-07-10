"""
ATOS PRO v2 — 价值因子
=======================
计算估值相关因子：P/E、P/B、P/S、股息率、EV/EBITDA。
数据源：yfinance (stock.info)
"""
import yfinance as yf
from atos.core.logging import get_logger, log_error
import concurrent.futures
import threading
import socket

logger = get_logger("factors.value")

# 🆕 全局 socket 超时 — 防止 yfinance HTTP 请求永久卡死
# 这是最关键的防御：requests 库默认无超时，闭市/网络问题时线程永久阻塞
socket.setdefaulttimeout(30)

# yfinance 全局锁 — 防止多线程并发写 SQLite 缓存
_yf_lock = threading.Lock()

# 闭市时段 Yahoo Finance API 经常超时，全局超时控制
_INFO_TIMEOUT = 15             # 单只最多等 15 秒（从 20 收紧）
_BATCH_TOTAL_TIMEOUT = 120     # 整批最多等 120 秒


def get_value_factors(symbol: str) -> dict:
    """
    获取单只股票的价值因子。
    返回归一化得分（0-1，越高越便宜/越有投资价值）。
    """
    try:
        with _yf_lock:
            stock = yf.Ticker(symbol)
        # 加超时防止闭市时段卡死
        info = stock.info or {}
    except Exception as e:
        if "curl" in str(e) or "resolve host" in str(e) or "Recv failure" in str(e):
            logger.warning(f"value {symbol}: yfinance网络波动 — {str(e)[:80]}")
        else:
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
    """批量获取价值因子（并行，带超时保护不卡死）"""
    results = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    try:
        fut_to_sym = {pool.submit(get_value_factors, sym): sym for sym in symbols}
        done_count = 0
        for fut in concurrent.futures.as_completed(fut_to_sym, timeout=_BATCH_TOTAL_TIMEOUT):
            sym = fut_to_sym[fut]
            try:
                results[sym] = fut.result(timeout=_INFO_TIMEOUT)
            except Exception as e:
                log_error("value", f"{sym}: {e}")
                results[sym] = _empty()
            done_count += 1
            if done_count % 10 == 0:
                logger.info(f"价值因子进度: {done_count}/{len(symbols)}")
    except (concurrent.futures.TimeoutError, TimeoutError):
        logger.warning(f"价值因子批次超时 ({_BATCH_TOTAL_TIMEOUT}s)，取消剩余任务")
    finally:
        # 🆕 关键修复：强制关闭线程池，cancel 所有未完成的任务
        pool.shutdown(wait=False, cancel_futures=True)
    logger.info(f"价值因子完成: {len(results)} 只")
    return results


def _normalize(val: float, low: float, high: float, cap: float = 0.05) -> float:
    """正向归一化：值越大分越高。无效数据返回 0.0 避免假性中性分"""
    if high <= low:
        return 0.0  # Fix: 退化数据不参与排名
    raw = (val - low) / (high - low)
    return max(cap, min(1.0 - cap, raw))


def _normalize_inverse(val: float, low: float, high: float, cap: float = 0.05) -> float:
    """反向归一化：值越小分越高"""
    return 1.0 - _normalize(val, low, high, cap)


def _empty() -> dict:
    return {"symbol": "?", "sector": "Unknown", "composite": 0.0, "scores": {}}
