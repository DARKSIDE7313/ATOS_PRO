"""
ATOS PRO v2 — 业绩指标模块
=========================
计算专业量化指标：夏普比率、索提诺比率、最大回撤、卡玛比率、
胜率、盈亏比、Calmar比率、年化收益率、波动率。
"""

import math
from typing import List


def sharpe_ratio(returns: List[float], risk_free_rate: float = 0.05,
                  periods_per_year: int = 252) -> float:
    """
    夏普比率 = (年化收益率 - 无风险利率) / 年化波动率
    returns: 每期收益率列表（如日收益率）
    """
    if len(returns) < 2:
        return 0.0
    avg_return = sum(returns) / len(returns)
    if avg_return == 0:
        return 0.0
    # 年化
    ann_return = avg_return * periods_per_year
    std = _stddev(returns)
    ann_std = std * math.sqrt(periods_per_year)
    if ann_std == 0:
        return 0.0
    return (ann_return - risk_free_rate) / ann_std


def sortino_ratio(returns: List[float], risk_free_rate: float = 0.05,
                   periods_per_year: int = 252) -> float:
    """
    索提诺比率 — 只惩罚下行波动
    """
    if len(returns) < 2:
        return 0.0
    avg_return = sum(returns) / len(returns)
    ann_return = avg_return * periods_per_year
    downside = [r for r in returns if r < 0]
    if not downside:
        return 999.99 if ann_return > risk_free_rate else 0.0
    down_std = _stddev(downside) * math.sqrt(periods_per_year)
    if down_std == 0:
        return 0.0
    return (ann_return - risk_free_rate) / down_std


def max_drawdown(equity_curve: List[float]) -> float:
    """
    最大回撤 — 从峰值到谷底的最大跌幅（百分比）
    equity_curve: 净值序列
    """
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calmar_ratio(returns: List[float], equity_curve: List[float],
                  periods_per_year: int = 252) -> float:
    """
    卡玛比率 = 年化收益率 / 最大回撤
    衡量每承担1%回撤能获得多少收益
    """
    if len(returns) < 2:
        return 0.0
    avg_return = sum(returns) / len(returns)
    ann_return = avg_return * periods_per_year
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return 999.99 if ann_return > 0 else 0.0
    return ann_return / mdd


def win_rate(trades: List[float]) -> float:
    """胜率 — 盈利交易占比"""
    if not trades:
        return 0.0
    wins = [t for t in trades if t > 0]
    return len(wins) / len(trades)


def profit_factor(trades: List[float]) -> float:
    """盈亏比 — 总盈利 / 总亏损"""
    wins = sum(t for t in trades if t > 0)
    losses = abs(sum(t for t in trades if t < 0))
    if losses == 0:
        return 999.99 if wins > 0 else 0.0
    return wins / losses


def annual_return(returns: List[float], periods_per_year: int = 252) -> float:
    """年化收益率"""
    if not returns:
        return 0.0
    avg = sum(returns) / len(returns)
    return avg * periods_per_year


def annual_volatility(returns: List[float], periods_per_year: int = 252) -> float:
    """年化波动率"""
    if len(returns) < 2:
        return 0.0
    return _stddev(returns) * math.sqrt(periods_per_year)


def all_metrics(returns: List[float], equity_curve: List[float] = None,
                risk_free_rate: float = 0.05) -> dict:
    """
    一次性计算所有指标，返回完整报告
    """
    if equity_curve is None:
        equity_curve = _returns_to_equity(returns)

    return {
        "total_trades": len(returns),
        "win_rate": round(win_rate(returns), 4),
        "profit_factor": round(profit_factor(returns), 2),
        "annual_return": round(annual_return(returns), 4),
        "annual_volatility": round(annual_volatility(returns), 4),
        "sharpe_ratio": round(sharpe_ratio(returns, risk_free_rate), 2),
        "sortino_ratio": round(sortino_ratio(returns, risk_free_rate), 2),
        "max_drawdown": round(max_drawdown(equity_curve), 4),
        "calmar_ratio": round(calmar_ratio(returns, equity_curve), 2),
    }


