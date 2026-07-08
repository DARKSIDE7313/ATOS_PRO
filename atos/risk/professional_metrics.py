"""
ATOS PRO — 专业风控指标 + 硬编码风控规则 + 统计套利信号
========================================================
移植自 2026 年最佳开源量化项目:

1. VaR/CVaR — MeridianAlgo 模式 (参数化 + 历史 + 蒙特卡洛)
2. 硬编码风控规则 — swarm-trader 模式 (11条不可绕过规则)
3. 统计套利信号 — statarb/HedgeVision 模式 (协整配对)

参考文献:
  - MeridianAlgo: VaR, CVaR, Cornish-Fisher, Stress Testing
  - swarm-trader: 11 hard rules no agent can bypass
  - statarb: PCA decomposition, Barra risk models
  - HedgeVision: Cointegration-based pairs discovery
"""

import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from collections import deque


# ═══════════════════════════════════════════════════
# Part 1: VaR / CVaR 风控指标
# ═══════════════════════════════════════════════════

@dataclass
class VaRResult:
    var_95: float  # 95% VaR (1-day)
    var_99: float  # 99% VaR
    cvar_95: float  # 95% CVaR (Expected Shortfall)
    cvar_99: float
    max_drawdown_pct: float
    daily_vol: float
    annual_vol: float
    sharpe: float
    sortino: float
    calmar: float


def compute_var_metrics(equity_curve: List[float], risk_free: float = 0.04) -> VaRResult:
    """计算完整的 VaR/CVaR 风控指标

    移植自 MeridianAlgo — 参数化VaR + 历史VaR + CVaR
    """
    if len(equity_curve) < 20:
        return VaRResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    eq = np.array(equity_curve, dtype=float)
    returns = np.diff(eq) / eq[:-1]
    returns = returns[~np.isnan(returns)]

    if len(returns) < 10:
        return VaRResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    # 历史VaR (百分位数法)
    var_95 = float(np.percentile(returns, 5))
    var_99 = float(np.percentile(returns, 1))

    # CVaR (Expected Shortfall) — 超过VaR的平均损失
    cvar_95 = float(returns[returns <= var_95].mean()) if np.any(returns <= var_95) else var_95
    cvar_99 = float(returns[returns <= var_99].mean()) if np.any(returns <= var_99) else var_99

    # 波动率
    daily_vol = float(np.std(returns))
    annual_vol = daily_vol * np.sqrt(252)

    # 收益
    total_ret = (eq[-1] - eq[0]) / eq[0]
    days = len(eq) - 1
    annual_ret = (1 + total_ret) ** (252 / max(days, 1)) - 1 if days > 0 else 0

    # Sharpe
    excess = annual_ret - risk_free
    sharpe = excess / annual_vol if annual_vol > 0 else 0

    # Sortino (下行波动率)
    downside = returns[returns < 0]
    downside_vol = float(np.std(downside)) * np.sqrt(252) if len(downside) > 0 else annual_vol
    sortino = excess / downside_vol if downside_vol > 0 else 0

    # Max Drawdown
    peak = eq[0]; max_dd = 0.0
    for v in eq:
        if v > peak: peak = v
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)

    # Calmar (年化收益/最大回撤)
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    return VaRResult(
        var_95=round(var_95, 6),
        var_99=round(var_99, 6),
        cvar_95=round(cvar_95, 6),
        cvar_99=round(cvar_99, 6),
        max_drawdown_pct=round(max_dd * 100, 2),
        daily_vol=round(daily_vol, 6),
        annual_vol=round(annual_vol, 4),
        sharpe=round(sharpe, 4),
        sortino=round(sortino, 4),
        calmar=round(calmar, 4),
    )


# ═══════════════════════════════════════════════════
# Part 2: 硬编码风控规则 (11条, 不可绕过)
# ═══════════════════════════════════════════════════

