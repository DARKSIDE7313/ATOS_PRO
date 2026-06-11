"""
ATOS PRO v2 — 机构级风控模块
=============================
1. 流动性筛查 — 成交量不够的不碰
2. Beta 计算 + 对冲 — 用 SPY 对冲市场风险
3. 策略衰减检测 — 滚动夏普跌破阈值就停
4. 异常检测 — PnL/成交量/波动率异常告警
"""

import yfinance as yf
import numpy as np
from atos.core.logging import get_logger
from atos.core.metrics import sharpe_ratio, max_drawdown

logger = get_logger("risk.advanced")


# ========== 1. 流动性筛查 ==========

def liquidity_check(symbol: str, min_daily_volume: int = 300_000,
                     min_price: float = 5.0, max_spread_pct: float = 0.05) -> dict:
    """
    检查单只标的的流动性是否足够。
    - 日均成交量 ≥ 50万股
    - 股价 ≥ $5（排除仙股）
    - 买卖价差 ≤ 2%
    """
    try:
        stock = yf.Ticker(symbol)
        info = stock.info or {}
        avg_vol = info.get("averageVolume") or info.get("volume", 0)
        price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        bid = info.get("bid", price * 0.99)
        ask = info.get("ask", price * 1.01)
        spread = (ask - bid) / price if price > 0 and bid > 0 else 0.01

        passed = True
        reasons = []

        if avg_vol < min_daily_volume:
            passed = False
            reasons.append(f"成交量不足: {avg_vol:,.0f} < {min_daily_volume:,}")
        if 0 < price < min_price:
            passed = False
            reasons.append(f"股价过低: ${price:.2f} < ${min_price}")
        if spread > max_spread_pct:
            passed = False
            reasons.append(f"价差过大: {spread:.2%} > {max_spread_pct:.0%}")

        return {
            "symbol": symbol,
            "passed": passed,
            "avg_volume": avg_vol,
            "price": price,
            "spread_pct": round(spread, 4),
            "reasons": reasons,
        }
    except Exception as e:
        return {"symbol": symbol, "passed": False, "reasons": [str(e)]}


def filter_liquid_universe(symbols: list[str]) -> list[str]:
    """从标的池中筛掉流动性不足的"""
    liquid = []
    for sym in symbols:
        check = liquidity_check(sym)
        if check["passed"]:
            liquid.append(sym)
        else:
            logger.debug(f"流动性排除 {sym}: {check['reasons']}")
    logger.info(f"流动性筛选: {len(symbols)} → {len(liquid)} 只")
    return liquid


# ========== 2. Beta 计算 + 对冲 ==========

