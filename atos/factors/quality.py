"""
ATOS PRO v2 — 质量因子
=======================
计算公司质量：ROE、利润率、负债率、盈利稳定性。
融合 Serenity 瓶颈检测逻辑作为增强质量信号。

2026-06-24 修复：添加 yfinance 全局锁 + 重试逻辑，防止多线程
并发调用导致 SQLite 损坏和 TLS 连接拒绝（Yahoo rate limiting）。
"""
import yfinance as yf
from atos.core.logging import get_logger, log_error
from atos.longterm.serenity import serenity_quality_filter
import concurrent.futures
import threading
import time

logger = get_logger("factors.quality")

# yfinance 全局锁 — 防止多线程并发写 SQLite 缓存
_yf_lock = threading.Lock()

# 质量因子中 Serenity 分数的混合权重
SERENITY_BLEND_WEIGHT = 0.30

# 单标的 yfinance 调用最大重试次数
MAX_QUALITY_RETRIES = 3


def get_quality_factors(symbol: str) -> dict:
    """获取单只股票的质量因子（带 yfinance 锁 + 重试）"""
    for attempt in range(MAX_QUALITY_RETRIES):
        try:
            with _yf_lock:
                stock = yf.Ticker(symbol)
                info = stock.info or {}
            break  # 成功，跳出重试循环
        except Exception as e:
            if attempt < MAX_QUALITY_RETRIES - 1:
                logger.debug(f"{symbol} quality retry {attempt+1}/{MAX_QUALITY_RETRIES}: {e}")
                time.sleep(1.0 * (attempt + 1))
            else:
                log_error("quality", f"{symbol}: {e}")
                return _empty()

    roe = info.get("returnOnEquity")
    profit_margin = info.get("profitMargins")
    debt_to_equity = info.get("debtToEquity")
    current_ratio = info.get("currentRatio")
    operating_margins = info.get("operatingMargins")
    free_cashflow = info.get("freeCashflow")
    revenue_growth = info.get("revenueGrowth")

    raw = {
        "symbol": symbol,
        "roe": round(roe, 4) if roe else None,
        "profit_margin": round(profit_margin, 4) if profit_margin else None,
        "debt_to_equity": round(debt_to_equity, 2) if debt_to_equity else None,
        "current_ratio": round(current_ratio, 2) if current_ratio else None,
        "operating_margins": round(operating_margins, 4) if operating_margins else None,
    }

    scores = {}
    # ROE: 越高越好（15%-40%最优）
    if roe and roe > 0:
        scores["roe_score"] = _normalize(roe, 0.05, 0.40, 0.15)
    # 利润率: 越高越好
    if profit_margin and profit_margin > 0:
        scores["margin_score"] = _normalize(profit_margin, 0.05, 0.50, 0.15)
    # 负债率: 越低越好
    if debt_to_equity and debt_to_equity > 0:
        scores["debt_score"] = _normalize_inverse(debt_to_equity, 0, 200, 0.15)
    # 流动比率: 1.5-3最佳
    if current_ratio and current_ratio > 0:
        if 1.5 <= current_ratio <= 3.0:
            scores["liquidity_score"] = 0.8
        elif current_ratio > 0.5:
            scores["liquidity_score"] = 0.4
        else:
            scores["liquidity_score"] = 0.1
    # 经营利润率
    if operating_margins and operating_margins > 0:
        scores["opmargin_score"] = _normalize(operating_margins, 0.05, 0.40, 0.15)

    composite = sum(scores.values()) / len(scores) if scores else 0.0  # v5: 无数据→0.0

    return {
        **raw,
        "scores": scores,
        "composite": round(composite, 4),
    }


def batch_quality_factors(symbols: list[str]) -> dict:
    """
    批量获取质量因子，融合 Serenity 瓶颈检测质量评分。

    最终 composite = (1 - SERENITY_BLEND_WEIGHT) * 传统质量得分
                    + SERENITY_BLEND_WEIGHT * Serenity 瓶颈得分
    """
    # 1) 传统质量因子（并行）
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fut_to_sym = {pool.submit(get_quality_factors, sym): sym for sym in symbols}
        for fut in concurrent.futures.as_completed(fut_to_sym, timeout=60):
            sym = fut_to_sym[fut]
            try:
                results[sym] = fut.result()
            except Exception as e:
                logger.warning(f"{sym} 质量因子并行超时: {e}")
                results[sym] = {"composite": 0.0}

    # 2) Serenity 瓶颈质量评分
    try:
        from atos.longterm.serenity import serenity_quality_filter
        serenity_scores = serenity_quality_filter(symbols)
    except Exception:
        logger.warning("Serenity quality filter unavailable — skipping")
        serenity_scores = {}

    # 3) 融合
    blend = SERENITY_BLEND_WEIGHT
    for sym in symbols:
        if sym in results:
            q_comp = results[sym].get("composite", 0.5)
            s_comp = serenity_scores.get(sym, {}).get("composite", 0.3)
            blended = (1 - blend) * q_comp + blend * s_comp
            results[sym]["composite"] = round(blended, 4)
            results[sym]["serenity_composite"] = round(s_comp, 4)
            results[sym]["serenity_decision"] = serenity_scores.get(sym, {}).get("decision", "PASS")

    logger.info(f"质量因子完成（含Serenity融合）: {len(results)} 只")
    return results


def _normalize(val, low, high, cap=0.05):
    raw = (val - low) / (high - low)
    return max(cap, min(1.0 - cap, raw))


def _normalize_inverse(val, low, high, cap=0.05):
    return 1.0 - _normalize(val, low, high, cap)


def _empty():
    return {"symbol": "?", "composite": 0.0, "scores": {}}
