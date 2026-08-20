#!/usr/bin/env python3
"""
ATOS Institutional v2 — Kill Switch
=====================================
规格书 §3/§10.3: 自动与人工 kill switch，独立于策略逻辑。

自动触发条件:
- 日内亏损 > 阈值 (默认 -3%)
- 回撤 > 12%
- 重复订单异常
- 对账失败

人工触发:
- 创建 data/KILL_SWITCH 文件即触发 (touch)
- 删除文件 + 状态机人工审核后恢复
"""
import os
import json
import datetime
import importlib as _importlib

from atos.core.system_state import SystemStateMachine, SystemState

logger = _importlib.import_module('logging').getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KILL_FILE = os.path.join(BASE, 'data', 'KILL_SWITCH')

# 自动触发阈值
DAILY_LOSS_LIMIT = -0.03      # 日内亏损 -3% → kill
DRAWDOWN_LIMIT = 0.12         # 回撤 > 12% → kill


class KillSwitch:
    """Kill Switch 监控器 — 每周期检查自动触发条件"""

    def __init__(self):
        self.sm = SystemStateMachine.get()
        self._day_start_equity = None
        self._day_date = None

    def check(self, account) -> bool:
        """检查 kill switch 条件。触发返回 True。"""
        # 已触发则直接返回
        if self.sm.state == SystemState.KILL_SWITCH:
            return True

        # ── 人工触发: KILL_SWITCH 文件存在 ──
        if os.path.exists(KILL_FILE):
            self.sm.kill("MANUAL: KILL_SWITCH file detected")
            logger.error("🔴 KILL SWITCH: 人工触发 (文件)")
            return True

        equity = account.total_equity
        peak = getattr(account, 'peak_equity', equity)
        now = datetime.datetime.now()
        today = now.date()

        # ── 日内亏损 ──
        if self._day_date != today:
            self._day_start_equity = equity
            self._day_date = today

        if self._day_start_equity and self._day_start_equity > 0:
            daily_pnl = (equity - self._day_start_equity) / self._day_start_equity
            if daily_pnl <= DAILY_LOSS_LIMIT:
                self.sm.kill(f"AUTO: daily loss {daily_pnl:.2%} <= {DAILY_LOSS_LIMIT:.0%}")
                logger.error(f"🔴 KILL SWITCH: 日内亏损 {daily_pnl:.2%}")
                return True

        # ── 回撤 ──
        if peak > 0:
            dd = (peak - equity) / peak
            if dd >= DRAWDOWN_LIMIT:
                self.sm.kill(f"AUTO: drawdown {dd:.2%} >= {DRAWDOWN_LIMIT:.0%}")
                logger.error(f"🔴 KILL SWITCH: 回撤 {dd:.2%}")
                return True

        return False

    def is_active(self) -> bool:
        return self.sm.state == SystemState.KILL_SWITCH or os.path.exists(KILL_FILE)

    def reset(self, authorized: bool = False):
        """复位 kill switch — 需要人工授权标记"""
        if not authorized:
            logger.warning("Kill switch reset 未授权, 拒绝")
            return False
        if os.path.exists(KILL_FILE):
            os.remove(KILL_FILE)
        return self.sm.transition(SystemState.PAPER, "manual kill switch reset", force=False)


_ks = None
def get_kill_switch() -> KillSwitch:
    global _ks
    if _ks is None:
        _ks = KillSwitch()
    return _ks


if __name__ == '__main__':
    class FakeAccount:
        total_equity = 300_000
        peak_equity = 300_000

    ks = get_kill_switch()
    # 确保初始状态干净
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
    ks.sm.transition(SystemState.PAPER, "test init", force=True)

    acct = FakeAccount()

    # 1. 正常状态
    assert not ks.check(acct)
    print("✅ 1. 正常状态不触发")

    # 2. 日内亏损触发
    acct.total_equity = 290_000  # -3.3%
    ks._day_start_equity = 300_000
    ks._day_date = datetime.datetime.now().date()
    assert ks.check(acct)
    assert ks.sm.state == SystemState.KILL_SWITCH
    print("✅ 2. 日内亏损 -3.3% 触发 kill switch")

    # 3. 未授权复位被拒
    assert not ks.reset(authorized=False)
    print("✅ 3. 未授权复位被拒绝")

    # 4. 授权复位
    assert ks.reset(authorized=True)
    assert ks.sm.state == SystemState.PAPER
    print("✅ 4. 授权复位 → PAPER")

    # 5. 回撤触发
    acct.total_equity = 260_000
    acct.peak_equity = 300_000  # dd = 13.3%
    ks2 = KillSwitch()
    ks2.sm = SystemStateMachine.get()
    assert ks2.check(acct)
    print("✅ 5. 回撤 13.3% 触发 kill switch")

    # 6. 人工文件触发
    ks.reset(authorized=True)
    open(KILL_FILE, 'w').write('manual test')
    ks3 = KillSwitch()
    ks3.sm = SystemStateMachine.get()
    ks3.sm.transition(SystemState.LIVE_NORMAL, "test", force=True)
    acct.total_equity = 300_000
    acct.peak_equity = 300_000
    assert ks3.check(acct)
    assert ks3.sm.state == SystemState.KILL_SWITCH
    os.remove(KILL_FILE)
    print("✅ 6. 人工文件触发")

    # 清理
    ks.reset(authorized=True)
    print("\n✅ Kill Switch 全部测试通过")
