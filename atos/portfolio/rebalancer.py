"""
ATOS PRO v2 — 动态再平衡器
===========================
决定何时调仓、调多少。不是每天都调（过度交易损耗），
而是设阈值，漂移超过阈值才触发。

规则：
  1. 权重漂移 > 5% → 触发再平衡
  2. VIX > 25 → 增加现金比例
  3. VIX > 30 → 只减仓不加仓
  4. 单日亏损 > 3% → 暂停新开仓
  5. 通过 FutuOpenD 下单（限价单为主，减少滑点）
"""

from atos.core.logging import get_logger

logger = get_logger("portfolio.rebalancer")

# 再平衡阈值
DRIFT_THRESHOLD = 0.05   # 权重偏离 5% 以上触发
MIN_REBALANCE_INTERVAL_HOURS = 4  # 最少间隔 4 小时（避免频繁交易）


def enforce_cash_buffer(positions: list[dict], target_cash_pct: float,
                         current_cash: float, total_equity: float) -> list[dict]:
    """
    现金缓冲强制。

    当当前现金 < 目标现金时：
      1. 计算现金缺口
      2. 从所有盈利持仓中按比例卖出以筹集现金（不超过缺口金额）
      3. 返回生成的卖出订单列表

    参数:
        positions: 当前持仓列表（每项需包含 symbol, mkt_val, pl_pct）
        target_cash_pct: 目标现金比例（如 0.15 表示 15%）
        current_cash: 当前现金余额
        total_equity: 总资产（现金 + 持仓市值）

    返回:
        卖出订单列表 [{symbol, shares, estimated_value, reason}, ...]
    """
    target_cash = total_equity * target_cash_pct
    deficit = target_cash - current_cash

    if deficit <= 0:
        return []  # 现金充足，无需操作

    if deficit < 100:
        logger.debug(f"现金缺口仅${deficit:.0f}，不足$100，跳过强制补仓")
        return []

    logger.warning(
        f"现金不足: 当前 ${current_cash:,.0f} < 目标 ${target_cash:,.0f} "
        f"(缺口 ${deficit:,.0f})，开始强制卖出筹集现金"
    )

    # 收集所有盈利持仓
    profitable = []
    for p in positions:
        mkt_val = p.get("mkt_val", 0)
        pl_pct = p.get("pl_pct", 0.0)
        price = p.get("last", 0.0)
        if mkt_val > 0 and pl_pct > 0 and price > 0:
            profitable.append({
                "symbol": p["symbol"],
                "mkt_val": mkt_val,
                "pl_pct": pl_pct,
                "price": price,
            })

    if not profitable:
        logger.warning("无盈利持仓可卖，无法筹集现金")
        return []

    # 按盈利比例排序（盈利越多越优先卖）
    profitable.sort(key=lambda x: x["pl_pct"], reverse=True)
    total_profitable = sum(p["mkt_val"] for p in profitable)
    if total_profitable <= 0:
        return []

    sell_orders = []
    remaining = deficit
    for p in profitable:
        if remaining <= 0:
            break

        # 按盈利仓位比例分摊卖出
        proportion = p["mkt_val"] / total_profitable
        sell_value = min(deficit * proportion, p["mkt_val"] * 0.5, remaining)
        if sell_value < 100:  # 忽略太小的一笔
            continue

        shares = int(sell_value / p["price"])
        if shares < 1:
            continue

        actual_value = round(shares * p["price"], 2)
        sell_orders.append({
            "symbol": p["symbol"],
            "action": "SELL",
            "shares": shares,
            "estimated_value": actual_value,
            "reason": f"现金缓冲: 缺口 ${deficit:,.0f}，强制降低 {p['symbol']} 仓位",
        })
        remaining -= actual_value

    logger.info(
        f"现金强制卖出: {len(sell_orders)} 笔, "
        f"预计筹集 ${sum(o['estimated_value'] for o in sell_orders):,.0f}"
    )
    return sell_orders


def compute_cash_buffer(vix: float = 18.0, market_regime: str = "UNKNOWN") -> float:
    """
    计算应该保留多少现金。
    VIX 越高 → 现金越多 → 仓位越轻。

    映射：
      VIX < 15  → 现金 5%    (极度贪婪，但保持底线)
      VIX 15-20 → 现金 10%   (正常)
      VIX 20-25 → 现金 15%   (谨慎)
      VIX 25-30 → 现金 25%   (防御)
      VIX > 30  → 现金 40%   (高度防御)
      BEAR      → +10%
    """
    if vix < 15:
        base = 0.05
    elif vix < 20:
        base = 0.10
    elif vix < 25:
        base = 0.15
    elif vix < 30:
        base = 0.25
    else:
        base = 0.40

    # 熊市额外加 10% 现金
    if market_regime in ("BEAR", "HIGH_VOL"):
        base += 0.10

    return min(0.50, base)  # 最多 50% 现金


def check_drift(current_weights: dict[str, float],
                 target_weights: dict[str, float],
                 threshold: float = DRIFT_THRESHOLD) -> list[dict]:
    """
    检查哪些持仓偏离了目标权重。
    返回需要调整的列表。
    """
    drifts = []
    all_symbols = set(current_weights.keys()) | set(target_weights.keys())

    for sym in all_symbols:
        curr = current_weights.get(sym, 0.0)
        targ = target_weights.get(sym, 0.0)
        drift = abs(curr - targ)
        if drift > threshold:
            drifts.append({
                "symbol": sym,
                "current_weight": round(curr, 4),
                "target_weight": round(targ, 4),
                "drift": round(drift, 4),
                "direction": "BUY" if curr < targ else "SELL",
            })

    return sorted(drifts, key=lambda d: d["drift"], reverse=True)