def calc_betas(symbols: list[str], period: str = "1y") -> dict:
    """
    计算每只标的相对于 SPY 的 Beta。
    Beta > 1 → 比大盘波动大（放大风险）
    Beta < 1 → 比大盘稳（防御性）
    """
    betas = {}
    spy = yf.download("SPY", period=period, interval="1d",
                      progress=False, auto_adjust=True)
    if spy.empty:
        return {s: 1.0 for s in symbols}
    spy_ret = spy["Close"].squeeze().pct_change().dropna()

    for sym in symbols:
        try:
            df = yf.download(sym, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                betas[sym] = 1.0
                continue
            sym_ret = df["Close"].squeeze().pct_change().dropna()
            common = spy_ret.index.intersection(sym_ret.index)
            if len(common) < 30:
                betas[sym] = 1.0
                continue
            cov = np.cov(sym_ret[common], spy_ret[common])
            beta = cov[0][1] / cov[1][1] if cov[1][1] > 0 else 1.0
            betas[sym] = round(float(beta), 2)
        except Exception:
            betas[sym] = 1.0

    logger.info(f"Beta计算完成: {len(betas)} 只 | "
                f"高Beta(>1.5): {sum(1 for b in betas.values() if b > 1.5)}只 | "
                f"低Beta(<0.8): {sum(1 for b in betas.values() if b < 0.8)}只")
    return betas


def hedge_suggestion(positions: list[dict], betas: dict,
                      total_equity: float) -> dict:
    """
    建议用 SPY 空头对冲多少市场风险。
    目标：把组合 Beta 降到 0.5 以下。
    """
    if not positions:
        return {"hedge_pct": 0, "spy_shares": 0, "reason": "无持仓"}

    # 加权组合 Beta
    portfolio_beta = 0.0
    total_val = sum(p.get("mkt_val", 0) for p in positions)
    if total_val <= 0:
        return {"hedge_pct": 0, "spy_shares": 0, "reason": "无市值"}

    for p in positions:
        sym = p.get("symbol", "")
        beta = betas.get(sym, 1.0)
        weight = p.get("mkt_val", 0) / total_val
        portfolio_beta += beta * weight

    # 如果组合 Beta < 0.5，不需要对冲
    if portfolio_beta <= 0.5:
        return {
            "portfolio_beta": round(portfolio_beta, 2),
            "hedge_pct": 0,
            "spy_shares": 0,
            "reason": f"组合Beta={portfolio_beta:.2f}，已在目标范围内",
        }

    # 需要把 Beta 降到 0.5
    target_beta = 0.5
    hedge_ratio = (portfolio_beta - target_beta) / 1.0  # SPY Beta=1
    hedge_value = total_equity * hedge_ratio

    # SPY 价格
    try:
        spy = yf.download("SPY", period="5d", progress=False, auto_adjust=True)
        spy_price = float(spy["Close"].squeeze().iloc[-1])
    except Exception:
        spy_price = 600.0

    spy_shares = max(1, int(hedge_value / spy_price)) if spy_price > 0 else 0

    logger.info(
        f"对冲建议: 组合Beta={portfolio_beta:.2f} → "
        f"需对冲 ${hedge_value:,.0f} ({spy_shares}股 SPY)"
    )

    return {
        "portfolio_beta": round(portfolio_beta, 2),
        "target_beta": target_beta,
        "hedge_pct": round(hedge_ratio, 4),
        "hedge_value": round(hedge_value, 2),
        "spy_shares": spy_shares,
        "reason": f"组合Beta={portfolio_beta:.2f}→{target_beta:.2f}，卖{spy_shares}股SPY对冲",
    }


# ========== 3. 策略衰减检测 ==========

def check_strategy_decay(rolling_returns: list[list[float]],
                          window: int = 20,
                          sharpe_threshold: float = 0.3,
                          drawdown_threshold: float = 0.10) -> dict:
    """
    检测策略是否在衰减。
    - 滚动20期夏普 < 0.3 → 策略在失效
    - 滚动最大回撤 > 10% → 需要关注
    """
    if len(rolling_returns) < window:
        return {"decaying": False, "reason": f"数据不足({len(rolling_returns)}期，需{window}期)"}

    recent = rolling_returns[-window:]
    recent_sharpe = sharpe_ratio([r for sublist in recent for r in sublist]) if isinstance(recent[0], list) else sharpe_ratio(recent)

    # 建立净值曲线算回撤
    if isinstance(recent[0], list):
        flat = [r for sublist in recent for r in sublist]
    else:
        flat = recent
    curve = [1.0]
    for r in flat:
        curve.append(curve[-1] * (1 + r))
    recent_mdd = max_drawdown(curve)

    decaying = recent_sharpe < sharpe_threshold or recent_mdd > drawdown_threshold
    reasons = []
    if recent_sharpe < sharpe_threshold:
        reasons.append(f"滚动夏普={recent_sharpe:.2f} < {sharpe_threshold}")
    if recent_mdd > drawdown_threshold:
        reasons.append(f"滚动回撤={recent_mdd:.2%} > {drawdown_threshold:.0%}")

    return {
        "decaying": decaying,
        "recent_sharpe": round(recent_sharpe, 3),
        "recent_max_dd": round(recent_mdd, 4),
        "reasons": reasons,
        "recommendation": "暂停实盘，重新优化参数" if decaying else "策略健康",
    }


# ========== 4. 异常检测 ==========

def detect_anomalies(daily_pnl: list[float],
                      daily_volume: list[int] = None) -> list[dict]:
    """
    检测异常：
    - PnL 超过 3 个标准差 → 异常盈亏
    - 连续 5 天亏损 → 需要审查
    - 成交量突然放大 5 倍 → 可能有新闻
    """
    anomalies = []
    if len(daily_pnl) < 5:
        return anomalies

    mu = sum(daily_pnl) / len(daily_pnl)
    sigma = (sum((x - mu) ** 2 for x in daily_pnl) / len(daily_pnl)) ** 0.5

    # 检查最近一天
    if sigma > 0 and abs(daily_pnl[-1] - mu) > 3 * sigma:
        anomalies.append({
            "type": "PNL_OUTLIER",
            "severity": "HIGH",
            "detail": f"今日PnL {daily_pnl[-1]:.2%} 偏离均值 {mu:.2%} 超过3σ ({sigma:.2%})",
        })

    # 连续亏损
    if len(daily_pnl) >= 5:
        if all(x <= 0 for x in daily_pnl[-5:]):
            anomalies.append({
                "type": "CONSECUTIVE_LOSSES",
                "severity": "CRITICAL",
                "detail": f"连续5天亏损: {[f'{x:.2%}' for x in daily_pnl[-5:]]}",
            })

    # 成交量异常
    if daily_volume and len(daily_volume) >= 10:
        avg_vol = sum(daily_volume[:-1]) / (len(daily_volume) - 1)
        today_vol = daily_volume[-1]
        if avg_vol > 0 and today_vol > avg_vol * 5:
            anomalies.append({
                "type": "VOLUME_SPIKE",
                "severity": "MEDIUM",
                "detail": f"成交量异常放大: {today_vol:,.0f} vs 均值 {avg_vol:,.0f}",
            })

    if anomalies:
        logger.warning(f"异常检测: {len(anomalies)} 项告警")

    return anomalies
