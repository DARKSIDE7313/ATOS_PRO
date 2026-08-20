#!/usr/bin/env python3
"""
ATOS Institutional v2 — Integration Test Matrix
=================================================
规格书 §14: 故障注入与集成测试

测试整个 v29 安全链: 状态机 + 风控门 + kill switch + execute() 接入
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.system_state import SystemStateMachine, SystemState
from atos.core.risk_gate import get_gate, OrderIntent, PreTradeRiskGate
from atos.core.kill_switch import KillSwitch, KILL_FILE
from atos.core.daily_session import (layer0_bootstrap, layer1_premarket,
                                      layer2_intraday_permission, us_market_phase)

PASS, FAIL = 0, 0

def check(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


class FakeAccount:
    def __init__(self, equity=300_000, cash=150_000):
        self.total_equity = equity
        self.peak_equity = equity
        self.cash = cash
        self.positions = {'QQQ': {'qty': 50, 'avg_price': 727.0, 'last_price': 723.0}}


def reset_state():
    sm = SystemStateMachine.get()
    sm._state = SystemState.PAPER
    sm._save()
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
    # 清空已见订单
    gate = get_gate()
    gate._seen_orders.clear()


print("═══ ATOS v29 集成测试矩阵 ═══\n")

# ── 1. 状态机 ─────────────────────────────────────────
print("1. 状态机转移")
reset_state()
sm = SystemStateMachine.get()
check("PAPER→LIVE_NORMAL", sm.transition(SystemState.LIVE_NORMAL, "t"))
check("LIVE_NORMAL→RISK_REDUCED", sm.transition(SystemState.RISK_REDUCED, "t"))
check("RISK_REDUCED→PAPER 被拒", not sm.transition(SystemState.PAPER, "t"))
check("任何状态→KILL", (sm.kill("t") or sm.state == SystemState.KILL_SWITCH))
check("KILL 不能直接回 LIVE_NORMAL", not sm.transition(SystemState.LIVE_NORMAL, "t"))

# ── 2. 风控门 ─────────────────────────────────────────
print("\n2. Pre-Trade Risk Gate")
reset_state()
sm.transition(SystemState.LIVE_NORMAL, "t")
gate = get_gate()
acct = FakeAccount()

d = gate.check(OrderIntent('NVDA', 'BUY', 100, 220.0, 'v28 test'), acct)
check("正常 BUY 通过", d.decision == 'APPROVE')

d = gate.check(OrderIntent('NVDA', 'BUY', 100, 220.0, 'v28 test'), acct)
check("重复订单被拒 (幂等)", d.decision == 'REJECT')

d = gate.check(OrderIntent('BAD', 'BUY', -1, -5.0, 'bad'), acct)
check("非法参数被拒", d.decision == 'REJECT')

# 个股上限 12%
d = gate.check(OrderIntent('MSFT', 'BUY', 200, 480.0, 'v28 big'), acct)
check("超单仓被减量", d.decision in ('REDUCE', 'REJECT') and d.approved_quantity < 200)

# QQQ ETF 65% 上限 (不受 12% 限制)
d = gate.check(OrderIntent('QQQ', 'BUY', 150, 723.0, 'v28 core'), acct)
check("QQQ ETF 不受 12% 限制", d.approved_quantity > 50)

# ── 3. Kill Switch ───────────────────────────────────
print("\n3. Kill Switch")
reset_state()
sm.transition(SystemState.LIVE_NORMAL, "t")
ks = KillSwitch()
acct2 = FakeAccount()

check("正常不触发", not ks.check(acct2))

# 日内亏损触发
acct2.total_equity = 290_000
ks._day_start_equity = 300_000
ks._day_date = datetime.datetime.now().date()
check("日内亏损 -3.3% 触发", ks.check(acct2))
check("触发后状态=KILL_SWITCH", sm.state == SystemState.KILL_SWITCH)

# kill 状态下订单被拒
d = gate.check(OrderIntent('AAPL', 'BUY', 10, 310.0, 'v28 kill test'), acct2)
check("KILL 状态下 BUY 被拒", d.decision == 'REJECT')

# 复位
check("授权复位", ks.reset(authorized=True))

# 人工文件触发
open(KILL_FILE, 'w').write('test')
sm.transition(SystemState.LIVE_NORMAL, "t", force=True)
acct3 = FakeAccount()
ks2 = KillSwitch()
check("人工文件触发", ks2.check(acct3))
os.remove(KILL_FILE)
reset_state()

# ── 4. 每日时间线 ────────────────────────────────────
print("\n4. 每日时间线")
l0 = layer0_bootstrap(FakeAccount())
check("Layer 0 自检", l0['go'])

l1 = layer1_premarket(FakeAccount())
check("Layer 1 盘前", 'risk_multiplier' in l1)

# 回撤降档
acct_dd = FakeAccount(equity=282_000)  # dd=6% from peak 300k
acct_dd.peak_equity = 300_000
l1_dd = layer1_premarket(acct_dd)
check("回撤降档正确", l1_dd['risk_multiplier'] < 1.0)

# 时段权限
check("开盘禁新仓", not layer2_intraday_permission('OPENING_AUCTION')['can_open'])
check("主时段可交易", layer2_intraday_permission('OPEN')['can_open'])
check("收盘只平仓", not layer2_intraday_permission('CLOSING')['can_open'] and layer2_intraday_permission('CLOSING')['can_close'])
check("闭市全禁", not layer2_intraday_permission('CLOSED')['can_close'])

# 阶段判断函数不崩溃
check("阶段判断运行", us_market_phase() in ['PRE_MARKET','OPEN','OPENING_AUCTION','CLOSING','POST_MARKET','CLOSED'])

# ── 5. fail closed ───────────────────────────────────
print("\n5. Fail Closed 原则")
reset_state()
class BrokenAccount:
    @property
    def total_equity(self):
        raise RuntimeError("boom")
    positions = {}
    cash = 0

d = gate.check(OrderIntent('NVDA', 'BUY', 10, 220.0, 'v28 failtest'), BrokenAccount())
check("账户异常 → REJECT (fail closed)", d.decision == 'REJECT')

# ── 结果 ─────────────────────────────────────────────
print(f"\n{'═'*50}")
print(f"  测试结果: {PASS} 通过 / {FAIL} 失败")
print(f"{'═'*50}")
sys.exit(0 if FAIL == 0 else 1)
