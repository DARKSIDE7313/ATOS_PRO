"""
ATOS PRO v2 — Shadow Trader（影子交易）
========================================
完全本地模拟交易，不需要 FutuOpenD。
在真钱之前先"纸上谈兵"，验证策略。

与 FutuOpenD 的区别：
  - 账户数据本地模拟
  - 下单本地记录（不连券商）
  - 价格用 yfinance 实时价
  - 滑点 + 佣金模拟更真实

使用方法：
  python3 -m atos.shadow.shadow_trader
"""

import os
import sys
import json
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.config_shared import ALLOCATION  # 🆕 共享资金配置
from atos.core.logging import get_logger, log_trade, log_risk
from atos.core.metrics import format_report
from atos.live.signal_engine import get_signals, get_realtime_signals
from atos.live.risk_manager import check_stop_losses
from atos.market.regime.regime_engine import RegimeEngine
from atos.factors import batch_value_factors, batch_momentum_factors, batch_quality_factors, combine, get_top_picks
from atos.ai.engine_v2 import get_advice_v2
from atos.core.universe import ALL_SYMBOLS, UNIVERSE_FULL
from atos.portfolio import compute_cash_buffer, compute_target_positions, should_rebalance, check_concentration_risk
from atos.shadow.reporter import generate_report
from atos.risk.advanced import (
    liquidity_check, filter_liquid_universe, calc_betas,
    hedge_suggestion, check_strategy_decay, detect_anomalies,
)
from atos.risk.professional import (
    triple_barrier, vol_target_position, TrailingStop, kelly_after_drawdown,
)
from atos.core.metrics import var_historical, cvar_historical, stress_test
from atos.debugger.safety_net import (
    validate_market_data, safe_price, safe_divide, clamp, money_round,
    is_duplicate_order, check_disk_space, full_health_check,
    atomic_write, safe_load_json, is_safe_to_trade,
)
from atos.market.sentiment import get_full_sentiment
from atos.factors.advanced_signals import get_all_advanced_signals, intermarket_signals
from atos.longterm.value_investor import screen_long_term_candidates, BURRY_PRINCIPLES
import yfinance as yf

logger = get_logger("shadow_trader")


class ShadowAccount:
    """本地模拟账户 — 短期交易 $10,000"""

    def __init__(self, initial_cash: float = 1000000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}  # {symbol: {qty, avg_price, last_price}}
        self.trade_history = []
        self.cycle_returns = []  # 每周期收益序列（用于 VaR/CVaR 计算）
        self.trailing_stops = {}  # 🆕 {symbol: TrailingStop}
        self.cycle_count = 0
        self.prev_equity = initial_cash
        self.commission_per_share = 0.005
        self.min_commission = 1.0
        self.slippage_pct = 0.001  # 0.1%
        self.stop_loss_blacklist = {}  # 🔴 P0-1: 止损冷却期 {symbol: sell_cycle}
        self.strategy_decay_factor = 1.0  # 🔴 P3-1: 策略衰减系数，默认1.0=正常

    def is_cooling_off(self, symbol: str) -> bool:
        """检查标的是否在止损冷却期内（24h = 288周期）"""
        if symbol in self.stop_loss_blacklist:
            sold_cycle = self.stop_loss_blacklist[symbol]
            if self.cycle_count - sold_cycle < 288:
                return True
            else:
                del self.stop_loss_blacklist[symbol]
        return False

    def add_to_blacklist(self, symbol: str):
        """止损卖出后将标的加入冷却黑名单"""
        self.stop_loss_blacklist[symbol] = self.cycle_count
        logger.info(f"🔒 止损冷却: {symbol} 禁止买入，冷却至周期 #{self.cycle_count + 288}")

    def clean_blacklist(self):
        """清理过期的冷却记录"""
        expired = [s for s, c in self.stop_loss_blacklist.items()
                   if self.cycle_count - c >= 288]
        for s in expired:
            del self.stop_loss_blacklist[s]
            logger.debug(f"冷却期结束: {s} 恢复可交易")

    @property
    def total_equity(self) -> float:
        pos_val = sum(
            p["qty"] * p.get("last_price", p["avg_price"])
            for p in self.positions.values()
        )
        return self.cash + pos_val

    @property
    def position_list(self) -> list[dict]:
        result = []
        for sym, p in self.positions.items():
            last = p.get("last_price", p["avg_price"])
            pnl = (last - p["avg_price"]) * p["qty"]
            pnl_pct = (last - p["avg_price"]) / p["avg_price"] if p["avg_price"] > 0 else 0
            result.append({
                "symbol": sym,
                "qty": p["qty"],
                "avg_price": p["avg_price"],
                "last": last,
                "mkt_val": last * p["qty"],
                "pnl_pct": round(pnl_pct, 4),
            })
        return result

    @property
    def mode(self) -> str:
        t = self.total_equity
        if t < 50000: return "VERY_AGGRESSIVE"
        elif t < 200000: return "AGGRESSIVE"
        elif t < 500000: return "MODERATE"
        return "CONSERVATIVE"

    @property
    def max_positions(self) -> int:
        return { "VERY_AGGRESSIVE": 3, "AGGRESSIVE": 5, "MODERATE": 8, "CONSERVATIVE": 10 }[self.mode]

    @property
    def max_single_pct(self) -> float:
        return { "VERY_AGGRESSIVE": 0.35, "AGGRESSIVE": 0.25, "MODERATE": 0.20, "CONSERVATIVE": 0.12 }[self.mode]

    @property
    def min_cash_pct(self) -> float:
        return { "VERY_AGGRESSIVE": 0.03, "AGGRESSIVE": 0.05, "MODERATE": 0.10, "CONSERVATIVE": 0.15 }[self.mode]

    def get_state(self) -> dict:
        pos_val = sum(
            p["qty"] * p.get("last_price", p["avg_price"])
            for p in self.positions.values()
        )
        return {
            "total": self.total_equity,
            "cash": self.cash,
            "mkt_val": pos_val,
            "mode": self.mode,
            "max_positions": self.max_positions,
            "alloc": {"short_pct": 0.2, "long_pct": 0.7, "cash_pct": 0.1},
            "positions": self.position_list,
            "constraints": {
                "max_single_pct": self.max_single_pct,
                "short_budget": self.total_equity * 0.2,
                "long_budget": self.total_equity * 0.7,
                "min_cash": self.total_equity * self.min_cash_pct,
            },
        }

    def update_prices(self, signals: dict):
        """用最新信号更新持仓价格"""
        for sym, p in self.positions.items():
            if sym in signals:
                p["last_price"] = signals[sym]["price"]

    def execute(self, symbol: str, action: str, shares: int,
                price: float, reason: str = "") -> bool:
        """执行模拟交易（带全套安全检查）"""
        # 安全检查
        if shares <= 0:
            return False
        if not symbol or not isinstance(symbol, str):
            logger.error(f"无效标的: {symbol}")
            return False
        if safe_price(price) is None:
            logger.error(f"无效价格: {price}")
            return False
        if shares > 100000:
            logger.error(f"数量异常: {shares}股 — 可能是计算错误")
            return False
        if is_duplicate_order(symbol, action, shares):
            return False

        # 🔒 硬性现金下限：买入后现金不能低于min_cash_pct
        if action == "BUY":
            min_cash = self.total_equity * self.min_cash_pct
            estimated_cost = price * shares + max(self.min_commission, shares * self.commission_per_share)
            if self.cash - estimated_cost < min_cash:
                affordable_shares = int((self.cash - min_cash) / (price * 1.001))
                if affordable_shares <= 0:
                    return False
                shares = affordable_shares

        # 硬上限：单只不超设定比例
        max_position_value = self.total_equity * self.max_single_pct
        current_value = self.positions[symbol]["qty"] * price if symbol in self.positions else 0
        max_buy_value = max_position_value - current_value
        if max_buy_value <= 0 and action == "BUY":
            return False

        # 滑点
        slip = price * self.slippage_pct
        fill = price + slip if action == "BUY" else price - slip
        comm = max(self.min_commission, shares * self.commission_per_share)

        if action == "BUY":
            # 硬约束：单只不超总资产的 20%
            max_position_value = self.total_equity * 0.20
            current_value = self.positions[symbol]["qty"] * fill if symbol in self.positions else 0
            max_buy_value = max_position_value - current_value
            if max_buy_value <= 0:
                return False
            buy_value = fill * shares
            if buy_value > max_buy_value:
                shares = int(max_buy_value / fill)
            if shares <= 0:  # Bug #2: 调整后可能为0
                return False

            cost = fill * shares + comm
            if cost > self.cash:
                affordable = int((self.cash - self.min_commission) / fill)
                if affordable <= 0:
                    return False
                shares = affordable
                cost = fill * shares + comm

            self.cash -= cost
            if symbol in self.positions:
                old = self.positions[symbol]
                total_qty = old["qty"] + shares
                old_cost = old["qty"] * old["avg_price"]
                self.positions[symbol] = {
                    "qty": total_qty,
                    "avg_price": (old_cost + fill * shares) / total_qty,
                    "last_price": fill,
                }
            else:
                self.positions[symbol] = {
                    "qty": shares,
                    "avg_price": fill,
                    "last_price": fill,
                }

        elif action == "SELL":
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
            if pos["qty"] < shares:
                shares = pos["qty"]

            pnl = (fill - pos["avg_price"]) * shares
            self.cash += fill * shares - comm

            pos["qty"] -= shares
            if pos["qty"] <= 0:
                del self.positions[symbol]

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol,
                "action": action,
                "shares": shares,
                "price": round(fill, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
            })

        log_trade(symbol, action, shares, price, reason=reason)
        return True


