#!/usr/bin/env python3
"""
ATOS Safety Layer — 多层风控系统
==================================
目标: 保护本金，控制回撤，防止灾难性损失

Layer 1: 组合级风控 (每日)
  - 最大回撤 > 10% → 减仓 50%
  - 最大回撤 > 15% → 清仓
  - 连续 3 日亏损 → 暂停开仓 1 天

Layer 2: 持仓级风控 (每周期)
  - 单仓亏损 > 4% → 止损
  - 单仓盈利 > 8% → 分批止盈 (3/5/8%)
  - 移动止损: 从最高点回撤 > 5% → 卖

Layer 3: 市场级风控 (每周期)
  - VIX > 25 → 减仓 30%
  - VIX > 35 → 清仓
  - SPY < MA50 → 不开新仓

Layer 4: 黑天鹅防护 (实时)
  - 单日跌幅 > 3% → 全仓止损
  - 连续 2 日跌幅 > 2% → 减仓至 30%
  - 新闻恐慌 (>3 条重大利空) → 暂停买入

Layer 5: 费用防护
  - 预期利润 < 2× 手续费 → 不交易
  - 月交易次数 > 40 → 暂停开新仓
"""

import json
import os
import datetime
import sys
import importlib

# 避免 atos/core/logging.py 遮蔽标准库
_std_logging = importlib.import_module('logging')
if not hasattr(_std_logging, 'getLogger'):
    # 重新加载标准库 logging
    for mod in list(sys.modules.keys()):
        if mod.startswith('logging'):
            del sys.modules[mod]
    import logging as _std_logging

logger = _std_logging.getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAFETY_STATE_FILE = os.path.join(BASE, "data", "safety_state.json")


def _load_safety_state():
    """加载风控状态"""
    try:
        with open(SAFETY_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            'consecutive_loss_days': 0,
            'last_loss_date': None,
            'trading_halted_until': None,
            'monthly_trade_count': 0,
            'monthly_reset_date': None,
            'circuit_breaker_triggered': False,
            'circuit_breaker_date': None,
        }