def should_rebalance(current_positions: list[dict],
                      target_positions: dict[str, dict],
                      total_equity: float,
                      last_rebalance_time: float = None,
                      daily_pnl_pct: float = 0.0,
                      market_regime: str = "UNKNOWN",
                      current_cash: float = 0.0) -> dict:
    """
    决定是否触发再平衡，以及如何执行。

    参数:
        current_cash: 当前现金余额（用于现金缓冲检查）

    返回:
        {"should_rebalance": True/False,
         "reason": "...",
         "trades": [...],
         "cash_buffer_pct": 0.15}
    """
    # 1. 检查时间间隔
    import time
    if last_rebalance_time:
        hours_elapsed = (time.time() - last_rebalance_time) / 3600
        if hours_elapsed < MIN_REBALANCE_INTERVAL_HOURS:
            return {
                "should_rebalance": False,
                "reason": f"距上次再平衡仅 {hours_elapsed:.1f} 小时，跳过",
            }

    # 2. 单日亏损暂停
    if daily_pnl_pct < -0.03:
        return {
            "should_rebalance": True,
            "reason": f"单日亏损 {daily_pnl_pct:.1%}，只允许减仓",
            "allow_buys": False,
            "allow_sells": True,
        }

    # 2b. 现金缓冲检查
    target_cash_pct = compute_cash_buffer(market_regime=market_regime)
    cash_trades = enforce_cash_buffer(
        current_positions, target_cash_pct, current_cash, total_equity
    )
    cash_deficit = (target_cash_pct * total_equity) - current_cash
    if cash_deficit > 0:
        # 现金不足 → 阻止新开仓，生成强制卖出
        result = {
            "should_rebalance": True,
            "reason": (f"现金不足: 目标 {target_cash_pct:.0%} "
                       f"(${target_cash_pct * total_equity:,.0f}) "
                       f"< 当前 ${current_cash:,.0f}，强制卖出筹集现金"),
            "trades": cash_trades,
            "allow_buys": False,
            "allow_sells": True,
            "cash_buffer_pct": target_cash_pct,
            "cash_trades": cash_trades,
        }
        if cash_trades:
            logger.warning(f"现金缓冲强制执行: {len(cash_trades)} 笔卖出")
        return result

    # 3. 计算当前权重
    if total_equity <= 0:
        return {"should_rebalance": False, "reason": "总资产为0"}

    current_weights = {}
    for p in current_positions:
        mkt_val = p.get("mkt_val", 0)
        if mkt_val > 0:
            current_weights[p["symbol"]] = mkt_val / total_equity

    target_weights = {
        sym: t["weight"]
        for sym, t in target_positions.items()
    }

    # 4. 检查漂移
    drifts = check_drift(current_weights, target_weights)

    if not drifts:
        logger.debug("无需再平衡：所有持仓在目标范围内")
        return {"should_rebalance": False, "reason": "权重漂移在阈值内"}

    # 5. 生成交易指令
    trades = []
    for d in drifts:
        target_value = total_equity * d["target_weight"]
        current_value = total_equity * d["current_weight"]
        diff_value = target_value - current_value

        # 找到当前持仓获取价格
        current = next(
            (p for p in current_positions if p["symbol"] == d["symbol"]), None
        )
        price = current["last"] if current and current.get("last") else 100.0
        if price <= 0:
            price = 100.0

        shares = abs(int(diff_value / price))
        if shares > 0:
            trades.append({
                "symbol": d["symbol"],
                "action": d["direction"],
                "shares": shares,
                "estimated_value": round(shares * price, 2),
                "drift_pct": round(d["drift"] * 100, 1),
                "reason": f"再平衡: 权重偏离 {d['drift']:.1%}",
            })

    # 高波动市场只卖不买
    allow_buys = market_regime not in ("HIGH_VOL",) or False
    if not allow_buys:
        trades = [t for t in trades if t["action"] == "SELL"]

    drift_info = []
    for d in drifts[:3]:
        drift_info.append(f"{d['symbol']}:{d['drift']:.1%}")
    logger.info(
        f"再平衡触发: {len(trades)} 笔 | 漂移: {drift_info}"
    )

    return {
        "should_rebalance": True,
        "reason": f"{len(drifts)} 只持仓偏离阈值",
        "drifts": drifts,
        "trades": trades,
        "allow_buys": allow_buys,
        "allow_sells": True,
    }


def get_rebalance_summary(result: dict) -> str:
    """再平衡结果转可读文本，给 AI 做参考"""
    if not result.get("should_rebalance"):
        return f"无需再平衡: {result.get('reason', '')}"

    trades = result.get("trades", [])
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]

    lines = [
        f"再平衡: {len(buys)}买 {len(sells)}卖",
        f"原因: {result.get('reason', '')}",
    ]
    for t in trades[:5]:
        lines.append(
            f"  {t['action']} {t['shares']}股 {t['symbol']} "
            f"(~${t['estimated_value']:,.0f}) | {t['reason']}"
        )
    return "\n".join(lines)