def run_shadow_cycle(account: ShadowAccount, cycle: int = 0):
    """运行一个影子交易周期（与 live_trader 逻辑相同，但不连 FutuOpenD）"""
    account.cycle_count += 1
    logger.info(f"Shadow Cycle {cycle} (#{account.cycle_count}) | Equity=${account.total_equity:,.0f} | Cash=${account.cash:,.0f} | Mode={account.mode}")

    # 周期级安全检查
    check_disk_space(min_free_mb=50)
    full_health_check(account.get_state())

    # 市场时间检查 — 如果收盘，跳过AI推演但保留风控和追踪止损
    market_ok, market_reason = is_safe_to_trade()
    ai_skip_reason = ""
    if not market_ok:
        ai_skip_reason = f"非交易时间 ({market_reason}) — 跳过AI推演，仅保留风控"
        logger.info(ai_skip_reason)
    else:
        logger.info(f"交易时间: {market_reason}")

    # 1. 市场状态
    spy = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="1y", interval="1d", progress=False, auto_adjust=True)
    engine = RegimeEngine()
    spy_c = spy["Close"].squeeze().tolist()
    vix_c = vix["Close"].squeeze().tolist()
    for i in range(min(len(spy_c), len(vix_c))):
        engine.update(float(spy_c[i]), float(vix_c[i]))
    regime = engine.get_regime()

    current_vix = float(vix_c[-1]) if vix_c else 18.0
    logger.info(f"Regime={regime['regime']} | VIX={current_vix:.1f}")

    # 🟢 SPY 趋势过滤 — 决定做多/做空/轻仓
    spy_trend = "BULL"
    try:
        spy_close = spy["Close"].squeeze()
        spy_ma20 = spy_close.rolling(20).mean().iloc[-1] if len(spy_close) >= 20 else spy_close.iloc[-1]
        spy_ma50 = spy_close.rolling(50).mean().iloc[-1] if len(spy_close) >= 50 else spy_close.iloc[-1]
        spy_current = spy_close.iloc[-1]
        if spy_current < spy_ma20 and spy_current < spy_ma50:
            spy_trend = "BEAR"
            logger.warning(f"🐻 SPY趋势看空: 当前${spy_current:.0f} < MA20${spy_ma20:.0f} < MA50${spy_ma50:.0f} → 减仓50%")
        elif spy_current < spy_ma20:
            spy_trend = "CAUTIOUS"
            logger.info(f"🟡 SPY趋势谨慎: 当前${spy_current:.0f} < MA20${spy_ma20:.0f} → 减仓25%")
        else:
            spy_trend = "BULL"
            logger.info(f"🟢 SPY趋势看多: 当前${spy_current:.0f} > MA20${spy_ma20:.0f}")
    except Exception as e:
        logger.warning(f"SPY趋势分析失败: {e}")

    # 趋势系数: BULL=1.0, CAUTIOUS=0.75, BEAR=0.50
    trend_factor = {"BULL": 1.0, "CAUTIOUS": 0.75, "BEAR": 0.50}.get(spy_trend, 1.0)
    if trend_factor < 1.0:
        logger.info(f"📊 趋势系数: ×{trend_factor:.0%} ({spy_trend})")

    # 7. 信号（🆕 使用实时数据源获取当前价格，历史指标保持 yfinance）
    use_realtime = getattr(account, '_use_realtime', True)
    if use_realtime:
        signals = get_realtime_signals()
        # 提取第一个有 data_source 的标的来记录数据源
        data_source = next((s["data_source"] for s in signals.values()
                           if isinstance(s, dict) and "data_source" in s),
                          "yfinance")
        logger.info(f"📡 数据源: {data_source}")
    else:
        signals = get_signals()
    if not signals:
        return

    # 3. 因子
    symbols = list(signals.keys())[:20]  # 取有信号的前20只
    try:
        v = batch_value_factors(symbols)
        m = batch_momentum_factors(symbols)
        q = batch_quality_factors(symbols)
        factor_result = combine(signals, v, m, q, regime["regime"], use_v3_signals=True)
        top_picks = get_top_picks(factor_result, n=10)
    except Exception as e:
        logger.error(f"因子失败: {e}")
        top_picks = []

    # 4. 强制价格更新 + 硬性止盈止损
    account.update_prices(signals)
    for sym, pos in list(account.positions.items()):
        # yfinance兜底拉最新价
        if pos.get("last_price", 0) == pos.get("avg_price", 0):
            try:
                stock = yf.Ticker(sym)
                live_px = stock.fast_info.get("lastPrice", 0) if hasattr(stock, 'fast_info') else 0
                if live_px > 0: pos["last_price"] = live_px
            except: pass
        px = pos.get("last_price", pos.get("avg_price", 0))
        if px <= 0: continue
        change_pct = (px - pos["avg_price"]) / pos["avg_price"]
        if change_pct > 0.12:  # 止盈卖一半
            sq = pos["qty"] // 2
            if sq > 0:
                last_decision_id = pos.get("decision_id", 0) if sym in account.positions else 0
                account.execute(sym, "SELL", sq, px, reason=f"止盈 +{change_pct:.1%}")
                try:
                    from atos.ai.memory import record_outcome
                    from atos.live.kelly import save_trade
                    record_outcome(last_decision_id, "WIN", pnl_pct=change_pct, exit_reason="止盈", ai_correct=True)
                    save_trade(change_pct)
                except: pass
        # 🔴 动态波动率止损: 按ATR设置，最低3%，最高10%
        atr_for_stop = signals.get(sym, {}).get("atr", 0)
        if atr_for_stop > 0 and px > 0:
            dynamic_stop = max(0.03, min(0.10, (atr_for_stop / px) * 3))
        else:
            dynamic_stop = 0.05  # 默认5%
        if change_pct < -dynamic_stop:  # 动态止损
            last_decision_id = pos.get("decision_id", 0) if sym in account.positions else 0
            account.execute(sym, "SELL", pos["qty"], px, reason=f"硬止损 {change_pct:.1%}")
            account.add_to_blacklist(sym)  # Bug #6: 硬止损加入黑名单
            try:
                from atos.ai.memory import record_outcome
                from atos.live.kelly import save_trade
                record_outcome(last_decision_id, "LOSS", pnl_pct=change_pct, exit_reason="硬止损", ai_correct=False)
                save_trade(change_pct)
            except: pass

    # 5. 专业止损 (追踪止损 + 固定止损双保险)
    # 5a. 初始化/更新追踪止损
    for sym, pos in list(account.positions.items()):
        price = signals.get(sym, {}).get("price", pos.get("last_price", 0))
        if price <= 0:
            continue
        if sym not in account.trailing_stops:
            # 🔴 P3-2: 波动率追踪止损 — ATR×3，范围[3%, 10%]
            atr_val = signals.get(sym, {}).get("atr", 0)
            if atr_val > 0 and price > 0:
                vol_based_trail = max(0.03, min(0.10, (atr_val / price) * 3))
            else:
                vol_based_trail = 0.05  # 默认5%
            ts = TrailingStop(trail_pct=vol_based_trail)
            ts.init(pos["avg_price"])
            account.trailing_stops[sym] = ts
            logger.debug(f"追踪止损 {sym}: trail={vol_based_trail:.1%} (ATR=${atr_val:.2f})")
        ts_result = account.trailing_stops[sym].update(price)
        if ts_result["triggered"]:
            log_risk("TRAILING_STOP", f"{sym}: {ts_result['reason']}")
            pnl_pct = ts_result.get("unrealized_pnl", 0)
            last_decision_id = pos.get("decision_id", 0)
            account.execute(sym, "SELL", pos["qty"], price, reason="追踪止损")
            account.add_to_blacklist(sym)  # 🔴 P0-1: 止损后冷却
            del account.trailing_stops[sym]
            try:
                from atos.ai.memory import record_outcome
                from atos.live.kelly import save_trade
                record_outcome(last_decision_id, "LOSS" if pnl_pct < 0 else "WIN", pnl_pct=pnl_pct,
                               exit_reason="追踪止损", ai_correct=(pnl_pct > 0))
                save_trade(pnl_pct)
            except: pass

    # 5b. 固定止损兜底 (strategy_config 里的 stop_loss_pct)
    for order in check_stop_losses(account.position_list, account.get_state()):
        sym = order["symbol"]
        if sym in account.trailing_stops:
            # 追踪止损已经触发了，跳过固定止损
            continue
        logger.warning(f"FIXED STOP LOSS: {sym} qty={order['qty']}")
        pnl_pct = order.get("pnl_pct", 0)
        last_decision_id = account.positions.get(sym, {}).get("decision_id", 0)
        account.execute(sym, "SELL", order["qty"],
                        signals.get(sym, {}).get("price", 0),
                        reason="固定止损")
        account.add_to_blacklist(sym)  # 🔴 P0-1: 止损后冷却
        try:
            from atos.ai.memory import record_outcome
            from atos.live.kelly import save_trade
            record_outcome(last_decision_id, "LOSS", pnl_pct=pnl_pct, exit_reason="固定止损", ai_correct=False)
            save_trade(pnl_pct)
        except: pass

    # 5c. 回撤减仓检查
    peak_equity = max(account.initial_cash, account.total_equity)
    current_dd = (peak_equity - account.total_equity) / peak_equity if peak_equity > 0 else 0
    dd_adj = kelly_after_drawdown(
        base_kelly_pct=0.20,  # AGGRESSIVE 基准 Kelly 20%
        current_drawdown=current_dd,
    )
    if dd_adj["status"] != "正常":
        log_risk("DRAWDOWN", f"回撤{current_dd:.2%} → {dd_adj['status']}")

    # 5.5 市场情绪
    sentiment = get_full_sentiment(
        [s for s in list(signals.keys())[:10]]
    )
    logger.info(f"情绪: {sentiment['overall']} | 恐惧贪婪={sentiment['fear_greed_index']} | VIX={sentiment['vix']} | {sentiment['breadth']}")

    # 跨资产信号
    intermarket = intermarket_signals()
    if intermarket:
        for key, msg in intermarket.items():
            logger.info(f"跨资产: {msg[:80]}")

    # 高级信号（仅对持仓 + Top3 候选）
    adv_signal_targets = [p["symbol"] for p in account.position_list] + \
                         [p["symbol"] for p in top_picks[:3]] if top_picks else []
    advanced = {}
    for sym in list(set(adv_signal_targets))[:6]:
        adv = get_all_advanced_signals(sym)
        if "error" not in adv:
            advanced[sym] = adv
            mr = adv.get("mean_reversion", {}).get("signal", "")
            if mr != "NONE":
                logger.info(f"高级信号 {sym}: {mr}")
            vol_div = adv.get("volume_divergence", {}).get("signal", "")
            if vol_div != "NONE":
                logger.info(f"量价信号 {sym}: {vol_div}")

    # 长期价值筛选（每天只跑一次，省API）
    if account.cycle_count % 288 == 1:  # 每天第一个周期 (288 × 5min = 24h)
        long_term_candidates = screen_long_term_candidates(
            [s for s in list(signals.keys())[:20]], min_score=60
        )
        logger.info(f"长期投资候选: {len(long_term_candidates)} 只 | Top: {[(c['symbol'], c['burry_score']) for c in long_term_candidates[:3]]}")

        # 🔴 P3-1: 策略衰减检测 — 每天检查一次
        decay_check = check_strategy_decay(
            [[r] for r in account.cycle_returns[-60:]] if account.cycle_returns else [],
            window=20, sharpe_threshold=0.3, drawdown_threshold=0.10,
        )
        if decay_check.get("decaying"):
            account.strategy_decay_factor = 0.5
            logger.warning(f"⚠️ 策略衰减检测: 滚动夏普={decay_check['recent_sharpe']:.2f} → "
                          f"总仓位降至50% | {decay_check.get('recommendation', '')}")
        else:
            account.strategy_decay_factor = 1.0
            logger.info(f"✅ 策略健康: 滚动夏普={decay_check.get('recent_sharpe', 'N/A')}")
    else:
        long_term_candidates = []

    # 5.6 机构级风控检查
    # 流动性筛查
    liquid_symbols = filter_liquid_universe(
        [s for s in list(signals.keys())[:30]]
    ) if account.cycle_count == 1 else list(signals.keys())

    # 🆕 宏观数据采集 — 注入到后续AI决策
    macro_summary = {}
    try:
        from atos.macro.collector import get_macro_summary
        macro_summary = get_macro_summary()
        logger.info(f"🌍 宏观数据: {macro_summary.get('narrative', '')[:200]}")
    except Exception as e:
        logger.warning(f"宏观数据采集失败: {e}")

    deploy_hedge_adjustment = 1.0  # 默认不对冲调整
    # Beta + 对冲建议（Bug #9: 执行实际对冲）
    pos_symbols = []
    for p in account.position_list:
        s = p.get("symbol", "")
        if s and isinstance(s, str):
            pos_symbols.append(str(s))
    betas = calc_betas(pos_symbols) if pos_symbols else {}
    hedge = hedge_suggestion(account.position_list, betas, account.total_equity) if account.positions else {}
    if hedge.get("spy_shares", 0) > 0:
        logger.info(f"对冲: 组合Beta={hedge.get('portfolio_beta',0):.2f} → 卖{hedge['spy_shares']}股SPY (模拟)")
        hedge_factor = min(1.0, hedge.get("hedge_pct", 0) / 0.5)
        deploy_hedge_adjustment = 1.0 - hedge_factor * 0.3  # 最多降低30%部署
        logger.info(f"对冲调整: 部署系数 ×{deploy_hedge_adjustment:.2f}")

    # 提前初始化 drawdown_scale（在压力测试和VaR分析前就可用）
    drawdown_scale = 1.0

    # VaR + CVaR — 双轨计算：历史法 + 参数法（Beta×VIX）
    # 历史法从5分钟周期收益计算，参数法从组合Beta和VIX推算
    # 保守取最大值：周末/数据不足时参数法补上，交易日两者互相印证
    historical_var95 = 0.0
    historical_cvar95 = 0.0
    has_historical = hasattr(account, 'cycle_returns') and len(account.cycle_returns) >= 20
    if has_historical:
        historical_var95 = var_historical(account.cycle_returns)
        historical_cvar95 = cvar_historical(account.cycle_returns)

    # 参数法：基于组合Beta × VIX隐含波动率
    portfolio_beta = hedge.get("portfolio_beta", 1.0) if hedge else 1.0
    if portfolio_beta <= 0:
        portfolio_beta = 1.0
    daily_market_vol = current_vix / 100.0 / 15.87  # VIX年化 → 日波动率（√252≈15.87）
    daily_portfolio_vol = daily_market_vol * abs(portfolio_beta)
    parametric_var95 = daily_portfolio_vol * 1.645   # 95%置信度 z-score
    parametric_cvar95 = daily_portfolio_vol * 2.063  # CVaR: σ × φ(1.645)/(1-0.95)

    # 保守取最大值
    daily_var = max(historical_var95 * 17.0, parametric_var95)
    daily_cvar = max(historical_cvar95 * 17.0, parametric_cvar95)

    logger.info(f"VaR_日(95%)= {daily_var:.2%} (历史={historical_var95*17:.2%}, 参数={parametric_var95:.2%}) | "
                f"CVaR_日(95%)= {daily_cvar:.2%} | Beta={portfolio_beta:.1f} | VIX={current_vix:.1f}")
    if daily_var > 0.03:
        log_risk("VAR_BREACH", f"日VaR {daily_var:.2%} > 3%，风险偏高")

    # 压力测试（Bug #14: CRITICAL/HIGH → 实际减仓）
    if account.positions:
        stress = stress_test(account.positions)
        worst = max(stress.items(), key=lambda x: x[1]["loss_pct"])
        if worst[1]["severity"] in ("CRITICAL", "HIGH"):
            log_risk("STRESS_TEST", f"{worst[0]}: -{worst[1]['loss_pct']:.0%} → {worst[1]['severity']}")
            # 压力测试告警 → 强制提高现金比例
            drawdown_scale = min(drawdown_scale, 0.5)
            logger.warning(f"⚠️ 压力测试 {worst[1]['severity']} → 总暴露降至50%")

    # 6. 组合优化 + 再平衡（渐进式建仓）
    cash_pct = compute_cash_buffer(current_vix, regime["regime"])
    # 渐进建仓：大资金更慢，小资金更快
    tiers = {1: 0.20, 2: 0.30, 3: 0.40, 4: 0.50, 5: 0.60, 6: 0.70, 7: 0.80}
    deploy_pct = tiers.get(account.cycle_count, 0.85)
    cash_pct = max(cash_pct, 1.0 - deploy_pct)
    if account.cycle_count <= 7:
        logger.info(f"渐进建仓: 周期{account.cycle_count} → 最多部署{deploy_pct:.0%}")

    # 🔴 P1-3: Kelly回撤调整 + 压力测试 — 取最保守
    kelly_scale = dd_adj.get("scale", 1.0)
    drawdown_scale = min(drawdown_scale, kelly_scale)  # 不覆盖压力测试的结果
    if kelly_scale < 1.0:
        logger.info(f"⚠️ Kelly回撤缩放: ×{kelly_scale:.0%} (回撤{current_dd:.2%} → {dd_adj['status']})")
    if drawdown_scale <= 0:
        logger.warning("🔴 回撤超10%，暂停所有新开仓")

    # 🔴 P3-1: 策略衰减系数
    if account.strategy_decay_factor < 1.0:
        logger.info(f"⚠️ 策略衰减中，仓位系数: ×{account.strategy_decay_factor:.0%}")

    # 跟踪本周期已部署金额（大资金最小10%现金，小资金最小3%）
    min_cash = max(account.total_equity * 0.10, 50000) if account.total_equity > 100000 else account.total_equity * 0.03
    cash_pct = max(cash_pct, min_cash / account.total_equity)
    deployed_this_cycle = 0.0
    # 多种风控取最保守（min而非乘法），避免多层叠加导致锁死
    # 每个风控层独立判断 → 只取最严格的那个系数
    risk_multiplier = min(drawdown_scale, account.strategy_decay_factor,
                           deploy_hedge_adjustment, trend_factor)
    max_deploy = account.total_equity * (1.0 - cash_pct) * risk_multiplier
    if risk_multiplier < 1.0:
        logger.info(f"风控综合系数: ×{risk_multiplier:.0%} (Kelly={drawdown_scale:.0%} "
                    f"衰减={account.strategy_decay_factor:.0%} 对冲={deploy_hedge_adjustment:.0%} "
                    f"趋势={trend_factor:.0%}) → 取最保守")

    # 🟢 SPY趋势下减少max_positions: BULL=5, CAUTIOUS=3, BEAR=2
    trend_max_pos = {"BULL": 5, "CAUTIOUS": 3, "BEAR": 2}.get(spy_trend, 5)
    effective_max_pos = min(account.max_positions, trend_max_pos)
    if effective_max_pos < account.max_positions:
        logger.info(f"📊 趋势限制持仓数: {account.max_positions} → {effective_max_pos}")

    # 🔴 P1-1: 行业分散 — 计算当前行业敞口
    sector_exposure = {}
    if account.positions:
        from atos.portfolio.correlation import get_sector_exposure, SECTOR_MAP
        sector_exposure = get_sector_exposure(account.position_list, SECTOR_MAP)
        if sector_exposure:
            max_sector = max(sector_exposure, key=sector_exposure.get)
            logger.info(f"行业敞口: {len(sector_exposure)}个行业 | 最大: {max_sector}={sector_exposure[max_sector]:.1%}")

    # 🔴 P2-1: 相关矩阵 — 检测高相关性标的
    high_corr_symbols = set()
    if len(account.position_list) >= 2:
        try:
            corr_alerts = check_concentration_risk(account.position_list)
            for alert in corr_alerts:
                if alert.get("severity") == "HIGH":
                    # 标记配对中权重较小的那个
                    pair = alert["pair"]
                    logger.warning(f"高相关性告警: {pair[0]}-{pair[1]} = {alert['correlation']:.1%}")
                    high_corr_symbols.update(pair)
        except Exception as e:
            logger.debug(f"相关性检查跳过: {e}")

    try:
        # 标的选择：多只小仓位 > 单只大仓位
        max_syms = min(effective_max_pos, max(2, account.cycle_count * 1))
        raw_picks = [p["symbol"] for p in top_picks[:max_syms]] if top_picks else symbols[:max_syms]

        # ETF去重：持有同组ETF时跳过其他高度重叠的ETF
        ETF_GROUPS = [
            {"SPY", "QQQ", "IWM", "VTI", "VOO"},   # 美股宽基
            {"GLD", "SLV", "IAU"},                   # 贵金属
            {"TLT", "IEF", "SHY"},                   # 美债
            {"USO", "BNO", "UNG"},                   # 能源商品
        ]
        held_etf_groups = set()
        for sym in account.positions:
            for grp in ETF_GROUPS:
                if sym in grp:
                    held_etf_groups.add(frozenset(grp))
        # 从候选里移除同组ETF（已有1只就不再加同组的）
        raw_picks = [s for s in raw_picks if not any(
            s in grp and frozenset(grp) in held_etf_groups for grp in ETF_GROUPS
        )]

        # 🔴 P1-1: 行业分散 — 优先低配行业，降低科技股优先级
        # 科技上限30%（对标S&P500权重），其他行业20%
        DEFENSIVE_SECTORS = ["Healthcare", "Consumer", "Financial", "Industrial", "Energy"]
        SECTOR_LIMITS = {"Tech": 0.30}  # 科技允许更高（对标大盘）
        if sector_exposure:
            tech_exposure = sector_exposure.get("Tech", 0)
            tech_limit = SECTOR_LIMITS.get("Tech", 0.20)
            if tech_exposure > tech_limit:
                logger.warning(f"科技行业敞口{tech_exposure:.1%}已超{tech_limit:.0%}，降低科技股优先级")
                # 把候选标的中的防御性行业提前
                defended = [s for s in raw_picks if SECTOR_MAP.get(s, "Unknown") in DEFENSIVE_SECTORS]
                others = [s for s in raw_picks if s not in defended]
                raw_picks = defended + others

        pick_syms = raw_picks
        logger.info(f"候选标的: {len(pick_syms)}只 (上限{account.max_positions})")

        # 均分预算：每只最多配 max_deploy/max_syms
        per_symbol_budget = max_deploy / max(max_syms, 1)
        for sym in pick_syms:
            if deployed_this_cycle >= max_deploy:
                break
            if drawdown_scale <= 0:  # 🔴 P1-3: 回撤暂停
                break
            # 已有持仓或现金不足 → 跳过
            if sym in account.positions:
                continue
            # 🔴 P0-1: 止损冷却期检查
            if account.is_cooling_off(sym):
                logger.info(f"⏳ {sym} 在止损冷却期，跳过买入")
                continue
            # 🔴 P1-1: 行业敞口 — 科技30%/其他20%
            sym_sector = SECTOR_MAP.get(sym, "Unknown")
            current_sector_pct = sector_exposure.get(sym_sector, 0)
            sector_limit = SECTOR_LIMITS.get(sym_sector, 0.20)
            if current_sector_pct >= sector_limit:
                logger.info(f"🚫 {sym} 行业{sym_sector}敞口已达{current_sector_pct:.1%}，跳过")
                continue
            # 🔴 P2-1: 高相关性标的跳过
            if sym in high_corr_symbols and len(account.positions) >= account.max_positions - 2:
                logger.debug(f"高相关性跳过: {sym}")
                continue
            if account.cash < account.total_equity * account.min_cash_pct:
                logger.info(f"现金不足最低{account.min_cash_pct:.0%}，暂停建仓")
                break
            price = signals.get(sym, {}).get("price", 0)
            if price <= 0:
                continue
            # 🔴 RSI过滤器：RSI > 70 表示超买，跳过买入
            rsi_val = signals.get(sym, {}).get("rsi", 50)
            if rsi_val > 70:
                logger.debug(f"⏭ {sym} RSI={rsi_val:.1f} > 70（超买），跳过买入")
                continue
            # 🔴 MA200过滤器：价格超过MA200的15%以上，跳过买入
            ma200 = signals.get(sym, {}).get("ma200", 0)
            if ma200 > 0 and price > ma200 * 1.15:
                logger.debug(f"⏭ {sym} 价格${price:.2f} > MA200×1.15=${ma200*1.15:.2f}（过高），跳过买入")
                continue

            # 🔴 P0-2: 波动率目标仓位 — 替代暴力平分
            atr_val = signals.get(sym, {}).get("atr", 0)
            if atr_val > 0:
                daily_vol = atr_val / price  # 日波动率 ≈ ATR/价格
            else:
                daily_vol = 0.02  # 默认2%日波动率

            vol_result = vol_target_position(
                capital=min(per_symbol_budget, account.total_equity * account.max_single_pct),
                price=price,
                volatility=daily_vol,
                target_annual_vol=0.15,  # 目标年化波动率15%
                max_position_pct=account.max_single_pct,
            )
            max_shares = vol_result["shares"]
            logger.debug(f"波动率仓位 {sym}: vol={daily_vol:.2%} → {max_shares}股 ({vol_result['reason']})")

            # 🔴 P2-3: 0股买入检查 — 最少1股且金额≥$100
            if max_shares < 1 or max_shares * price < 100:
                logger.debug(f"跳过 {sym}: 仓位太小 ({max_shares}股 × ${price:.0f} = ${max_shares * price:.0f})")
                continue

            # 🔴 P2-2: 交易成本模型 — 预期收益必须 > 2×成本
            estimated_cost = max(account.min_commission, max_shares * account.commission_per_share) + price * max_shares * account.slippage_pct
            sym_score = next((p["score"] for p in top_picks if p["symbol"] == sym), 0.5) if top_picks else 0.5
            expected_return = price * max_shares * max(0.01, (sym_score - 0.5) * 0.05)  # 至少1%预期收益
            if expected_return < estimated_cost * 2:
                logger.debug(f"跳过 {sym}: 预期收益${expected_return:.0f} < 2×成本${estimated_cost:.0f}")
                continue

            ok = account.execute(sym, "BUY", max_shares, price, reason=f"渐进建仓 周期{account.cycle_count}")
            if ok:
                deployed_this_cycle += max_shares * price
                logger.info(f"建仓 {sym}: {max_shares}股 @ ${price:.2f} (vol调整)")
    except Exception as e:
        logger.error(f"组合优化失败: {e}")

    # 7. AI 决策（🔴 P1-2: 降频 — 每48周期/4小时才调用一次AI）
    AI_CYCLE_INTERVAL = 48  # 4小时 = 48 × 5分钟

    # 非交易时间跳过AI推演（保留风控、追踪止损等其余逻辑）
    if not market_ok:
        advice = {"short_term_actions": [], "long_term_actions": [], "risk_notes": ""}
        ai_risks = "N/A (非交易时间)"
        is_ai_cycle = False
        logger.debug(f"⏭ AI推演跳过: {ai_skip_reason}")
    else:
        snapshot = {
            "mode": account.mode,
            "total_equity": account.total_equity,
            "cash": account.cash,
            "positions": account.position_list,
            "market_regime": regime,
            "macro_data": macro_summary,  # 🆕 宏观数据注入
            "macro_narrative": macro_summary.get("narrative", "N/A") if macro_summary else "Not available",
            "factor_rankings": [
                {"symbol": p["symbol"], "score": p["score"],
                 "breakdown": p.get("breakdown", {})}
                for p in top_picks[:10]
            ] if top_picks else [],
            "vix": round(current_vix, 1),
            "cash_buffer_pct": round(cash_pct, 3),
            "constraints": account.get_state()["constraints"],
            "universe_long": symbols[:5],
            "universe_short": symbols[5:10] if len(symbols) > 5 else symbols,
            "universe": [{"symbol": s, **signals[s]} for s in symbols[:10] if s in signals],
            "quality_symbols": symbols[:5],
            "watch_symbols": symbols[5:10],
        }

        advice = {"short_term_actions": [], "long_term_actions": [], "risk_notes": ""}
        ai_risks = ""
        is_ai_cycle = (account.cycle_count % AI_CYCLE_INTERVAL == 0)

        if is_ai_cycle:
            try:
                advice = get_advice_v2(snapshot)
                ai_risks = advice.get("risk_notes", "")
                logger.info(f"🧠 AI周期 #{account.cycle_count} — 辩论完成")
            except Exception as e:
                logger.error(f"AI失败: {e}")
                ai_risks = f"失败: {e}"
        else:
            logger.debug(f"⏭ 非AI周期 #{account.cycle_count} (下次AI: #{((account.cycle_count // AI_CYCLE_INTERVAL) + 1) * AI_CYCLE_INTERVAL})")

        # 8. 执行 AI 建议
        available_cash = (account.cash - account.total_equity * account.min_cash_pct) * drawdown_scale * account.strategy_decay_factor
        for a in advice.get("short_term_actions", []) + advice.get("long_term_actions", []):
            action = a.get("action", "HOLD")
            if action == "HOLD":
                continue
            # 🔴 P1-3: 回撤超10%暂停AI新开仓
            if drawdown_scale <= 0 and action == "BUY":
                continue
            # 卖出不需要现金，直接执行
            if action == "SELL":
                sym = a.get("symbol", "")
                if sym in account.positions:
                    px = signals.get(sym, {}).get("price", account.positions[sym].get("last_price", 0))
                    sq = account.positions[sym]["qty"]
                    pnl_pct = (px - account.positions[sym]["avg_price"]) / account.positions[sym]["avg_price"] if account.positions[sym]["avg_price"] > 0 else 0
                    last_decision_id = account.positions[sym].get("decision_id", 0)
                    account.execute(sym, "SELL", sq, px, reason=a.get("reason", "AI卖出"))
                    account.add_to_blacklist(sym)  # AI卖出也加入冷却黑名单，避免立即重新买入
                    try:
                        from atos.ai.memory import record_outcome
                        from atos.live.kelly import save_trade
                        oc = "WIN" if pnl_pct > 0 else "LOSS"
                        record_outcome(last_decision_id, oc, pnl_pct=pnl_pct, exit_reason=a.get("reason","AI卖出"), ai_correct=(pnl_pct > 0))
                        save_trade(pnl_pct)
                    except: pass
                    logger.info(f"AI卖出: {sq}股 {sym} PnL={pnl_pct:+.2%}")
                continue
            # 买入才检查现金
            if available_cash <= 0:
                continue
            conf = a.get("confidence", 0.5)
            # 🔴 AI置信度惩罚：对于聚集在0.64-0.66的买入决策，降低0.1置信度
            if action == "BUY" and 0.64 <= conf <= 0.66:
                conf -= 0.1
                logger.debug(f"AI置信度惩罚 {a.get('symbol','')}: {conf+0.1:.2f} → {conf:.2f} (聚集值)")
            if conf < 0.4:  # 降低门槛，$1M级可以接受40%置信度的小仓位试探
                continue
            sym = a["symbol"]
            # 🔴 P0-1: 止损冷却期检查
            if account.is_cooling_off(sym):
                logger.info(f"⏳ AI买入跳过 {sym}：在止损冷却期")
                continue
            # 🔴 P1-1: 行业敞口检查
            sym_sector = SECTOR_MAP.get(sym, "Unknown")
            current_sector_pct = sector_exposure.get(sym_sector, 0)
            sector_limit = SECTOR_LIMITS.get(sym_sector, 0.20)
            if current_sector_pct >= sector_limit:
                logger.info(f"🚫 AI买入跳过 {sym}：行业{sym_sector}敞口{current_sector_pct:.1%}")
                continue
            price = signals.get(sym, {}).get("price", 0)
            if price <= 0:
                continue
            target_pct = min(a.get("target_pct", 0.03), account.max_single_pct) * drawdown_scale * account.strategy_decay_factor
            if target_pct <= 0.005:  # 少于0.5%仓位无意义
                continue
            target_val = min(account.total_equity * target_pct, available_cash)
            current_val = account.positions[sym]["qty"] * price if sym in account.positions else 0

            if a["action"] == "BUY":
                buy_val = min(target_val - current_val, available_cash)
                if buy_val > 0 and buy_val >= max(100, price):  # Bug #2: 至少能买1股且金额≥$100
                    # 🔴 P0-2: 波动率仓位调整
                    atr_val = signals.get(sym, {}).get("atr", 0)
                    daily_vol = atr_val / price if atr_val > 0 else 0.02
                    vol_adj = vol_target_position(
                        capital=buy_val, price=price, volatility=daily_vol,
                        target_annual_vol=0.15, max_position_pct=account.max_single_pct,
                    )
                    shares = vol_adj["shares"]
                    if shares < 1:
                        continue
                    # 🔴 P2-2: 成本效益检查
                    est_cost = max(account.min_commission, shares * account.commission_per_share) + price * shares * account.slippage_pct
                    if price * shares * 0.005 < est_cost:  # 预期0.5%收益 vs 成本
                        logger.debug(f"AI买入跳过 {sym}: 预期收益 < 成本")
                        continue
                    ok = account.execute(sym, "BUY", shares, price, reason=a.get("reason", ""))
                    if ok:
                        available_cash -= shares * price
                        # 追踪链路：保存decision_id到持仓，卖出时可关联结果
                        if sym in account.positions:
                            account.positions[sym]["decision_id"] = a.get("decision_id", 0)
                        logger.info(f"AI交易: BUY {shares}股 {sym} @ ${price:.2f} (vol调整)")
                # Bug #5: 删除重复的SELL分支（已在上面处理）

    # 记录周期收益（用于 VaR/CVaR + 净值曲线）
    cycle_ret = (account.total_equity - account.prev_equity) / account.prev_equity if account.prev_equity > 0 else 0
    account.cycle_returns.append(round(cycle_ret, 6))
    # 🟢 保存净值曲线（带时间戳）
    if not hasattr(account, 'equity_history'):
        account.equity_history = []
    account.equity_history.append({'time': datetime.datetime.now().isoformat(), 'equity': round(account.total_equity, 2)})
    account.prev_equity = account.total_equity

    logger.info(f"Cycle {cycle} done | Equity=${account.total_equity:,.0f} | Cycle ret={cycle_ret:+.4%}")

    # 每次周期后自动保存状态（防止崩溃丢数据）
    state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "shadow_state.json")
    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "equity_history": getattr(account, 'equity_history', []),
        "last_cycle": datetime.datetime.now().isoformat(),
        "stop_loss_blacklist": account.stop_loss_blacklist,      # Bug #1: 保存止损冷却期
        "strategy_decay_factor": account.strategy_decay_factor,  # Bug #1: 保存策略衰减系数
        "trailing_stops": {
            sym: {"trail_pct": ts.trail_pct, "highest_price": ts.highest_price,
                  "stop_price": ts.stop_price, "entry_price": ts.entry_price,
                  "confirm_cycles": getattr(ts, 'confirm_cycles', 3)}
            for sym, ts in account.trailing_stops.items()
        },
    }
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    atomic_write(state_file, json.dumps(state, indent=2))
    logger.debug(f"状态已保存: cycle={account.cycle_count}")

    # 生成透明报告
    try:
        report_path = generate_report(
            account=account, cycle=cycle, regime=regime, vix=current_vix,
            factor_rankings=[{"symbol": p["symbol"], "score": p["score"],
                              "breakdown": p.get("breakdown", {})}
                             for p in top_picks] if top_picks else [],
            trades=account.trade_history[-20:],
            ai_risks=ai_risks,
        )
        logger.info(f"透明报告: {report_path}")
    except Exception as e:
        logger.error(f"报告生成失败: {e}")