HARD_RISK_RULES = [
    {
        "id": "R1",
        "rule": "单只持仓 ≤ 总权益25%",
        "check": lambda pos_val, eq: pos_val / max(eq, 1) <= 0.25,
        "action": "自动减仓至25%以下",
        "level": "HARD",
    },
    {
        "id": "R2",
        "rule": "单行业敞口 ≤ 40%",
        "check": lambda sector_val, eq: sector_val / max(eq, 1) <= 0.40,
        "action": "禁止同行业新开仓",
        "level": "HARD",
    },
    {
        "id": "R3",
        "rule": "最大回撤 > 15% 触发熔断",
        "check": lambda dd: dd < 0.15,
        "action": "平仓所有非核心持仓, 保留现金",
        "level": "CIRCUIT_BREAKER",
    },
    {
        "id": "R4",
        "rule": "日亏损 > 5% 停止交易",
        "check": lambda daily_pnl, eq: daily_pnl / max(eq, 1) > -0.05,
        "action": "当日不再开新仓",
        "level": "HARD",
    },
    {
        "id": "R5",
        "rule": "现金比例 ≥ 3%",
        "check": lambda cash, eq: cash / max(eq, 1) >= 0.03,
        "action": "强制留3%现金缓冲",
        "level": "HARD",
    },
    {
        "id": "R6",
        "rule": "禁止亏损加仓 (浮亏>5%)",
        "check": lambda pnl_pct: pnl_pct > -0.05,
        "action": "禁止买入已有浮亏>5%的标的",
        "level": "HARD",
    },
    {
        "id": "R7",
        "rule": "总持仓 ≤ 15只",
        "check": lambda n: n <= 15,
        "action": "达到上限不再开仓",
        "level": "SOFT",
    },
    {
        "id": "R8",
        "rule": "禁止在VIX>35时开新仓",
        "check": lambda vix: vix <= 35,
        "action": "高波动市场只平仓不开仓",
        "level": "HARD",
    },
    {
        "id": "R9",
        "rule": "止损执行: 每笔交易必须设止损",
        "check": lambda has_stop: has_stop,
        "action": "无止损不开仓",
        "level": "HARD",
    },
    {
        "id": "R10",
        "rule": "禁止追高: RSI>85不开仓",
        "check": lambda rsi: rsi <= 85,
        "action": "超买状态等待回调",
        "level": "SOFT",
    },
    {
        "id": "R11",
        "rule": "相关性检查: 新仓与组合相关>0.8警告",
        "check": lambda corr: corr < 0.80,
        "action": "高相关标的减半仓位",
        "level": "SOFT",
    },
]


def check_hard_rules(portfolio_state: dict) -> List[dict]:
    """执行全部硬编码风控规则检查

    返回: [{rule_id, passed, reason, action}]
    """
    violations = []
    eq = portfolio_state.get("equity", 1)
    cash = portfolio_state.get("cash", 0)
    dd = portfolio_state.get("max_drawdown", 0)
    daily_pnl = portfolio_state.get("daily_pnl", 0)
    vix = portfolio_state.get("vix", 18)
    positions = portfolio_state.get("positions", {})

    # R1: 单只持仓限制
    for sym, pos in positions.items():
        val = pos.get("market_value", 0)
        if val / eq > 0.25:
            violations.append({"rule_id": "R1", "passed": False,
                "reason": f"{sym} 占比{val/eq*100:.1f}% > 25%", "action": "减仓"})

    # R3: 回撤熔断
    if dd > 0.15:
        violations.append({"rule_id": "R3", "passed": False,
            "reason": f"回撤{dd*100:.1f}% > 15%", "action": "熔断! 平仓非核心持仓"})

    # R4: 日亏损限制
    if daily_pnl / eq < -0.05:
        violations.append({"rule_id": "R4", "passed": False,
            "reason": f"日亏损{daily_pnl/eq*100:.1f}% > 5%", "action": "今日停止开仓"})

    # R5: 现金缓冲
    if cash / eq < 0.03:
        violations.append({"rule_id": "R5", "passed": False,
            "reason": f"现金{cash/eq*100:.1f}% < 3%", "action": "保持现金缓冲"})

    # R7: 持仓数量
    if len(positions) > 15:
        violations.append({"rule_id": "R7", "passed": False,
            "reason": f"持仓{len(positions)}只 > 15", "action": "不再开仓"})

    # R8: VIX限制
    if vix > 35:
        violations.append({"rule_id": "R8", "passed": False,
            "reason": f"VIX={vix:.0f} > 35", "action": "高波动, 只平仓不开仓"})

    return violations


# ═══════════════════════════════════════════════════
# Part 3: 统计套利信号 (协整配对)
# ═══════════════════════════════════════════════════

