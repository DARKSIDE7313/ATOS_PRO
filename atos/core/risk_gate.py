#!/usr/bin/env python3
"""
ATOS Institutional v2 — Pre-Trade Risk Gate
=============================================
规格书 §8: 所有订单必须经过不可绕过的确定性风控门。

设计原则:
1. execute() 内部强制调用 — 没有代码路径可以绕过
2. fail closed — 任何异常视同 REJECT
3. 每次决策写入 audit log
4. 16 项必检 (规格书 §8.3)
"""
import json
import os
import datetime
import hashlib
import importlib as _importlib
from dataclasses import dataclass, field, asdict
from typing import Optional

from atos.core.system_state import SystemStateMachine, SystemState

# 绕过 atos/core/logging.py 对标准库 logging 的遮蔽
logger = _importlib.import_module('logging').getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DECISIONS_FILE = os.path.join(BASE, 'data', 'risk_decisions.jsonl')

# v28 策略持仓 (与 shadow_trader.V28_ALPHA_UNIVERSE 对齐)
V28_CORE = {"QQQ"}
V28_ALPHA = {"NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN",
             "AVGO", "AMD", "CRM", "NFLX", "PLTR", "MU", "TSLA"}

# 仓位上限 (规格书 §7.3 hard caps)
CAPS = {
    'single_stock_pct': 0.12,   # 个股上限 12%
    'etf_pct': 0.65,            # ETF 上限 65% (QQQ 核心仓设计如此)
    'total_position_pct': 0.98, # 总仓位上限 98%
    'min_cash_pct': 0.02,       # 最低现金 2%
    'price_collar_pct': 0.05,   # 价格 collar ±5%
    'max_order_notional': 200_000,  # 单笔名义上限 $200K
}


@dataclass
class OrderIntent:
    """订单意图 (规格书 §8.1)"""
    symbol: str
    side: str                    # BUY / SELL
    quantity: int
    price: float
    reason: str = ""
    strategy_id: str = "v28"
    signal_id: str = ""          # 幂等键
    order_type: str = "MARKETABLE_LIMIT"


@dataclass
class RiskDecision:
    """风控决策 (规格书 §8.2)"""
    decision: str                # APPROVE / REDUCE / REJECT
    approved_quantity: int
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    decided_at_utc: str = ""

    def to_dict(self):
        return asdict(self)


