"""
ATOS PRO v2 — 实时交易引擎
==========================
每30分钟运行一个周期：
1. 获取账户状态
2. 判断市场环境
3. 计算技术信号（50只标的）
4. AI 决策
5. 风控过滤
6. 执行下单
7. 记录日志 + 更新指标
"""

import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.live.signal_engine import get_signals, UNIVERSE, ALL_SYMBOLS
from atos.live.portfolio import get_account_state
from atos.live.ai_advisor import get_advice
from atos.live.risk_manager import filter_orders, check_all_stops as check_stop_losses, reset_daily, record_fill
from atos.market.regime.regime_engine import RegimeEngine
from atos.live.futu_bridge import safe_place_order as place_order
from atos.live.kelly import kelly_fraction, kelly_qty, save_trade, crouching_allocation
from atos.core.logging import get_logger, log_trade, log_risk, log_error
from atos.core.universe import filter_by_volume, filter_by_trend, get_active_symbols
from atos.factors import (
    batch_value_factors, batch_momentum_factors, batch_quality_factors,
    combine, get_top_picks,
)
from atos.portfolio import (
    check_concentration_risk, compute_cash_buffer,
    compute_target_positions, should_rebalance, get_rebalance_summary,
)

import yfinance as yf

INTERVAL_MINUTES = 30
logger = get_logger("live_trader")
_REGIME_ENGINE = None
_REGIME_CACHE = None   # Bug #10: 缓存 regime 结果
_REGIME_CACHE_TS = None


def is_market_open():
    """美股交易时间：9:30 AM – 4:00 PM EST (UTC 13:30–20:00)"""
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() >= 5:
        return False
    open_ = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_ <= now <= close_


def _seconds_until_next_market_open() -> float:
    """计算到下一个交易时段开始的秒数。休市时用于智能 sleep。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    # 下一个可能的开市时间：今天 13:30 或下个交易日 13:30
    today_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
    if now.weekday() < 5 and now < today_open:
        # 今天还没到开市时间，等到今天 13:30
        return (today_open - now).total_seconds()
    # 计算下个交易日
    days_until_next = 1
    next_day = now + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:
        days_until_next += 1
        next_day = now + datetime.timedelta(days=days_until_next)
    next_open = next_day.replace(hour=13, minute=30, second=0, microsecond=0)
    return (next_open - now).total_seconds()


def get_regime():
    """获取当前市场状态（带缓存，Bug #10）"""
    global _REGIME_ENGINE, _REGIME_CACHE, _REGIME_CACHE_TS
    now = datetime.datetime.now()
    # 缓存 5 分钟
    if _REGIME_CACHE is not None and _REGIME_CACHE_TS is not None:
        if (now - _REGIME_CACHE_TS).total_seconds() < 300:
            return _REGIME_CACHE
    if _REGIME_ENGINE is None:
        _REGIME_ENGINE = RegimeEngine()
    try:
        spy = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True)
        vix = yf.download("^VIX", period="1y", interval="1d", progress=False, auto_adjust=True)
    except Exception:
        return {"regime": "UNKNOWN", "risk_multiplier": 0.5}
    engine = _REGIME_ENGINE
    engine.spy_prices = []
    engine.vix_prices = []
    spy_c = spy["Close"].squeeze().tolist()
    vix_c = vix["Close"].squeeze().tolist()
    for i in range(min(len(spy_c), len(vix_c))):
        engine.update(float(spy_c[i]), float(vix_c[i]))
    result = engine.get_regime()
    _REGIME_CACHE = result
    _REGIME_CACHE_TS = now
    return result


def _calc_sell_pnl(symbol, qty, account, signals) -> float:
    """Bug #4/#5: 统一卖出 PnL 计算。找不到持仓时返回 0（不伪造数据）。"""
    pos = next((p for p in account["positions"] if p["symbol"] == symbol), None)
    if pos:
        sell_price = signals.get(symbol, {}).get("price", pos.get("last", 0))
        return qty * (sell_price - pos["avg_price"])
    return 0.0


def _log_sell_pnl(symbol, qty, account, signals, reason="SELL"):
    """统一卖出日志记录。"""
    try:
        real_pnl = _calc_sell_pnl(symbol, qty, account, signals)
        record_fill(real_pnl, account["total"])
        from atos.live.daily_review import log_trade as save_log
        save_log(symbol, reason, qty, signals.get(symbol, {}).get("price", 0), pnl_pct=real_pnl)
    except Exception as e:
        logger.debug(f"卖出日志写入失败: {e}")