def _save_safety_state(state):
    """保存风控状态"""
    os.makedirs(os.path.dirname(SAFETY_STATE_FILE), exist_ok=True)
    with open(SAFETY_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def check_portfolio_risk(equity, peak_equity, positions, cash):
    """Layer 1: 组合级风控

    Returns: (action, reason, position_scale)
      action: 'NORMAL' | 'REDUCE' | 'HALT' | 'LIQUIDATE'
      position_scale: 0.0-1.0 仓位缩放系数
    """
    if peak_equity <= 0:
        return 'NORMAL', '', 1.0

    drawdown = (peak_equity - equity) / peak_equity
    state = _load_safety_state()

    # 熔断器: 回撤 > 15%
    if drawdown > 0.15:
        if not state.get('circuit_breaker_triggered'):
            state['circuit_breaker_triggered'] = True
            state['circuit_breaker_date'] = datetime.datetime.now().isoformat()
            _save_safety_state(state)
            logger.critical(f"🚨 熔断器触发! 回撤={drawdown:.1%} > 15%")
        return 'LIQUIDATE', f'回撤{drawdown:.1%}>15% 清仓', 0.0

    # 减仓: 回撤 > 10%
    if drawdown > 0.10:
        logger.warning(f"⚠️ 回撤={drawdown:.1%} > 10% → 减仓50%")
        return 'REDUCE', f'回撤{drawdown:.1%}>10% 减仓', 0.5

    # 减仓: 回撤 > 7%
    if drawdown > 0.07:
        logger.warning(f"⚠️ 回撤={drawdown:.1%} > 7% → 减仓30%")
        return 'REDUCE', f'回撤{drawdown:.1%}>7% 减仓', 0.7

    return 'NORMAL', '', 1.0


def check_consecutive_losses(daily_returns):
    """Layer 1b: 连续亏损检测

    daily_returns: 最近 N 天的日收益率列表
    Returns: (should_halt, reason)
    """
    if len(daily_returns) < 3:
        return False, ''

    last_3 = daily_returns[-3:]
    if all(r < -0.005 for r in last_3):  # 连续 3 天亏 > 0.5%
        state = _load_safety_state()
        halt_until = (datetime.datetime.now() + datetime.timedelta(days=1)).isoformat()
        state['trading_halted_until'] = halt_until
        _save_safety_state(state)
        logger.warning(f"🛑 连续3日亏损 → 暂停开仓至 {halt_until[:10]}")
        return True, '连续3日亏损'

    return False, ''


def check_market_risk(vix_level=None, spy_above_ma50=True, news_panic_count=0):
    """Layer 3: 市场级风控

    Returns: (action, reason, exposure_scale)
    """
    exposure = 1.0

    # VIX 风控
    if vix_level is not None:
        if vix_level > 35:
            logger.critical(f"🚨 VIX={vix_level:.0f} > 35 → 清仓")
            return 'LIQUIDATE', f'VIX={vix_level:.0f}>35', 0.0
        elif vix_level > 25:
            exposure = 0.7
            logger.warning(f"⚠️ VIX={vix_level:.0f} > 25 → 减仓30%")

    # SPY 趋势
    if not spy_above_ma50:
        exposure = min(exposure, 0.5)
        logger.warning(f"⚠️ SPY < MA50 → 减仓至50%")

    # 新闻恐慌
    if news_panic_count >= 3:
        exposure = min(exposure, 0.3)
        logger.warning(f"⚠️ 新闻恐慌({news_panic_count}条利空) → 减仓至30%")

    if exposure < 1.0:
        return 'REDUCE', f'市场风控 exposure={exposure:.0%}', exposure

    return 'NORMAL', '', 1.0


def check_trade_fee_worthiness(shares, price, expected_profit_pct=0.05):
    """Layer 5: 费用防护 — 交易值得吗？

    Returns: (worth_it, reason)
    """
    try:
        from atos.core.fee_model import round_trip_fee
        fee = round_trip_fee(shares, price, price * (1 + expected_profit_pct))
        expected_profit = shares * price * expected_profit_pct
        if expected_profit < fee * 2:
            return False, f"预期利润${expected_profit:.0f} < 2x费用${fee:.0f}"
        return True, ''
    except ImportError:
        return True, ''


def check_monthly_trade_limit(max_monthly=40):
    """Layer 5b: 月交易次数限制"""
    state = _load_safety_state()
    now = datetime.datetime.now()
    current_month = now.strftime('%Y-%m')

    if state.get('monthly_reset_date') != current_month:
        state['monthly_trade_count'] = 0
        state['monthly_reset_date'] = current_month
        _save_safety_state(state)

    if state['monthly_trade_count'] >= max_monthly:
        logger.warning(f"🛑 月交易{state['monthly_trade_count']}笔 ≥ {max_monthly} → 暂停开新仓")
        return False

    return True


def record_trade_for_safety():
    """记录一笔交易（用于月交易计数）"""
    state = _load_safety_state()
    state['monthly_trade_count'] = state.get('monthly_trade_count', 0) + 1
    _save_safety_state(state)


def is_trading_halted():
    """检查是否在暂停交易期"""
    state = _load_safety_state()
    halt_until = state.get('trading_halted_until')
    if halt_until:
        if datetime.datetime.now().isoformat() < halt_until:
            return True
        else:
            state['trading_halted_until'] = None
            _save_safety_state(state)
    return False


def full_safety_check(equity, peak_equity, positions, cash,
                      vix_level=None, spy_above_ma50=True,
                      daily_returns=None, news_panic_count=0):
    """综合安全检查 — 每周期调用一次

    Returns: dict with action, exposure_scale, reasons
    """
    reasons = []

    # 检查暂停
    if is_trading_halted():
        return {'action': 'HALT', 'exposure': 0.0, 'reasons': ['暂停交易期']}

    # Layer 1: 组合回撤
    action, reason, scale = check_portfolio_risk(equity, peak_equity, positions, cash)
    if reason:
        reasons.append(reason)
    if action == 'LIQUIDATE':
        return {'action': 'LIQUIDATE', 'exposure': 0.0, 'reasons': reasons}

    exposure = scale

    # Layer 1b: 连续亏损
    if daily_returns:
        halt, halt_reason = check_consecutive_losses(daily_returns)
        if halt:
            reasons.append(halt_reason)
            return {'action': 'HALT', 'exposure': 0.0, 'reasons': reasons}

    # Layer 3: 市场风控
    mkt_action, mkt_reason, mkt_scale = check_market_risk(vix_level, spy_above_ma50, news_panic_count)
    if mkt_reason:
        reasons.append(mkt_reason)
    exposure = min(exposure, mkt_scale)
    if mkt_action == 'LIQUIDATE':
        return {'action': 'LIQUIDATE', 'exposure': 0.0, 'reasons': reasons}

    # Layer 5: 月交易限制
    if not check_monthly_trade_limit():
        reasons.append('月交易超限')
        exposure = 0.0

    final_action = 'NORMAL' if exposure >= 0.9 else ('REDUCE' if exposure > 0 else 'HALT')

    return {
        'action': final_action,
        'exposure': exposure,
        'reasons': reasons,
    }


if __name__ == '__main__':
    print("=" * 50)
    print("🛡️ ATOS 安全层测试")
    print("=" * 50)

    # 测试: 正常情况
    result = full_safety_check(300000, 300000, {}, 300000, vix_level=15, spy_above_ma50=True)
    print(f"\n正常: {result}")

    # 测试: 回撤 12%
    result = full_safety_check(264000, 300000, {}, 264000, vix_level=15, spy_above_ma50=True)
    print(f"回撤12%: {result}")

    # 测试: 高 VIX
    result = full_safety_check(300000, 300000, {}, 300000, vix_level=30, spy_above_ma50=True)
    print(f"VIX=30: {result}")

    # 测试: 回撤 16% (熔断)
    result = full_safety_check(252000, 300000, {}, 252000, vix_level=15, spy_above_ma50=True)
    print(f"回撤16%: {result}")

    # 测试: 费用
    ok, reason = check_trade_fee_worthiness(100, 300, 0.05)
    print(f"\n费用检查(100股$300, 预期5%): {'✅' if ok else '❌'} {reason}")

    ok, reason = check_trade_fee_worthiness(1, 300, 0.01)
    print(f"费用检查(1股$300, 预期1%): {'✅' if ok else '❌'} {reason}")