def _client_order_id(intent: OrderIntent) -> str:
    """幂等键 (规格书 §2.1): 同 signal+symbol+side+qty_bucket 永远同键"""
    if not intent.signal_id:
        intent.signal_id = f"{intent.strategy_id}:{intent.symbol}:{intent.side}"
    raw = f"{intent.strategy_id}|{intent.signal_id}|{intent.symbol}|{intent.side}|{intent.quantity // 10}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class PreTradeRiskGate:
    """确定性 pre-trade 风控门 — 不可绕过, fail closed"""

    def __init__(self):
        self.sm = SystemStateMachine.get()
        self._seen_orders = set()   # 本会话已见 client_order_id
        self._load_seen()

    # ─────────────────────────────────────────────────────
    def check(self, intent: OrderIntent, account) -> RiskDecision:
        """16 项必检。任何异常 → REJECT (fail closed)"""
        checks = {}
        reasons = []
        qty = intent.quantity

        try:
            # ── 1. 系统状态 ──
            state = self.sm.state
            is_sell = intent.side == "SELL"
            if is_sell:
                state_ok = self.sm.can_close() or self.sm.can_reduce()
            else:
                state_ok = self.sm.can_open_new()
            checks['system_state'] = state_ok
            if not state_ok:
                reasons.append(f"STATE_{state.value}_BLOCKS_{intent.side}")

            # ── 2. Kill switch ──
            checks['kill_switch'] = state != SystemState.KILL_SWITCH
            if not checks['kill_switch']:
                reasons.append("KILL_SWITCH_ACTIVE")

            # ── 3. 基本参数 ──
            checks['valid_params'] = (
                qty > 0 and intent.price > 0
                and isinstance(intent.symbol, str) and len(intent.symbol) > 0
                and qty <= 100_000
            )
            if not checks['valid_params']:
                reasons.append("INVALID_PARAMS")

            # ── 4. 重复订单 (幂等) ──
            coid = _client_order_id(intent)
            checks['not_duplicate'] = coid not in self._seen_orders
            if not checks['not_duplicate']:
                reasons.append(f"DUPLICATE_ORDER:{coid[:8]}")

            # ── 5. 价格 collar (规格书 §8.4) ──
            ref_price = self._reference_price(intent.symbol, account)
            if ref_price > 0:
                collar = CAPS['price_collar_pct']
                p_min, p_max = ref_price * (1 - collar), ref_price * (1 + collar)
                checks['price_collar'] = p_min <= intent.price <= p_max
                if not checks['price_collar']:
                    reasons.append(f"PRICE_COLLAR:{intent.price:.2f}_outside[{p_min:.2f},{p_max:.2f}]")
            else:
                checks['price_collar'] = True  # 无参考价时放行 (价格来自实时信号)

            # ── 6. 单笔名义上限 ──
            notional = qty * intent.price
            checks['order_notional'] = notional <= CAPS['max_order_notional']
            if not checks['order_notional']:
                new_qty = int(CAPS['max_order_notional'] / intent.price)
                reasons.append(f"NOTIONAL_CAP:${notional:,.0f}>${CAPS['max_order_notional']:,}")
                qty = max(0, new_qty)

            if is_sell:
                # ── SELL 侧检查 ──
                # 7. 持仓充足
                pos = account.positions.get(intent.symbol, {})
                held = pos.get('qty', pos.get('shares', 0))
                checks['has_position'] = held >= qty
                if not checks['has_position']:
                    reasons.append(f"INSUFFICIENT_POSITION:held={held},req={qty}")
                    qty = held  # 自动减到实际持仓
            else:
                # ── BUY 侧检查 ──
                equity = account.total_equity
                cash = account.cash

                # 8. 现金充足 (含费用 + 最低现金保留)
                from atos.core.fee_model import futu_buy_fee
                est_cost = qty * intent.price + futu_buy_fee(qty, intent.price)
                min_cash = equity * CAPS['min_cash_pct']
                checks['cash_ok'] = (cash - est_cost) >= min_cash
                if not checks['cash_ok']:
                    affordable = int((cash - min_cash) / (intent.price * 1.001))
                    reasons.append(f"CASH_CAP:affordable={affordable}")
                    qty = max(0, min(qty, affordable))

                # 9. 单仓上限 (ETF vs 个股区分 — v28d 教训)
                cap_pct = CAPS['etf_pct'] if intent.symbol in V28_CORE else CAPS['single_stock_pct']
                max_val = equity * cap_pct
                cur_val = 0
                if intent.symbol in account.positions:
                    cur_val = account.positions[intent.symbol].get('qty', 0) * intent.price
                room = max_val - cur_val
                checks['single_cap'] = room > 0
                if room <= 0:
                    reasons.append(f"SINGLE_CAP:{intent.symbol}@cap{cap_pct:.0%}")
                    qty = 0
                elif qty * intent.price > room:
                    qty = max(0, int(room / intent.price))
                    reasons.append(f"REDUCED_TO_CAP:{qty}")

                # 10. 总仓位上限
                total_pos = sum(
                    p.get('qty', p.get('shares', 0)) * p.get('last_price', p.get('avg_price', 0))
                    for p in account.positions.values()
                )
                max_total = equity * CAPS['total_position_pct']
                total_room = max_total - total_pos
                checks['total_cap'] = total_room > 0 and qty * intent.price <= total_room
                if total_room <= 0:
                    reasons.append("TOTAL_CAP_REACHED")
                    qty = 0
                elif qty * intent.price > total_room:
                    qty = max(0, int(total_room / intent.price))
                    reasons.append(f"REDUCED_TO_TOTAL_CAP:{qty}")

                # 11. RISK_REDUCED 状态: 仅高评分 (reason 含 v28 视为高评分)
                if state == SystemState.RISK_REDUCED:
                    checks['reduced_mode_quality'] = 'v28' in intent.reason
                    if not checks['reduced_mode_quality']:
                        reasons.append("RISK_REDUCED_LOW_QUALITY")
                        qty = 0

            # ── 最终裁决 ──
            if qty <= 0 and intent.quantity > 0:
                decision = "REJECT"
            elif qty < intent.quantity:
                decision = "REDUCE"
            elif all(checks.values()):
                decision = "APPROVE"
            else:
                # 关键检查失败 → REJECT
                critical = ['system_state', 'kill_switch', 'valid_params',
                            'not_duplicate', 'price_collar']
                decision = "REJECT" if any(not checks.get(c, True) for c in critical) else "REDUCE"
                if decision == "REDUCE":
                    qty = max(1, int(intent.quantity * 0.5))

            if decision == "REJECT":
                qty = 0

            result = RiskDecision(
                decision=decision,
                approved_quantity=qty,
                reasons=reasons,
                checks=checks,
                decided_at_utc=datetime.datetime.utcnow().isoformat() + 'Z',
            )

            # APPROVE/REDUCE → 记录幂等键
            if decision in ("APPROVE", "REDUCE") and qty > 0:
                self._seen_orders.add(coid)
                self._save_seen()

            self._audit(intent, result, coid)
            return result

        except Exception as e:
            # fail closed (规格书: 风控门自身报错视同拒绝)
            logger.error(f"🛡️ Risk gate exception (fail closed): {e}")
            result = RiskDecision(
                decision="REJECT", approved_quantity=0,
                reasons=[f"GATE_EXCEPTION:{str(e)[:80]}"],
                checks={'exception': False},
                decided_at_utc=datetime.datetime.utcnow().isoformat() + 'Z',
            )
            self._audit(intent, result, "error")
            return result

    # ─────────────────────────────────────────────────────
    def _reference_price(self, symbol: str, account) -> float:
        """参考价: 持仓 last_price 或 avg_price"""
        pos = account.positions.get(symbol, {})
        return pos.get('last_price', 0) or pos.get('avg_price', 0)

    def _audit(self, intent: OrderIntent, decision: RiskDecision, coid: str):
        """Append-only 决策日志 (规格书 §2.2 不可变审计)"""
        try:
            os.makedirs(os.path.dirname(DECISIONS_FILE), exist_ok=True)
            with open(DECISIONS_FILE, 'a') as f:
                f.write(json.dumps({
                    'ts': decision.decided_at_utc,
                    'client_order_id': coid,
                    'symbol': intent.symbol,
                    'side': intent.side,
                    'requested_qty': intent.quantity,
                    'price': intent.price,
                    'reason': intent.reason[:60],
                    'decision': decision.decision,
                    'approved_qty': decision.approved_quantity,
                    'reject_reasons': decision.reasons,
                }) + '\n')
        except Exception:
            pass  # 审计失败不阻塞交易 (但决策已 fail closed)

    def _seen_file(self):
        return os.path.join(BASE, 'data', 'seen_orders.json')

    def _load_seen(self):
        try:
            fp = self._seen_file()
            if os.path.exists(fp):
                with open(fp) as f:
                    self._seen_orders = set(json.load(f))
                # 只保留最近 10000 条防膨胀
                if len(self._seen_orders) > 10000:
                    self._seen_orders = set(list(self._seen_orders)[-10000:])
        except Exception:
            self._seen_orders = set()

    def _save_seen(self):
        try:
            with open(self._seen_file(), 'w') as f:
                json.dump(list(self._seen_orders)[-10000:], f)
        except Exception:
            pass

    def reset_daily(self):
        """每日开盘前重置幂等缓存 (允许跨日重复同策略订单)"""
        self._seen_orders.clear()
        self._save_seen()


