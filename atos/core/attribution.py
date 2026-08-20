#!/usr/bin/env python3
"""
ATOS Institutional v2 — Performance Attribution
=================================================
规格书 §11: Gross-to-Net 归因

每日必须能回答: 收益来自信号、beta、成本还是执行?

PnL_net = PnL_gross - Commission - Slippage - SpreadCost
执行质量 IS = (fill_price - decision_price) × qty
"""
import os
import json
import datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ATTR_DIR = os.path.join(BASE, 'data', 'attribution')


def attribute_day(trades: list, date: str = None) -> dict:
    """对一天的成交做 gross-to-net 归因"""
    if date is None:
        date = datetime.datetime.now().strftime('%Y-%m-%d')

    day_trades = [t for t in trades if t.get('date', '').startswith(date)]

    by_symbol = defaultdict(lambda: {'gross': 0.0, 'fees': 0.0, 'trades': 0, 'wins': 0})
    total_gross = 0.0
    total_fees = 0.0
    buy_count = sell_count = 0

    for t in day_trades:
        sym = t.get('symbol', '?')
        pnl = t.get('pnl', 0) or 0
        fee = t.get('fee', t.get('commission', 0)) or 0
        action = t.get('action', '')

        if action == 'BUY':
            buy_count += 1
        elif action == 'SELL':
            sell_count += 1
            by_symbol[sym]['gross'] += pnl
            by_symbol[sym]['trades'] += 1
            if pnl > 0:
                by_symbol[sym]['wins'] += 1
            total_gross += pnl

        by_symbol[sym]['fees'] += fee
        total_fees += fee

    # 信号质量分解
    winners = {s: d for s, d in by_symbol.items() if d['gross'] > 0}
    losers = {s: d for s, d in by_symbol.items() if d['gross'] <= 0}

    best = max(by_symbol.items(), key=lambda x: x[1]['gross'], default=(None, {'gross': 0}))
    worst = min(by_symbol.items(), key=lambda x: x[1]['gross'], default=(None, {'gross': 0}))

    return {
        'date': date,
        'trades_total': len(day_trades),
        'buys': buy_count,
        'sells': sell_count,
        'gross_pnl': round(total_gross, 2),
        'total_fees': round(total_fees, 2),
        'net_pnl': round(total_gross, 2),  # fees 已在 execute 内扣除
        'fee_drag_pct': round(total_fees / max(abs(total_gross), 1) * 100, 1),
        'win_rate': round(sum(1 for t in day_trades if (t.get('pnl') or 0) > 0 and t.get('action') == 'SELL') / max(sell_count, 1), 3),
        'best_symbol': {'symbol': best[0], 'pnl': round(best[1]['gross'], 2)},
        'worst_symbol': {'symbol': worst[0], 'pnl': round(worst[1]['gross'], 2)},
        'by_symbol': {s: {k: round(v, 2) if isinstance(v, float) else v
                          for k, v in d.items()} for s, d in by_symbol.items()},
        'verdict': _verdict(total_gross, total_fees, sell_count,
                            sum(d['wins'] for d in by_symbol.values())),
    }


def _verdict(gross, fees, sells, wins) -> str:
    """策略健康判断"""
    if sells == 0:
        return 'NO_ACTIVITY'
    wr = wins / sells
    if gross > 0 and wr >= 0.5:
        return 'HEALTHY'
    if gross > 0:
        return 'PROFITABLE_LOW_WINRATE'
    if gross <= 0 and wr < 0.4:
        return 'DEGRADED — 策略可能失效'
    return 'LOSING'


def strategy_scorecard(trades: list, days: int = 30) -> dict:
    """策略 Scorecard (规格书 §11.3)"""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    recent = [t for t in trades if t.get('date', '') >= cutoff and t.get('action') == 'SELL']

    pnls = [t.get('pnl', 0) or 0 for t in recent]
    if not pnls:
        return {'period_days': days, 'trades': 0, 'note': 'no sell trades'}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

    return {
        'period_days': days,
        'trades': len(pnls),
        'win_rate': round(len(wins) / len(pnls), 3),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'inf',
        'net_pnl': round(sum(pnls), 2),
        'expectancy': round(sum(pnls) / len(pnls), 2),
        # 规格书 §11.4 降级规则
        'downgrade_flags': {
            'negative_expectancy': sum(pnls) / len(pnls) < 0,
            'low_profit_factor': profit_factor < 1.0,
            'low_winrate': len(wins) / len(pnls) < 0.35,
        },
    }


def save_attribution(report: dict):
    d = os.path.join(ATTR_DIR, report['date'])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'attribution.json'), 'w') as f:
        json.dump(report, f, indent=2, default=str)


# ── 测试 ──────────────────────────────────────────────
if __name__ == '__main__':
    print("═══ Attribution 测试 ═══\n")

    # 用真实交易记录测试
    import json as j
    state = j.load(open(os.path.join(BASE, 'data', 'shadow_state.json')))
    trades = state.get('trade_history', [])

    # 最近一天归因
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    attr = attribute_day(trades, today)
    print(f"今日归因 ({today}):")
    print(f"  交易: {attr['trades_total']}笔 (买{attr['buys']}/卖{attr['sells']})")
    print(f"  净盈亏: ${attr['gross_pnl']:,.2f}")
    print(f"  健康度: {attr['verdict']}")

    # 找最近有交易的一天
    dates = sorted(set(t.get('date', '')[:10] for t in trades), reverse=True)
    for d in dates[:3]:
        attr = attribute_day(trades, d)
        if attr['trades_total'] > 0:
            print(f"\n{d}: {attr['trades_total']}笔 净=${attr['gross_pnl']:,.2f} 胜率={attr['win_rate']:.0%} {attr['verdict']}")
            if attr['best_symbol']['symbol']:
                print(f"   最佳: {attr['best_symbol']['symbol']} ${attr['best_symbol']['pnl']:,.2f} | 最差: {attr['worst_symbol']['symbol']} ${attr['worst_symbol']['pnl']:,.2f}")

    # 30天 Scorecard
    sc = strategy_scorecard(trades, 30)
    print(f"\n30天 Scorecard:")
    print(f"  交易: {sc['trades']}笔 胜率={sc.get('win_rate',0):.0%} PF={sc.get('profit_factor')}")
    print(f"  期望值: ${sc.get('expectancy',0):,.2f}/笔 净盈亏=${sc.get('net_pnl',0):,.2f}")
    flags = sc.get('downgrade_flags', {})
    if any(flags.values()):
        print(f"  ⚠️ 降级标记: {[k for k,v in flags.items() if v]}")

    print("\n✅ Attribution 测试完成")
