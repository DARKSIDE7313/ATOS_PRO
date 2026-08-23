"""
ATOS PRO — 权益追踪与周期结算 (Phase 5 框架重塑)
====================================================
从 shadow_trader.py _finalize_cycle 抽离的纯结算逻辑:
  - compute_cycle_return():   从 equity_history 精确回溯的周期收益
  - record_cycle_equity():    equity_history / cycle_returns 追加与修剪
  - update_perf_tracker():    v17 统一绩效追踪 (每20周期汇报)
  - record_daily_returns():   每日收益记录
  - write_day_changes():      日内涨跌文件 (Dashboard 数据桥, Futu prev_close)

逻辑逐字保留。
"""

import os
import json
import datetime

from atos.core.logging import get_logger
from atos.shadow.cycle_state import CycleState

logger = get_logger("shadow_trader")


def compute_cycle_return(account, current_eq: float) -> float:
    """记录周期收益 — 从 equity_history 精确计算 (回溯最近非当前条目)"""
    prev_eq = None
    for e in reversed(account.equity_history):
        eq_val = e.get("equity") if isinstance(e, dict) else e
        if isinstance(eq_val, (int, float)) and eq_val > 0:
            prev_eq = eq_val
            break

    if prev_eq is None:
        prev_eq = account.initial_cash

    # 精确计算 cycle return
    cycle_ret = (current_eq - prev_eq) / prev_eq if prev_eq > 0 else 0
    # 防御 nan
    if isinstance(cycle_ret, float) and str(cycle_ret) in ("nan", "inf", "-inf"):
        cycle_ret = 0.0
    return cycle_ret


def record_cycle_equity(account, current_eq: float, cycle_ret: float) -> None:
    """追加 cycle_returns / equity_history (各保留最近 1000/500 条) + 更新峰值"""
    account.cycle_returns.append(round(cycle_ret, 6))
    # 只保留最近 1000 个周期收益
    if len(account.cycle_returns) > 1000:
        account.cycle_returns = account.cycle_returns[-1000:]

    account.equity_history.append({
        "time": datetime.datetime.now().isoformat(),
        "equity": current_eq,
    })
    # 只保留最近 500 个历史点
    if len(account.equity_history) > 500:
        account.equity_history = account.equity_history[-500:]

    account.prev_equity = current_eq

    # 更新峰值
    account.peak_equity = max(account.peak_equity, current_eq)


def update_perf_tracker(current_eq: float, cycle_ret: float, cycle: int) -> None:
    """v17: 统一绩效追踪 — 每20周期汇报"""
    state = CycleState.get()
    try:
        from atos.core.performance import get_tracker, init_tracker
        if state.perf_inited is False:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            init_tracker(base_dir)
            state.perf_inited = True
        perf = get_tracker()
        perf.update(current_eq, cycle_ret)
        if cycle % 20 == 0:
            m = perf.get_metrics()
            logger.info(f"📊 绩效#{cycle}: Sharpe={m.get('sharpe',0):.2f} Sortino={m.get('sortino',0):.2f} "
                       f"Calmar={m.get('calmar',0):.2f} WR={m.get('win_rate',0):.1f}% "
                       f"PF={m.get('profit_factor',0):.2f} 评级={m.get('grade','?')}")
        perf.save()
    except Exception as e:
        logger.debug(f"绩效追踪跳过: {e}")


def record_daily_returns(current_eq: float, account) -> None:
    """记录每日收益"""
    try:
        from atos.core.daily_returns import record_daily
        record_daily(current_eq, len(account.trade_history), len(account.positions))
    except Exception:
        pass


def write_day_changes(account) -> None:
    """v5.1: 写日内涨跌数据供 Dashboard 读取（从 Futu OpenD 获取 prev_close）

    Dashboard 数据桥: FILE BRIDGE 模式 — shadow_trader 写文件, Dashboard 读文件。
    """
    try:
        from atos.shadow.state_store import get_state_file_path
        dc_file = os.path.join(os.path.dirname(get_state_file_path()), "day_changes.json")
        day_data = {}
        # 尝试从 Futu OpenD 批量获取日内涨跌
        try:
            # v28i: 先 TCP 检查，避免 OpenQuoteContext 内部重试阻塞
            # v28k: 再加线程超时兜底 — TCP 通但需验证码时构造函数仍会无限重试
            import socket as _sock
            _s = _sock.create_connection(('127.0.0.1', 11111), timeout=2)
            _s.close()
            from futu import RET_OK
            from atos.live.realtime_feeds import open_quote_context_with_timeout
            pos_syms = [s for s in account.positions if isinstance(account.positions.get(s), dict)]
            if pos_syms:
                ctx = open_quote_context_with_timeout('127.0.0.1', 11111, timeout=5.0)
                if ctx is None:
                    raise RuntimeError("OpenQuoteContext timeout (验证码/登录过期)")
                ret, data = ctx.get_market_snapshot([f'US.{s}' for s in pos_syms])
                ctx.close()
                if ret == RET_OK:
                    for _, row in data.iterrows():
                        sym = row['code'].replace('US.', '')
                        day_data[sym] = {
                            'prev_close': round(float(row.get('prev_close_price', 0) or 0), 2),
                            'day_chg': round(float(row.get('change_val', 0) or 0), 2),
                            'day_pct': round(float(row.get('change_rate', 0) or 0), 2),
                        }
        except Exception:
            pass  # Futu 不可用时 fallback 到零值

        # Fallback: 对 Futu 没覆盖的持仓用 current price
        for sym, pos in account.positions.items():
            if sym in day_data: continue
            if not isinstance(pos, dict): continue
            px = pos.get('last_price', 0) or 0
            if px > 0:
                day_data[sym] = {'prev_close': round(px,2), 'day_chg': 0.0, 'day_pct': 0.0}

        with open(dc_file, 'w') as f:
            json.dump(day_data, f)
    except Exception:
        pass
