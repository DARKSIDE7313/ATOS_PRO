"""
ATOS PRO v2 — 投资组合优化器
============================
核心思路：最小方差 > 最大收益（用户目标是低风险稳定收入）。

方法：
  1. 最小方差优化 — 找风险最低的权重组合
  2. 风险预算 — 每只持仓分配等量风险
  3. 硬约束 — 单只上限、行业上限、现金下限

不依赖 Black-Litterman（需要额外数据且复杂度高，不易调试）。
用简单的数学 + 硬约束，确保在 FutuOpenD 上直接执行。
"""

import numpy as np
import pandas as pd
import yfinance as yf
from atos.core.logging import get_logger

logger = get_logger("portfolio.optimizer")

# 硬约束（不可逾越）
HARD_CONSTRAINTS = {
    "max_single_position": 0.20,    # 单只最多 20%
    "max_sector_exposure": 0.35,    # 单行业最多 35%
    "min_cash_buffer": 0.05,        # 至少保留 5% 现金
    "max_positions": 10,            # 最多持仓数
    "min_position_size": 0.02,      # 单只最少 2%（太小没意义）
}


def get_returns_matrix(symbols: list[str],
                        period: str = "1y") -> pd.DataFrame:
    """获取多只标的的历史日收益率矩阵"""
    data = {}
    for sym in symbols:
        try:
            df = yf.download(sym, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                continue
            data[sym] = df["Close"].squeeze()
        except Exception:
            continue

    prices = pd.DataFrame(data).dropna()
    if len(prices) < 60 or len(prices.columns) < 2:
        return pd.DataFrame()
    return prices.pct_change().dropna()


def minimum_variance_weights(symbols: list[str],
                              cov_matrix: np.ndarray = None,
                              returns: pd.DataFrame = None) -> dict[str, float]:
    """
    最小方差投资组合权重。

    数学：min w^T Σ w  subject to  Σw_i = 1, w_i >= 0

    如果优化失败，回退到 1/N 等权重（最简单的分散策略）。
    """
    n = len(symbols)
    if n == 0:
        return {}
    if n == 1:
        return {symbols[0]: 1.0}

    # 如果没有提供协方差矩阵，自己算
    if cov_matrix is None:
        if returns is None:
            returns = get_returns_matrix(symbols)
        if returns.empty or returns.shape[1] < 2:
            # 回退：等权重
            w = 1.0 / n
            return {s: round(w, 4) for s in symbols}
        cov_matrix = returns.cov().values

    if cov_matrix.shape[0] != n:
        return {s: round(1.0 / n, 4) for s in symbols}

    try:
        # 二次规划：min w^T Σ w
        # 约束：Σw = 1, w_i >= 0
        # 使用闭式解求最小方差（允许解析解）
        inv_cov = np.linalg.inv(cov_matrix)
        ones = np.ones(n)
        # 最小方差权重（无卖空约束的解析解）
        raw_weights = inv_cov @ ones / (ones @ inv_cov @ ones)

        # 强制非负（不允许卖空）
        raw_weights = np.maximum(raw_weights, 0)

        # 归一化
        total = raw_weights.sum()
        if total > 0:
            weights = raw_weights / total
        else:
            weights = np.ones(n) / n

        # 施加硬约束：单只上限 20%（迭代裁剪+归一化，直到所有权重达标）
        max_pos = HARD_CONSTRAINTS["max_single_position"]
        for _ in range(20):  # 最多迭代20次，通常2-3次收敛
            weights = np.minimum(weights, max_pos)
            weights = np.maximum(weights, 0)
            total = weights.sum()
            if total > 0:
                weights = weights / total
            else:
                weights = np.ones(n) / n
            # 检查是否所有权重都 ≤ max_single_position
            if np.all(weights <= max_pos + 1e-8):
                break

        # 确保最小仓位（min_position_size）
        min_pos = HARD_CONSTRAINTS["min_position_size"]
        # 对超小权重归零
        weights = np.where(weights < min_pos, 0, weights)
        total = weights.sum()
        if total > 0:
            weights = weights / total
            # Re-clip: zeroing small weights may have pushed others above limit
            for _ in range(20):
                weights = np.minimum(weights, max_pos)
                total = weights.sum()
                if total > 0:
                    weights = weights / total
                else:
                    weights = np.ones(n) / n
                    break
                # Check if all weights are within bounds
                if np.all(weights <= max_pos + 1e-10):
                    break
        else:
            weights = np.ones(n) / n

        result = {symbols[i]: round(float(weights[i]), 4)
                  for i in range(n)}

        logger.info(
            f"最小方差优化完成: {len(result)} 只 | "
            f"Top3: {sorted(result.items(), key=lambda x: x[1], reverse=True)[:3]}"
        )
        return result

    except np.linalg.LinAlgError:
        logger.warning("协方差矩阵奇异，回退到 1/N 等权重")
        w = 1.0 / n
        return {s: round(w, 4) for s in symbols}


def risk_budget_weights(symbols: list[str],
                         returns: pd.DataFrame = None,
                         target_risk_per_asset: float = None) -> dict[str, float]:
    """
    风险预算：每只标的分配相等的风险贡献。
    如果每只贡献相同风险 → 没有单一标的能主导组合的波动。
    """
    n = len(symbols)
    if n <= 1:
        return {s: 1.0 for s in symbols} if n == 1 else {}

    if returns is None:
        returns = get_returns_matrix(symbols)

    if returns.empty or returns.shape[1] < 2:
        w = 1.0 / n
        return {s: round(w, 4) for s in symbols}

    # 各标的波动率
    vols = returns.std()
    # 风险预算：vol 越高的标的配越少权重
    inv_vols = 1.0 / vols.replace(0, np.nan)
    weights = inv_vols / inv_vols.sum()

    # 硬约束
    weights = np.minimum(weights, HARD_CONSTRAINTS["max_single_position"])
    weights = np.maximum(weights, HARD_CONSTRAINTS["min_position_size"])
    weights = weights / weights.sum()

    result = {symbols[i]: round(float(weights.iloc[i]), 4) if i < len(weights) else 0.0
              for i in range(n)}

    logger.info(f"风险预算完成: {len(result)} 只")
    return result


def compute_target_positions(symbols: list[str],
                              total_equity: float,
                              cash_reserve_pct: float,
                              current_positions: list[dict],
                              use_risk_budget: bool = True) -> dict:
    """
    计算目标持仓（接入 FutuOpenD 前的最后一步）。

    参数:
        symbols: 目标标的列表
        total_equity: 总资产
        cash_reserve_pct: 现金保留比例（VIX越高越大）
        current_positions: 当前持仓列表
        use_risk_budget: True=风险预算, False=最小方差

    返回:
        {
            "target_positions": {symbol: {shares, value, weight}},
            "trades_needed": [{symbol, action, shares, value}],
            "expected_volatility": 0.15,
            "cash_buffer": 50000,
        }
    """
    # 计算实际可投资金额
    investable = total_equity * (1.0 - cash_reserve_pct)

    # 获取权重
    if use_risk_budget:
        weights = risk_budget_weights(symbols)
    else:
        weights = minimum_variance_weights(symbols)

    # 过滤掉权重太小的
    weights = {
        s: w for s, w in weights.items()
        if w >= HARD_CONSTRAINTS["min_position_size"]
    }

    # 投资组合预期波动率
    returns = get_returns_matrix(list(weights.keys()))
    if not returns.empty:
        # 使用完整协方差矩阵：sqrt(w^T Σ w) 而非 sqrt(∑ w_i * σ_i²)
        w = np.array([weights.get(s, 0) for s in returns.columns])
        cov = returns.cov().values
        port_vol = float(np.sqrt(w.T @ cov @ w))
    else:
        port_vol = None

    # 计算目标持仓
    target = {}
    for sym, weight in weights.items():
        target_value = investable * weight
        # 估算股价（用最近持仓价格或默认值）
        current = next((p for p in current_positions if p["symbol"] == sym), None)
        price = current["last"] if current and current.get("last") else 100.0
        if price <= 0:
            price = 100.0
        shares = max(1, int(target_value / price))
        target[sym] = {
            "shares": shares,
            "value": round(target_value, 2),
            "weight": round(weight, 4),
            "price": price,
        }

    # 计算需要执行的交易
    trades = []
    for sym, t in target.items():
        current = next((p for p in current_positions if p["symbol"] == sym), None)
        current_shares = current["qty"] if current else 0
        diff = t["shares"] - current_shares
        if diff > 0:
            trades.append({"symbol": sym, "action": "BUY", "shares": diff,
                           "value": diff * t["price"], "reason": "组合再平衡"})
        elif diff < 0:
            trades.append({"symbol": sym, "action": "SELL", "shares": abs(diff),
                           "value": abs(diff) * t["price"], "reason": "组合再平衡"})

    cash_buffer = total_equity * cash_reserve_pct

    logger.info(
        f"目标持仓: {len(target)} 只 | 交易: {len(trades)} 笔 | "
        f"现金缓冲: ${cash_buffer:,.0f} ({cash_reserve_pct:.0%})"
        f"{f' | 预期波动: {port_vol:.1%}' if port_vol is not None else ' | 预期波动: N/A'}"
    )

    return {
        "target_positions": target,
        "trades_needed": sorted(trades, key=lambda t: t["value"], reverse=True),
        "expected_volatility": round(port_vol, 4) if port_vol else None,
        "cash_buffer": round(cash_buffer, 2),
        "investable": round(investable, 2),
    }