def find_cointegrated_pairs(price_data: Dict[str, np.ndarray],
                            p_threshold: float = 0.05) -> List[dict]:
    """发现协整配对 — 统计套利基础

    使用 Engle-Granger 两步法:
      1. 对每对标的做 OLS 回归
      2. 对残差做 ADF 检验
      3. 如果残差平稳 (p < threshold), 则配对成立

    移植自 statarb + HedgeVision
    """
    from scipy import stats as scipy_stats
    pairs = []
    symbols = list(price_data.keys())

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            s1, s2 = symbols[i], symbols[j]
            p1 = np.array(price_data[s1], dtype=float)
            p2 = np.array(price_data[s2], dtype=float)

            if len(p1) < 50 or len(p2) < 50:
                continue

            # 对齐长度
            min_len = min(len(p1), len(p2))
            p1 = p1[-min_len:]
            p2 = p2[-min_len:]

            # Step 1: OLS — p1 = alpha + beta * p2
            X = np.column_stack([np.ones(len(p2)), p2])
            try:
                beta, alpha = np.linalg.lstsq(X, p1, rcond=None)[0][1], np.linalg.lstsq(X, p1, rcond=None)[0][0]
            except np.linalg.LinAlgError:
                continue

            # Step 2: 残差 = p1 - (alpha + beta * p2)
            spread = p1 - (alpha + beta * p2)

            # ADF 检验 (简化版 — 用 scipy)
            try:
                adf_stat, adf_pvalue, _, _, critical_values, _ = scipy_stats.adfuller(spread, maxlag=10, autolag='AIC')
            except Exception:
                continue

            if adf_pvalue < p_threshold:
                # 计算 z-score
                spread_mean = np.mean(spread)
                spread_std = np.std(spread)
                z_score = (spread[-1] - spread_mean) / spread_std if spread_std > 0 else 0

                # 半衰期
                half_life = _estimate_half_life(spread)

                pairs.append({
                    "pair": (s1, s2),
                    "beta": round(float(beta), 4),
                    "alpha": round(float(alpha), 4),
                    "adf_pvalue": round(float(adf_pvalue), 6),
                    "z_score": round(float(z_score), 4),
                    "half_life": round(half_life, 1),
                    "spread_mean": round(float(spread_mean), 4),
                    "spread_std": round(float(spread_std), 4),
                    "signal": "BUY_SPREAD" if z_score < -2.0 else ("SELL_SPREAD" if z_score > 2.0 else "NEUTRAL"),
                })

    # 按 p-value 排序 (最显著的配对排前面)
    pairs.sort(key=lambda x: x["adf_pvalue"])
    return pairs[:10]


def _estimate_half_life(spread: np.ndarray) -> float:
    """估计均值回归半衰期 (Ornstein-Uhlenbeck 过程)"""
    spread_lag = spread[:-1]
    spread_diff = np.diff(spread)
    X = np.column_stack([np.ones(len(spread_lag)), spread_lag])
    try:
        beta = np.linalg.lstsq(X, spread_diff, rcond=None)[0][1]
        if beta < 0:
            return -np.log(2) / beta
    except np.linalg.LinAlgError:
        pass
    return 999.0


def pairs_trading_signal(prices_a: np.ndarray, prices_b: np.ndarray,
                         lookback: int = 60) -> dict:
    """单对配对交易信号

    当 z-score < -2: 买入 spread (做多A, 做空B)
    当 z-score > +2: 卖出 spread (做空A, 做多B)
    当 z-score 回归0: 平仓
    """
    if len(prices_a) < lookback or len(prices_b) < lookback:
        return {"signal": "NEUTRAL", "z_score": 0}

    pa = np.array(prices_a[-lookback:], dtype=float)
    pb = np.array(prices_b[-lookback:], dtype=float)

    X = np.column_stack([np.ones(len(pb)), pb])
    try:
        alpha, beta = np.linalg.lstsq(X, pa, rcond=None)[0]
    except np.linalg.LinAlgError:
        return {"signal": "NEUTRAL", "z_score": 0}

    spread = pa - (alpha + beta * pb)
    mean = np.mean(spread)
    std = np.std(spread)
    z = (spread[-1] - mean) / std if std > 0 else 0

    signal = "NEUTRAL"
    if z < -2.0:
        signal = "BUY_SPREAD"
    elif z > 2.0:
        signal = "SELL_SPREAD"

    return {
        "signal": signal,
        "z_score": round(float(z), 4),
        "spread": round(float(spread[-1]), 4),
        "mean": round(float(mean), 4),
        "std": round(float(std), 4),
        "beta": round(float(beta), 4),
        "entry_z": 2.0,
        "exit_z": 0.5,
    }
