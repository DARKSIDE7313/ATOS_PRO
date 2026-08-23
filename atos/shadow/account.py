"""
ATOS PRO — Shadow 模拟账户 (Phase 5 框架重塑)
================================================
从 shadow_trader.py (上帝对象, 2059行) 抽离的账户领域对象。
职责: 现金/持仓/成交/冷却黑名单/费用与滑点/风控门强制路由。

代码逐字迁移自原 shadow_trader.py ShadowAccount 类 (125-543 行)，
策略与风控数值零改动。唯一变更:
  - 成交后持久化改调 atos.shadow.state_store.save_account_state
    (原模块级函数 _save_account_state 的上移, 消除状态序列化重复构造)

所有订单仍强制经过 v29 Pre-Trade Risk Gate (不可绕过, fail-closed)。
"""

import math
import datetime

from atos.core.logging import get_logger, log_trade
from atos.live.risk_manager import record_fill, COOLDOWN_CYCLES
from atos.debugger.safety_net import safe_price, is_duplicate_order
from atos.shadow.state_store import save_account_state

logger = get_logger("shadow_trader")


class ShadowAccount:
    """本地模拟账户"""

    def __init__(self, initial_cash: float = 1000000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}            # {symbol: {qty, avg_price, last_price, decision_id}}
        self.trade_history = []
        self.cycle_returns = []
        self.trailing_stops = {}       # {symbol: TrailingStop}
        self.cycle_count = 0
        self.prev_equity = initial_cash
        self.commission_per_share = 0.005
        self.min_commission = 1.0
        self.slippage_pct = 0.001      # 0.1%
        self.stop_loss_blacklist = {}  # {symbol: sell_cycle} — 任何卖出都加入
        self.strategy_decay_factor = 1.0
        self.peak_equity = initial_cash
        self.equity_history = []
        self._last_ai_cycle = 0        # 上次AI运行的周期
        self._max_positions_override = None  # M6: AI 降仓时的仓位上限覆盖（可写）

    # ---- 冷却期 ----
    def is_cooling_off(self, symbol: str) -> bool:
        """检查冷却期（任何卖出都会触发，不仅仅是止损）。

        BUGFIX P2 2026-06-12: 使用真实的动态冷却长度判断。
        """
        if symbol in self.stop_loss_blacklist:
            entry = self.stop_loss_blacklist[symbol]
            if isinstance(entry, dict):
                sold_cycle = entry.get("sold_cycle", 0)
                cooldown = entry.get("cooldown", COOLDOWN_CYCLES)
            else:
                # 兼容旧格式（纯整数）
                sold_cycle = entry
                cooldown = COOLDOWN_CYCLES
                # 升级为新格式
                self.stop_loss_blacklist[symbol] = {
                    "sold_cycle": sold_cycle,
                    "cooldown": cooldown,
                }
            if self.cycle_count - sold_cycle < cooldown:
                return True
            else:
                del self.stop_loss_blacklist[symbol]
        return False

    def add_to_blacklist(self, symbol: str):
        """任何卖出都加入冷却黑名单。

        BUGFIX P2 2026-06-12: 存储 sold_cycle + 真实 cooldown 长度。
        之前只存了 cycle_count，比较时仍然用的固定 COOLDOWN_CYCLES，
        导致动态冷却实际没生效。
        """
        vol_mult = 1.0
        try:
            if hasattr(self, 'positions') and symbol in self.positions:
                pos = self.positions[symbol]
                lp = pos.get("last_price", pos.get("avg_price", 0))
                atr_val = pos.get("atr", 0)
                if atr_val > 0 and lp > 0:
                    daily_vol = atr_val / lp
                    if daily_vol > 0.03:
                        vol_mult = 1.5
                    elif daily_vol < 0.01:
                        vol_mult = 0.7
        except Exception:
            pass

        dynamic_cooldown = min(int(COOLDOWN_CYCLES * vol_mult), 12)  # Fix: 上限12周期≈1小时
        self.stop_loss_blacklist[symbol] = {
            "sold_cycle": self.cycle_count,
            "cooldown": dynamic_cooldown,
        }
        logger.info(f"🔒 冷却: {symbol} → 禁止买入至周期#{self.cycle_count + dynamic_cooldown} (波动率系数×{vol_mult:.1f})")

    def clean_blacklist(self):
        """清理过期条目（按真实 cooldown 判断）"""
        expired = []
        for s, entry in self.stop_loss_blacklist.items():
            if isinstance(entry, dict):
                sold = entry.get("sold_cycle", 0)
                cd = entry.get("cooldown", COOLDOWN_CYCLES)
            else:
                sold = entry
                cd = COOLDOWN_CYCLES
            if self.cycle_count - sold >= cd:
                expired.append(s)
        for s in expired:
            del self.stop_loss_blacklist[s]

    # ---- 属性 ----
    @property
    def total_equity(self) -> float:
        """计算总权益 — 防御 NaN 污染"""
        pos_val = 0.0
        for p in self.positions.values():
            lp = p.get("last_price", p.get("avg_price", 0))
            qty = p.get("shares", p.get("qty", p.get("quantity", 0)))  # Fix: 兼容 shares/qty/quantity 三个键名
            # 防御 nan / None / 负数
            if lp is None: lp = 0
            if isinstance(lp, float) and math.isnan(lp):
                lp = p.get("avg_price", 0)
            if lp is None: lp = 0
            if isinstance(lp, float) and math.isnan(lp):
                lp = 0
            if lp <= 0:
                ap = p.get("avg_price", 0)
                lp = ap if ap and (isinstance(ap, float) and not math.isnan(ap)) else 0
            if lp <= 0:
                lp = 0
            pos_val += qty * lp
        return self.cash + pos_val

    @property
    def position_list(self) -> list:
        result = []
        for sym, p in self.positions.items():
            qty = p.get("shares", p.get("qty", p.get("quantity", 0)))  # Fix: 兼容多键名
            last = p.get("last_price", p["avg_price"])
            pnl_pct = (last - p["avg_price"]) / p["avg_price"] if p["avg_price"] > 0 else 0
            result.append({
                "symbol": sym, "qty": qty, "avg_price": p["avg_price"],
                "last": last, "mkt_val": last * qty,
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

    # Bug #7 注释: mode 名称反映风险偏好而非仓位数量。
    # VERY_AGGRESSIVE(3仓)=资金极少时只能集中火力, AGGRESSIVE(15仓)=有资金后可以分散,
    # MODERATE(8仓)=适中, CONSERVATIVE(10仓)=大资金但适度分散保持流动性。
    @property
    def max_positions(self) -> int:
        # M6: AI 降仓可动态覆盖（可写属性），否则按 mode 返回默认值
        if self._max_positions_override is not None:
            return self._max_positions_override
        return {"VERY_AGGRESSIVE": 3, "AGGRESSIVE": 15, "MODERATE": 12, "CONSERVATIVE": 15}[self.mode]

    @max_positions.setter
    def max_positions(self, value: int):
        self._max_positions_override = max(1, int(value))

    @property
    def max_single_pct(self) -> float:
        return 0.12          # v19: 单仓上限 12%（从 20% 降低，专业基金标准 ≤12%，
                              # 防止单票黑天鹅事件造成过度集中损失）

    # v28: ETF 单仓上限（QQQ/SPY 是分散化ETF，不是单票）
    ETF_MAX_PCT = 0.65       # QQQ 可以到 65%

    @property
    def min_cash_pct(self) -> float:
        return 0.02  # v28: 满仓策略，最低现金 2%

    def get_state(self) -> dict:
        pos_val = sum(
            p.get("shares", p.get("qty", 0)) * p.get("last_price", p.get("avg_price", 0))
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
        """更新持仓价格 — 强制防御 NaN"""
        for sym, p in self.positions.items():
            if sym in signals:
                px = signals[sym].get("price", None)
                # 防御 nan：px 必须是 > 0 的数字
                if px is None or not isinstance(px, (int, float)):
                    continue
                if isinstance(px, float) and math.isnan(px):
                    continue
                if px <= 0:
                    continue
                p["last_price"] = px
                # Fix #10: 存储 ATR 供滑点计算
                atr = signals[sym].get("atr", 0)
                if atr > 0:
                    p["atr"] = atr

    # ---- 执行 ----
    def execute(self, symbol: str, action: str, shares: int,
                price: float, reason: str = "", ai_decision_id: int = 0) -> bool:
        """执行交易（带完整安全检查 + 风控记录）

        v29 Institutional: 所有订单强制经过 Pre-Trade Risk Gate。
        没有任何代码路径可以绕过风控门 (规格书 §8)。
        """
        if shares <= 0 or not symbol or not isinstance(symbol, str):
            return False
        if safe_price(price) is None:
            return False
        if shares > 100000:
            logger.error(f"数量异常: {shares}股")
            return False

        # ═══ v29: PRE-TRADE RISK GATE — 不可绕过 ═══
        try:
            from atos.core.risk_gate import get_gate, OrderIntent
            intent = OrderIntent(
                symbol=symbol, side=action, quantity=int(shares),
                price=float(price), reason=reason,
                strategy_id="v28" if reason.startswith("v28") else "legacy",
            )
            decision = get_gate().check(intent, self)
            if decision.decision == "REJECT":
                logger.warning(f"🛡️ 风控门拒绝: {action} {symbol} {shares}股 | {decision.reasons}")
                return False
            if decision.approved_quantity < shares:
                logger.info(f"🛡️ 风控门减量: {symbol} {shares}→{decision.approved_quantity}股 | {decision.reasons}")
                shares = decision.approved_quantity
                if shares <= 0:
                    return False
        except Exception as e:
            # fail closed: 风控门异常 = 拒绝交易
            logger.error(f"🛡️ 风控门异常 (fail closed): {e}")
            return False

        # v28: 跳过冷却和重复检查（v28 是季度再平衡策略，不需要这些限制）
        _is_v28 = reason.startswith("v28")
        if not _is_v28 and is_duplicate_order(symbol, action, shares):
            return False

        # BUGFIX P1: 执行层冷却拦截 — 任何 BUY/ADD 先查冷却
        if not _is_v28 and action in ("BUY", "ADD") and self.is_cooling_off(symbol):
            logger.debug(f"🚫 冷却拦截: {action} {symbol} (执行层)")
            return False

        # 硬性现金下限
        if action == "BUY":
            min_cash = self.total_equity * self.min_cash_pct
            try:
                from atos.core.fee_model import futu_buy_fee
                estimated_cost = price * shares + futu_buy_fee(shares, price)
            except ImportError:
                estimated_cost = price * shares + max(self.min_commission, shares * self.commission_per_share)
            if self.cash - estimated_cost < min_cash:
                affordable = int((self.cash - min_cash) / (price * 1.001))
                if affordable <= 0:
                    return False
                shares = affordable

        # 单仓上限（硬约束，不允许超过）
        # v28: ETF (QQQ/SPY/TLT/GLD/IWM) 用更高的上限 — ETF 是分散化的
        _ETF_SYMBOLS = {"QQQ", "SPY", "TLT", "GLD", "IWM", "SLV", "USO", "IBB"}
        if symbol in _ETF_SYMBOLS:
            max_single_val = self.total_equity * self.ETF_MAX_PCT
        else:
            max_single_val = self.total_equity * self.max_single_pct
        current_val = self.positions[symbol].get("qty", self.positions[symbol].get("shares", 0)) * price if symbol in self.positions else 0
        max_buy = max_single_val - current_val
        if max_buy <= 0 and action == "BUY":
            logger.debug(f"  {symbol} 已达单仓上限 (${max_single_val:,.0f})")
            return False

        # 总仓位上限（v28: 满仓策略 98%，留 2% 现金缓冲）
        if action == "BUY" or action == "ADD":
            total_pos_val = sum(p.get("qty", p.get("shares", 0)) * (p.get("last_price", p["avg_price"])) for p in self.positions.values())
            estimated_buy = price * shares
            max_total_pos = self.total_equity * 0.98
            if total_pos_val + estimated_buy > max_total_pos:
                available = max_total_pos - total_pos_val
                if available <= 0:
                    logger.debug(f"  总仓位已满 (${total_pos_val:,.0f}/${max_total_pos:,.0f})")
                    return False
                estimated_shares = max(1, int(available / price))
                if estimated_shares < shares:
                    shares = estimated_shares

        # 滑点 — Fix #10: 动态滑点，基于波动率
        daily_vol = 0.005
        if symbol in self.positions:
            atr_val = self.positions[symbol].get("atr", 0)
            if atr_val > 0 and price > 0:
                daily_vol = atr_val / price
        dynamic_slip = max(0.0005, min(0.005, daily_vol * 0.25))
        slip = price * dynamic_slip
        fill = price + slip if action == "BUY" else price - slip
        # v28: Futu 真实费用模型
        try:
            from atos.core.fee_model import futu_buy_fee, futu_sell_fee
            comm = futu_buy_fee(shares, fill) if action == "BUY" else futu_sell_fee(shares, fill)
        except ImportError:
            comm = max(self.min_commission, shares * self.commission_per_share)
        pnl = 0.0  # Fix: 声明在外层，log_trade 可以访问

        if action == "BUY":
            buy_val = fill * shares
            max_buy_val = max_single_val - current_val
            if buy_val > max_buy_val:
                shares = max(1, int(max_buy_val / fill))
            if shares <= 0:
                return False

            cost = fill * shares + comm
            if cost > self.cash:
                affordable = max(1, int((self.cash - self.min_commission) / fill))
                if affordable <= 0:
                    return False
                shares = affordable
                cost = fill * shares + comm

            self.cash -= cost
            if symbol in self.positions:
                old = self.positions[symbol]
                old_shares = old.get("shares", old.get("qty", 0))
                total_qty = old_shares + shares
                old_cost = old_shares * old["avg_price"]
                self.positions[symbol] = {
                    "shares": total_qty, "qty": total_qty,
                    "avg_price": (old_cost + fill * shares) / total_qty,
                    "last_price": fill,
                    "ai_decision_id": ai_decision_id or old.get("ai_decision_id", 0),
                    "buy_time": old.get("buy_time", datetime.datetime.now().isoformat()),
                }
            else:
                self.positions[symbol] = {"shares": shares, "qty": shares, "avg_price": fill, "last_price": fill,
                                          "ai_decision_id": ai_decision_id,  # v19: 追踪AI决策
                                          "buy_time": datetime.datetime.now().isoformat()}  # v17: Triple-Barrier时间追踪

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": 0, "pnl_pct": 0,
                "reason": reason,
                "source": "factor_engine",
            })

        elif action == "SELL":
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
            actual_qty = pos.get("shares", pos.get("qty", 0))  # Fix: 用实际持仓量
            if actual_qty < shares:
                shares = actual_qty

            pnl = (fill - pos["avg_price"]) * shares
            pnl_pct = (fill - pos["avg_price"]) / pos["avg_price"] if pos["avg_price"] > 0 else 0
            self.cash += fill * shares - comm

            # 记录风控
            record_fill(pnl, self.total_equity)

            # 保存到 trade_stats 供 Kelly 学习
            try:
                from atos.live.kelly import save_trade
                result = save_trade(pnl_pct)
                logger.info(f"[Kelly] 交易记录: {symbol} PnL={pnl_pct:.2%} total_trades={result.get('total_trades',0)} WR={result.get('win_rate',0):.1%}")
            except Exception as e:
                logger.warning(f"[Kelly] save_trade failed: {e}")

            pos["qty"] -= shares
            pos["shares"] = pos["qty"]  # Fix: 同步 shares 键

            # v19 Fix: 反馈闭环 — 根据持仓中记录的AI决策ID追踪结果
            try:
                from atos.ai.memory import record_outcome
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
                # 从持仓数据中获取AI决策ID（买入时记录的）
                decision_id = pos.get("ai_decision_id", 0) if isinstance(pos, dict) else 0
                if decision_id > 0:
                    record_outcome(decision_id, outcome, pnl_pct, 0, reason)
                    logger.info(f"[AI追踪] #{decision_id} → {outcome} PnL={pnl_pct:.2%}")
            except Exception:
                pass

            if pos["qty"] <= 0:
                del self.positions[symbol]
                if symbol in self.trailing_stops:
                    del self.trailing_stops[symbol]

            self.trade_history.append({
                "date": datetime.datetime.now().isoformat(),
                "symbol": symbol, "action": action, "shares": shares,
                "price": round(fill, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
                "reason": reason,
            })

            # v3: 任何卖出都触发冷却期
            self.add_to_blacklist(symbol)

        elif action == "ADD":
            # 加仓：按比例增持，但不超过单仓上限
            target_val = self.total_equity * 0.03  # 每次加仓3%
            add_val = min(target_val, max_buy, self.cash - self.total_equity * self.min_cash_pct)
            if add_val < price * 1.001:
                return False
            add_shares = max(1, int(add_val / fill))
            if add_shares <= 0:
                return False
            return self.execute(symbol, "BUY", add_shares, price, reason)

        log_trade(symbol, action, shares, price, pnl=pnl, reason=reason)
        # P0 修复: 每次成交后立即保存状态 (防止中断丢失)
        save_account_state(self)  # Fix: self 就是 account，execute() 是 ShadowAccount 的方法
        return True