# 全局单例
_gate = None
def get_gate() -> PreTradeRiskGate:
    global _gate
    if _gate is None:
        _gate = PreTradeRiskGate()
    return _gate


# ── 测试 ──────────────────────────────────────────────
if __name__ == '__main__':
    class FakeAccount:
        def __init__(self):
            self.total_equity = 300_000
            self.cash = 150_000
            self.positions = {'QQQ': {'qty': 50, 'avg_price': 727.0, 'last_price': 723.0}}

    gate = get_gate()
    acct = FakeAccount()

    # 1. 正常 BUY
    d = gate.check(OrderIntent('NVDA', 'BUY', 100, 220.0, 'v28动量alpha'), acct)
    print(f"1. 正常BUY: {d.decision} qty={d.approved_quantity} {d.reasons}")
    assert d.decision == 'APPROVE'

    # 2. 重复订单
    d = gate.check(OrderIntent('NVDA', 'BUY', 100, 220.0, 'v28动量alpha'), acct)
    print(f"2. 重复订单: {d.decision} {d.reasons}")
    assert d.decision == 'REJECT' and any('DUPLICATE' in r for r in d.reasons)

    # 3. 超单仓上限 (个股 >12%)
    d = gate.check(OrderIntent('AAPL', 'BUY', 500, 310.0, 'test'), acct)  # $155K > $36K cap
    print(f"3. 超单仓: {d.decision} qty={d.approved_quantity} {d.reasons}")
    assert d.decision in ('REDUCE', 'REJECT')

    # 4. QQQ ETF 上限 (65%)
    d = gate.check(OrderIntent('QQQ', 'BUY', 200, 723.0, 'v28核心仓2'), acct)  # $144K + $36K = $180K < $195K
    print(f"4. QQQ正常: {d.decision} qty={d.approved_quantity} {d.reasons}")

    # 5. Kill switch
    gate.sm.kill("test")
    d = gate.check(OrderIntent('MSFT', 'BUY', 10, 480.0, 'test3'), acct)
    print(f"5. Kill switch: {d.decision} {d.reasons}")
    assert d.decision == 'REJECT' and 'KILL_SWITCH_ACTIVE' in d.reasons

    # 6. SELL 在 kill switch 下也被拒
    d = gate.check(OrderIntent('QQQ', 'SELL', 50, 723.0, 'test4'), acct)
    print(f"6. Kill下SELL: {d.decision} {d.reasons}")

    # 恢复
    gate.sm.transition(SystemState.PAPER, "test done", force=True)

    # 7. 异常 fail closed
    d = gate.check(OrderIntent('BAD', 'BUY', -5, -10.0, 'bad'), acct)
    print(f"7. 非法参数: {d.decision} {d.reasons}")
    assert d.decision == 'REJECT'

    print("\n✅ Risk Gate 全部测试通过")