def main():
    # 进程锁：防止多个实例同时运行
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    lock_file = os.path.join(base_dir, "data", ".shadow_trader.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                old_pid = int(f.read().strip())
            # 检查旧进程是否还活着
            os.kill(old_pid, 0)
            logger.error(f"Shadow Trader 已在运行 (PID: {old_pid})，退出")
            return
        except (OSError, ValueError):
            os.remove(lock_file)
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    logger.info("Shadow Trader started (PAPER TRADING — no real money)")

    state_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "shadow_state.json"
    )

    # 加载历史状态
    if os.path.exists(state_file):
        with open(state_file) as f:
            saved = json.load(f)
        account = ShadowAccount(initial_cash=saved.get("initial_cash", ALLOCATION["short_term"]))
        account.cash = saved.get("cash", account.initial_cash)
        account.positions = saved.get("positions", {})
        account.trade_history = saved.get("trade_history", [])
        account.cycle_returns = saved.get("cycle_returns", [])
        account.cycle_count = saved.get("cycle_count", 0)
        account.prev_equity = saved.get("equity", account.initial_cash)
        # 🟢 恢复净值曲线
        account.equity_history = saved.get("equity_history", [])
        if not isinstance(account.equity_history, list):
            account.equity_history = []
        # Bug #1: 恢复冷却期和衰减系数
        account.stop_loss_blacklist = saved.get("stop_loss_blacklist", {})
        account.strategy_decay_factor = saved.get("strategy_decay_factor", 1.0)
        # 恢复追踪止损状态
        saved_ts = saved.get("trailing_stops", {})
        for sym, ts_data in saved_ts.items():
            ts = TrailingStop(
                trail_pct=ts_data.get("trail_pct", 0.05),
                confirm_cycles=ts_data.get("confirm_cycles", 3),
            )
            ts.highest_price = ts_data.get("highest_price", 0.0)
            ts.stop_price = ts_data.get("stop_price", 0.0)
            ts.entry_price = ts_data.get("entry_price", 0.0)
            ts._breach_count = 0  # 重置确认计数（重启后重新开始）
            account.trailing_stops[sym] = ts
        account.clean_blacklist()  # 清理过期条目
        logger.info(f"恢复状态: 现金${account.cash:,.0f} | 持仓{len(account.positions)}只 | 周期#{account.cycle_count} | 冷却期{len(account.stop_loss_blacklist)}只 | 追踪止损{len(account.trailing_stops)}只")
    else:
        account = ShadowAccount(initial_cash=ALLOCATION["short_term"])

    logger.info(f"资金: ${account.initial_cash:,.0f}")

    # 🆕 启动时尝试连接实时数据源 (FutuOpenD)
    account._use_realtime = True
    try:
        from atos.live.realtime_feeds import get_feed
        feed = get_feed()
        if feed.is_connected():
            logger.info(f"✅ 实时数据源: {feed.get_data_source()} — 延迟 < 1 秒")
        else:
            logger.warning(f"⚠️ 实时数据源不可用: {feed.get_data_source()}")
            account._use_realtime = False
            logger.info("↘️ 降级到 yfinance (15-20分钟延迟) — 历史指标仍使用 yfinance")
    except Exception as e:
        logger.warning(f"⚠️ 实时数据源加载失败: {e}")
        account._use_realtime = False
        logger.info("↘️ 使用 yfinance 作为数据源")

    logger.info("Press Ctrl+C to stop")

    cycle = 0
    while True:
        try:
            cycle += 1
            run_shadow_cycle(account, cycle)
            time.sleep(5 * 60)  # 5分钟周期
        except KeyboardInterrupt:
            logger.info("手动停止")
            os.remove(lock_file) if os.path.exists(lock_file) else None
            break
        except Exception as e:
            err = str(e)[:100]
            logger.error(f"周期崩溃: {err}，60秒后继续")
            # 402 = API 没钱，降频省成本
            if "402" in err or "Payment Required" in err:
                logger.warning("⚠️ DeepSeek API 余额不足！请充值。降频到30分钟。")
                time.sleep(30 * 60)
            else:
                try: time.sleep(60)
                except KeyboardInterrupt: break

    # 保存最终状态
    state = {
        "initial_cash": account.initial_cash,
        "cash": account.cash,
        "positions": account.positions,
        "trade_history": account.trade_history,
        "cycle_returns": account.cycle_returns,
        "cycle_count": account.cycle_count,
        "equity": account.total_equity,
        "equity_history": getattr(account, 'equity_history', []),
        "stopped_at": datetime.datetime.now().isoformat(),
        "stop_loss_blacklist": account.stop_loss_blacklist,
        "strategy_decay_factor": account.strategy_decay_factor,
        "trailing_stops": {
            sym: {"trail_pct": ts.trail_pct, "highest_price": ts.highest_price,
                  "stop_price": ts.stop_price, "entry_price": ts.entry_price,
                  "confirm_cycles": getattr(ts, 'confirm_cycles', 3)}
            for sym, ts in account.trailing_stops.items()
        },
    }
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass
    logger.info(f"State saved. Final equity: ${account.total_equity:,.0f}")
    logger.info(f"Total trades: {len(account.trade_history)}")


if __name__ == "__main__":
    main()