def compute_order_qty(symbol, target_pct, account_state, signals, score=0.5):
    """计算实际下单数量（使用 Crouching 方法）"""
    if symbol not in signals:
        return 0
    price = signals[symbol]["price"]
    if price <= 0:
        return 0
    try:
        from atos.live.risk_manager import get_state as get_risk_state
        rs = get_risk_state()
        drawdown = rs.get("drawdown", 0.0) or 0.0
    except Exception:
        drawdown = 0.0
    crouching_pct = crouching_allocation(score=score, drawdown=drawdown, has_news_catalyst=False)
    kelly_pct = kelly_fraction()
    final_pct = min(crouching_pct, kelly_pct, target_pct)
    # Bug #3: 删除强制的 max(final_pct, 0.01) — 当风控说 0% 时不应强行开仓
    if final_pct <= 0:
        return 0
    final_pct = min(final_pct, 0.20)
    target_val = account_state["total"] * final_pct
    current_val = next(
        (p["mkt_val"] for p in account_state["positions"] if p["symbol"] == symbol),
        0.0
    )
    delta = target_val - current_val
    if delta <= 0:
        return 0
    return max(1, int(delta / price))


def run_cycle():
    """执行一个交易周期"""
    logger.info("=" * 50)
    logger.info("交易周期开始")

    # 1. 账户状态
    try:
        account = get_account_state()
    except Exception as e:
        log_error("live_trader", f"账户获取失败: {e}")
        return

    logger.info(
        f"模式={account['mode']} | 总资产=${account['total']:,.0f} | "
        f"现金=${account['cash']:,.0f} | 持仓={len(account['positions'])}只"
    )

    # 2. 市场环境
    try:
        regime = get_regime()
    except Exception as e:
        logger.warning(f"市场状态获取失败: {e}")
        regime = {"regime": "UNKNOWN", "risk_multiplier": 0.5}

    logger.info(f"市场={regime['regime']} | 风险系数={regime['risk_multiplier']}")

    # 3. 信号计算
    try:
        signals = get_signals()
    except Exception as e:
        log_error("live_trader", f"信号计算失败: {e}")
        return

    active = get_active_symbols(signals)
    logger.info(
        f"信号: 优质={len(active['quality'])}只 | "
        f"观察={len(active['watch'])}只 | 回避={len(active['avoid'])}只"
    )

    # 3.5 多因子计算
    candidate_symbols = active["quality"] + active["watch"]
    try:
        value_factors = batch_value_factors(candidate_symbols)
        momentum_factors = batch_momentum_factors(candidate_symbols)
        quality_factors = batch_quality_factors(candidate_symbols)
        factor_result = combine(signals, value_factors, momentum_factors,
                                quality_factors, regime["regime"], use_v3_signals=True)
        top_picks = get_top_picks(factor_result, n=15)
        logger.info(
            f"因子合成完成 | Top5: {[(p['symbol'], p['score']) for p in top_picks[:5]]}"
        )
    except Exception as e:
        log_error("live_trader", f"因子计算失败: {e}")
        factor_result = None
        top_picks = []

    # 4. 止损检查
    stop_signals = {
        s["symbol"]: signals.get(s["symbol"], {})
        for s in account["positions"]
    }
    for order in check_stop_losses(account["positions"], stop_signals):
        log_risk("STOP_LOSS", f"{order['symbol']} qty={order['qty']}")
        place_order(order["symbol"], "SELL", order["qty"])
        log_trade(order["symbol"], "SELL", order["qty"], 0, reason="止损退出")
        _log_sell_pnl(order["symbol"], order["qty"], account, signals, reason="STOP_LOSS")

    # 4.5 组合优化
    vix = 18.0
    try:
        vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=True)
        if not vix_df.empty:
            vix = float(vix_df["Close"].squeeze().iloc[-1])
    except Exception:
        pass

    cash_buffer_pct = compute_cash_buffer(vix, regime["regime"])
    top_symbols = [p["symbol"] for p in top_picks[:10]] if top_picks else \
                  [s for s in list(signals.keys())[:10]]

    try:
        target_result = compute_target_positions(
            symbols=top_symbols,
            total_equity=account["total"],
            cash_reserve_pct=cash_buffer_pct,
            current_positions=account["positions"],
            use_risk_budget=True,
        )
        rebalance_result = should_rebalance(
            current_positions=account["positions"],
            target_positions=target_result["target_positions"],
            total_equity=account["total"],
            daily_pnl_pct=0.0,
            market_regime=regime["regime"],
            vix=vix,
        )
        logger.info(
            f"组合优化: VIX={vix:.1f} → 现金缓冲={cash_buffer_pct:.0%} | "
            f"预期波动={target_result.get('expected_volatility', 'N/A')} | "
            f"{get_rebalance_summary(rebalance_result)[:100]}"
        )
        if rebalance_result.get("should_rebalance"):
            for trade in rebalance_result.get("trades", [])[:5]:
                logger.info(f"再平衡: {trade['action']} {trade['shares']}股 {trade['symbol']}")
                place_order(trade["symbol"], trade["action"], trade["shares"])
    except Exception as e:
        log_error("portfolio", f"组合优化失败: {e}")
        target_result = None
        rebalance_result = {"should_rebalance": False}
        cash_buffer_pct = 0.10

    # 相关性检查
    corr_alerts = check_concentration_risk(account["positions"])
    if corr_alerts:
        for alert in corr_alerts[:3]:
            log_risk("CORRELATION", alert.get("suggestion", ""))

    # 5. AI 决策
    snapshot = {
        "mode": account["mode"],
        "total_equity": account["total"],
        "cash": account["cash"],
        "positions": account["positions"],
        "market_regime": regime,
        "universe": [
            {"symbol": s, **signals[s]}
            for s in ALL_SYMBOLS if s in signals
        ],
        "quality_symbols": active["quality"],
        "watch_symbols": active["watch"],
        "constraints": account["constraints"],
        "universe_long": UNIVERSE["long_term"],
        "universe_short": UNIVERSE["short_term"],
        "factor_rankings": [
            {"symbol": p["symbol"], "score": p["score"],
             "breakdown": p.get("breakdown", {})}
            for p in top_picks[:10]
        ] if top_picks else [],
        "factor_weights": factor_result["weights"] if factor_result else {},
        "vix": round(vix, 1),
        "cash_buffer_pct": round(cash_buffer_pct, 3),
        "target_positions": target_result.get("target_positions", {}) if target_result else {},
        "expected_volatility": target_result.get("expected_volatility") if target_result else None,
        "correlation_alerts": [{"pair": a["pair"], "corr": a["correlation"]}
                               for a in corr_alerts[:5]] if corr_alerts else [],
        "rebalance_needed": rebalance_result.get("should_rebalance", False),
    }

    # AI 否决审查
    advice = {}
    try:
        from atos.ai.engine_v2 import get_advice_v3
        advice = get_advice_v3(snapshot)
    except Exception as e:
        logger.debug(f"AI veto调用失败: {e}")

    veto_map = advice.get("veto_map", {})
    vetoed_symbols = {s for s, v in veto_map.items()
                      if isinstance(v, dict) and v.get("veto", False)}

    # 因子引擎决策
    proposed = []
    for p in top_picks[:8]:
        sym = p["symbol"]
        if sym in vetoed_symbols:
            logger.info(f"🧠 AI否决跳过 {sym}")
            continue
        if p.get("score", 0) < 0.55:
            continue
        existing = next((pos for pos in account["positions"] if pos["symbol"] == sym), None)
        if existing:
            continue
        proposed.append({
            "symbol": sym, "action": "BUY",
            "target_pct": min(p.get("score", 0.5) * 0.12, 0.15),
            "reason": f"因子开仓 score={p.get('score',0):.2f}",
        })

    safe = filter_orders(proposed, account, regime)
    logger.info(f"候选={len(top_picks)} | AI否决={len(vetoed_symbols)} | 风控通过={len(safe)}条")

    # 6. 执行下单
    for order in safe:
        sym = order["symbol"]
        action = order["action"]
        qty = compute_order_qty(sym, order.get("target_pct", 0), account, signals)
        if qty == 0:
            logger.debug(f"跳过 {action} {sym}: qty=0")
            continue
        if qty < 0 and action == "BUY":
            logger.debug(f"跳过买入 {sym}: 已超配")
            continue

        logger.info(f"下单 {action} {abs(qty)}股 {sym} | {order.get('reason', '--')}")
        result = place_order(sym, action, abs(qty))

        if result:
            log_trade(sym, action, abs(qty), signals.get(sym, {}).get("price", 0),
                      reason=order.get("reason", ""))
            if action == "SELL":
                _log_sell_pnl(sym, abs(qty), account, signals, reason="SELL")
            else:
                record_fill(0.0, account["total"])
        else:
            log_error("live_trader", f"下单失败: {action} {qty}股 {sym}")

    logger.info(f"交易周期结束 | {advice.get('risk_notes', '--')}")


