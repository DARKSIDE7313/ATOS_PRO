"""
ATOS PRO v2 — 相关性矩阵
=========================
计算持仓之间和历史数据的价格相关性。
高相关性 → 同涨同跌 → 虚假分散 → 实际风险集中。
触发相关性告警时，建议降低其中一方的仓位。

数据源：yfinance（历史价格）
完全独立于 FutuOpenD，不增加额外 API 负担。
"""

import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("portfolio.correlation")


def get_correlation_matrix(symbols: list[str],
                            period: str = "6mo") -> dict:
    """
    计算一组标的的价格相关性矩阵。
    返回 {symbol: {other_symbol: correlation, ...}, ...}
    """
    if len(symbols) < 2:
        return {}

    # 批量下载
    data = {}
    for sym in symbols:
        try:
            df = yf.download(sym, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 20:
                continue
            data[sym] = df["Close"].squeeze()
        except Exception as e:
            logger.debug(f"相关性数据获取失败 {sym}: {e}")

    if len(data) < 2:
        return {}

    # 对齐日期
    prices = pd.DataFrame(data).dropna()
    if len(prices) < 20:
        return {}

    # 日收益率
    returns = prices.pct_change().dropna()

    # 相关性矩阵
    corr_matrix = returns.corr()

    result = {}
    for sym1 in symbols:
        if sym1 not in corr_matrix.columns:
            continue
        result[sym1] = {}
        for sym2 in symbols:
            if sym2 not in corr_matrix.columns or sym1 == sym2:
                continue
            result[sym1][sym2] = round(float(corr_matrix.loc[sym1, sym2]), 3)

    return result


def check_concentration_risk(positions: list[dict],
                              correlation_threshold: float = 0.70) -> list[dict]:
    """
    检查持仓中是否存在高相关性（虚假分散）。
    返回需要关注的配对列表，包含减持建议。
    """
    symbols = [p["symbol"] for p in positions if p.get("symbol")]
    if len(symbols) < 2:
        return []

    corr = get_correlation_matrix(symbols)
    alerts = []

    # 计算总资产用于百分比
    total_equity = sum(p.get("mkt_val", 0) for p in positions)

    seen = set()
    for sym1, correlations in corr.items():
        for sym2, corr_val in correlations.items():
            pair = tuple(sorted([sym1, sym2]))
            if pair in seen:
                continue
            seen.add(pair)

            if abs(corr_val) >= correlation_threshold:
                # 找到两个持仓的实际权重
                p1 = next((p for p in positions if p["symbol"] == sym1), None)
                p2 = next((p for p in positions if p["symbol"] == sym2), None)
                mkt1 = p1.get("mkt_val", 0) if p1 else 0
                mkt2 = p2.get("mkt_val", 0) if p2 else 0
                total_value = mkt1 + mkt2

                # 确定减持对象（两者中市值较小的）
                smaller_sym = sym1 if mkt1 <= mkt2 else sym2
                smaller_val = min(mkt1, mkt2)
                reduction_pct = 0.30  # 建议减持 30%
                reduction_amount = round(smaller_val * reduction_pct, 2)

                combined_pct = round((total_value / total_equity * 100), 2) if total_equity > 0 else 0.0

                alerts.append({
                    "pair": [sym1, sym2],
                    "correlation": corr_val,
                    "severity": "HIGH" if abs(corr_val) > 0.85 else "MEDIUM",
                    "suggestion": (
                        f"{sym1}-{sym2} 相关性 {corr_val:.1%}，"
                        f"合计权重 {combined_pct:.1f}%，建议减持其中一只"
                    ),
                    "combined_weight_pct": combined_pct,
                    "reduce_symbol": smaller_sym,
                    "reduce_amount": reduction_amount,
                })
                logger.warning(
                    f"高相关性告警: {sym1}-{sym2} = {corr_val:.1%} | "
                    f"建议减持 {smaller_sym} ${reduction_amount:,.0f}"
                )

    return sorted(alerts, key=lambda a: abs(a["correlation"]), reverse=True)


def auto_reduce_correlation(positions: list[dict], corr_matrix: dict,
                             threshold: float = 0.70) -> list[dict]:
    """
    自动识别高相关性持仓并生成减持建议。

    对每一对相关系数 > threshold 的标的：
      - 标记其中市值较小的持仓进行减持
      - 建议减持当前市值的 30%

    参数:
        positions: 当前持仓 [{symbol, mkt_val, ...}, ...]
        corr_matrix: 相关性矩阵（由 get_correlation_matrix 返回）
        threshold: 相关系数阈值

    返回:
        [{symbol, current_val, suggested_reduction, reason}, ...]
    """
    if len(positions) < 2 or not corr_matrix:
        return []

    # 构建快速 lookup
    val_map = {}
    for p in positions:
        sym = p.get("symbol", "")
        if sym:
            val_map[sym] = p.get("mkt_val", 0)

    seen = set()
    reductions = {}

    for sym1, corrs in corr_matrix.items():
        for sym2, corr_val in corrs.items():
            if abs(corr_val) <= threshold:
                continue
            pair = tuple(sorted([sym1, sym2]))
            if pair in seen:
                continue
            seen.add(pair)

            mkt1 = val_map.get(sym1, 0)
            mkt2 = val_map.get(sym2, 0)

            # 标记市值较小的那个
            smaller = sym1 if mkt1 <= mkt2 else sym2
            smaller_val = min(mkt1, mkt2)

            if smaller_val <= 0:
                continue

            # 建议减持 30%
            reduction = round(smaller_val * 0.30, 2)

            if smaller not in reductions or reduction > reductions[smaller]["suggested_reduction"]:
                reductions[smaller] = {
                    "symbol": smaller,
                    "current_val": smaller_val,
                    "suggested_reduction": reduction,
                    "reason": (
                        f"高相关对 {sym1}({mkt1:,.0f})-{sym2}({mkt2:,.0f}) "
                        f"corr={corr_val:.2f}，{smaller} 市值较小建议减持"
                    ),
                }

    result = list(reductions.values())
    result.sort(key=lambda x: x["suggested_reduction"], reverse=True)
    logger.info(f"相关性自动减持建议: {len(result)} 只标的")
    return result


def get_sector_exposure(positions: list[dict],
                         sector_map: dict[str, str]) -> dict[str, float]:
    """
    计算各行业的敞口占比。
    sector_map: {symbol: sector_name}
    """
    total_val = sum(p.get("mkt_val", 0) for p in positions)
    if total_val == 0:
        return {}

    exposure = {}
    for p in positions:
        sym = p.get("symbol", "")
        sector = sector_map.get(sym, "Unknown")
        exposure[sector] = exposure.get(sector, 0) + p.get("mkt_val", 0)

    return {k: round(v / total_val, 4) for k, v in exposure.items()}


# 预定义的行业映射（基于 GICS）
SECTOR_MAP = {
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "GOOGL": "Tech",
    "META": "Tech", "AMD": "Tech", "INTC": "Tech", "AVGO": "Tech",
    "QCOM": "Tech", "TXN": "Tech", "MU": "Tech", "AMAT": "Tech",
    "AMZN": "Consumer", "TSLA": "Consumer", "COST": "Consumer",
    "WMT": "Consumer", "HD": "Consumer", "NKE": "Consumer",
    "SBUX": "Consumer", "MCD": "Consumer", "DIS": "Consumer",
    "JPM": "Financial", "BAC": "Financial", "GS": "Financial",
    "MS": "Financial", "V": "Financial", "MA": "Financial",
    "BLK": "Financial", "SCHW": "Financial",
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "TMO": "Healthcare",
    "DHR": "Healthcare",
    "CAT": "Industrial", "BA": "Industrial", "GE": "Industrial",
    "HON": "Industrial", "UPS": "Industrial",
    "XOM": "Energy", "CVX": "Energy",
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF",
    "TLT": "Bond", "GLD": "Commodity", "SLV": "Commodity", "USO": "Commodity",
}