def format_report(metrics: dict) -> str:
    """指标转成可读字符串"""
    labels = {
        "total_trades": "总交易数",
        "win_rate": "胜率",
        "profit_factor": "盈亏比",
        "annual_return": "年化收益",
        "annual_volatility": "年化波动",
        "sharpe_ratio": "夏普比率",
        "sortino_ratio": "索提诺比率",
        "max_drawdown": "最大回撤",
        "calmar_ratio": "卡玛比率",
    }
    lines = ["=" * 40, "ATOS PRO 业绩报告", "=" * 40]
    for key, label in labels.items():
        val = metrics.get(key, "N/A")
        if isinstance(val, float):
            if key in ("win_rate", "annual_return", "annual_volatility", "max_drawdown"):
                lines.append(f"{label:12s}: {val*100:.2f}%")
            else:
                lines.append(f"{label:12s}: {val:.2f}")
        else:
            lines.append(f"{label:12s}: {val}")
    lines.append("=" * 40)
    return "\n".join(lines)


def _stddev(values: List[float]) -> float:
    """计算标准差"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def var_historical(returns: List[float], confidence: float = 0.95) -> float:
    """
    历史 VaR（Value at Risk）。
    95% VaR = 在最坏的 5% 日子里，最大亏损是多少。
    例：VaR=0.02 → "有95%把握，单日亏损不超过2%"
    """
    if len(returns) < 20:
        return 0.0
    sorted_rets = sorted(returns)
    idx = int(len(sorted_rets) * (1 - confidence))
    return abs(sorted_rets[idx])


def var_parametric(returns: List[float], confidence: float = 0.95) -> float:
    """
    参数法 VaR（假设正态分布）。
    更快但依赖于正态假设。
    """
    from scipy.stats import norm
    if len(returns) < 5:
        return 0.0
    mu = sum(returns) / len(returns)
    sigma = _stddev(returns)
    z = norm.ppf(1 - confidence)
    return abs(mu - z * sigma)


def cvar_historical(returns: List[float], confidence: float = 0.95) -> float:
    """
    CVaR（条件 VaR / Expected Shortfall）。
    当亏损超过 VaR 时，平均亏多少。
    比 VaR 更保守——衡量尾部风险。
    """
    if len(returns) < 20:
        return 0.0
    sorted_rets = sorted(returns)
    idx = int(len(sorted_rets) * (1 - confidence))
    tail = sorted_rets[:idx]
    if not tail:
        return 0.0
    return abs(sum(tail) / len(tail))


def stress_test(positions: dict, scenarios: dict = None) -> dict:
    """
    压力测试：在不同历史危机情景下模拟损失。
    positions: {symbol: {qty, price, beta}}
    默认情景：2008金融危机、2020新冠、2022加息
    """
    if scenarios is None:
        scenarios = {
            "2008_金融危机": {"SPY": -0.38, "QQQ": -0.42, "TLT": 0.20, "VIX": 0.80},
            "2020_新冠崩盘": {"SPY": -0.34, "QQQ": -0.30, "TLT": 0.08, "VIX": 0.50},
            "2022_加息年":   {"SPY": -0.19, "QQQ": -0.33, "TLT": -0.13, "VIX": 0.25},
        }

    results = {}
    total_mkt_val = sum(
        p.get("qty", 0) * p.get("last_price", p.get("avg_price", 0))
        for p in positions.values()
    ) if isinstance(positions, dict) else sum(
        p.get("mkt_val", 0) for p in positions
    )

    for name, shocks in scenarios.items():
        estimated_loss = 0.0
        if isinstance(positions, dict):
            for sym, pos in positions.items():
                # 用 beta 估算个股跌幅（beta × 市场跌幅）
                beta = pos.get("beta", 1.0)
                spy_shock = shocks.get("SPY", -0.20)
                est_shock = beta * spy_shock
                mkt_val = pos.get("qty", 0) * pos.get("last_price", pos.get("avg_price", 0))
                estimated_loss += mkt_val * est_shock
        else:
            # positions 是列表格式
            for p in positions:
                beta = p.get("beta", 1.0)
                spy_shock = shocks.get("SPY", -0.20)
                mkt_val = p.get("mkt_val", 0)
                estimated_loss += mkt_val * beta * spy_shock

        pct_loss = estimated_loss / total_mkt_val if total_mkt_val > 0 else 0
        results[name] = {
            "estimated_loss": round(abs(estimated_loss), 2),
            "loss_pct": round(abs(pct_loss), 4),
            "severity": "CRITICAL" if abs(pct_loss) > 0.30 else
                        ("HIGH" if abs(pct_loss) > 0.15 else
                         ("MEDIUM" if abs(pct_loss) > 0.08 else "LOW")),
        }

    return results


def _returns_to_equity(returns: List[float], initial: float = 1.0) -> List[float]:
    """收益率序列转净值曲线"""
    curve = [initial]
    for r in returns:
        curve.append(curve[-1] * (1 + r))
    return curve