def main():
    logger.info("🚀 ATOS PRO v2 实时交易引擎启动")
    logger.info(f"标的池: {len(ALL_SYMBOLS)} 只")
    logger.info(f"交易间隔: {INTERVAL_MINUTES} 分钟")
    logger.info(f"DeepSeek API: {'已配置' if os.environ.get('DEEPSEEK_API_KEY') else '❌ 未配置'}")

    reset_daily()
    last_reset = datetime.date.today()

    while True:
        today = datetime.date.today()
        if today != last_reset:
            reset_daily()
            last_reset = today
            logger.info("日计数器已重置")

        if is_market_open():
            run_cycle()
            time.sleep(INTERVAL_MINUTES * 60)
        else:
            # Bug #8: 非交易时段智能 sleep，不等 30 分钟空跑
            wait_seconds = _seconds_until_next_market_open()
            # 至少等 5 分钟（避免频繁轮询），最多等到开市
            wait_seconds = max(300, min(wait_seconds, 3600 * 16))
            next_open_str = (datetime.datetime.now(datetime.timezone.utc) +
                             datetime.timedelta(seconds=wait_seconds)).strftime("%H:%M UTC")
            logger.debug(f"市场休市，{wait_seconds/60:.0f}分钟后 ({next_open_str}) 再检查")
            time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
