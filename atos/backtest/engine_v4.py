#!/usr/bin/env python3
"""
ATOS Backtest v4 — QQQ Core + Alpha Stock Picks
================================================
最终策略: 80% QQQ 核心 + 20% 精选个股 alpha

不择时，始终满仓。个股只选超大市值科技股（长期赢家）。
每季度再平衡。
"""

import json
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULT_FILE = os.path.join(BASE, "data", "backtest_v4_result.json")

# 候选 alpha 股票
ALPHA_STOCKS = ["NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "AMD", "CRM", "NFLX"]


def run_v4(start_date='2016-01-01', end_date='2026-08-01', capital=300000):
    import yfinance as yf

    print("=" * 60)
    print("📊 回测 v4 — QQQ Core + Alpha")
    print("=" * 60)

    # 下载
    all_syms = list(set(ALPHA_STOCKS + ['QQQ', 'SPY']))
    data = {}
    for sym in all_syms:
        try:
            hist = yf.Ticker(sym).history(start=start_date, end=end_date)
            if len(hist) > 100:
                data[sym] = hist
        except Exception:
            pass
    print(f"数据: {len(data)}/{len(all_syms)}")

    spy = data['SPY']
    qqq = data['QQQ']
    years = len(spy) / 252
    spy_annual = ((spy['Close'].iloc[-1] / spy['Close'].iloc[0]) ** (1 / years) - 1)
    qqq_annual = ((qqq['Close'].iloc[-1] / qqq['Close'].iloc[0]) ** (1 / years) - 1)
    print(f"基准: SPY={spy_annual:.2%} QQQ={qqq_annual:.2%}")

    # 计算动量
    for sym in ALPHA_STOCKS:
        if sym in data:
            df = data[sym]
            df['mom_3m'] = df['Close'].pct_change(63)
            df['mom_6m'] = df['Close'].pct_change(126)
            df['mom_score'] = df['mom_3m'] * 0.4 + df['mom_6m'] * 0.6

    # 策略变体
    strategies = {
        'QQQ_100': {'core': 1.0, 'alpha_n': 0, 'desc': '100% QQQ'},
        'QQQ80_alpha2': {'core': 0.80, 'alpha_n': 2, 'desc': '80% QQQ + 2 alpha'},
        'QQQ80_alpha3': {'core': 0.80, 'alpha_n': 3, 'desc': '80% QQQ + 3 alpha'},
        'QQQ70_alpha3': {'core': 0.70, 'alpha_n': 3, 'desc': '70% QQQ + 3 alpha'},
        'QQQ60_alpha5': {'core': 0.60, 'alpha_n': 5, 'desc': '60% QQQ + 5 alpha'},
        'QQQ80_NVDA_only': {'core': 0.80, 'alpha_n': 1, 'desc': '80% QQQ + NVDA'},
    }

    results = {}

    for strat_name, cfg in strategies.items():
        cash = capital
        positions = {}  # {sym: shares}
        total_fees = 0
        trades = 0
        equity_curve = []
        last_rebalance = 0

        for i, date in enumerate(spy.index):
            if i < 126:  # 需要 6 个月数据
                continue

            # 估值
            port_val = cash
            for sym, sh in positions.items():
                if sym in data and date in data[sym].index:
                    port_val += sh * data[sym].loc[date, 'Close']

            # 每季度再平衡 (63 交易日)
            if i - last_rebalance >= 63:
                last_rebalance = i

                # 目标配置
                core_val = port_val * cfg['core']
                alpha_val = port_val * (1 - cfg['core'])

                # 卖出现有持仓
                for sym in list(positions.keys()):
                    if date in data[sym].index:
                        price = data[sym].loc[date, 'Close']
                        fee = futu_sell_fee(positions[sym], price)
                        total_fees += fee
                        cash += positions[sym] * price - fee
                        trades += 1
                    del positions[sym]

                # 买 QQQ 核心
                if cfg['core'] > 0 and date in qqq.index:
                    q_price = qqq.loc[date, 'Close']
                    q_shares = max(1, int(core_val / q_price))
                    fee = futu_buy_fee(q_shares, q_price)
                    total_fees += fee
                    cash -= q_shares * q_price + fee
                    positions['QQQ'] = q_shares
                    trades += 1

                # 买 alpha 个股
                if cfg['alpha_n'] > 0:
                    # 排名
                    candidates = []
                    for sym in ALPHA_STOCKS:
                        if sym in data and date in data[sym].index:
                            row = data[sym].loc[date]
                            if not pd.isna(row.get('mom_score', np.nan)):
                                candidates.append((sym, row['mom_score'], row['Close']))

                    candidates.sort(key=lambda x: -x[1])
                    per_stock = alpha_val / max(cfg['alpha_n'], 1)

                    for sym, score, price in candidates[:cfg['alpha_n']]:
                        shares = max(1, int(per_stock / price))
                        fee = futu_buy_fee(shares, price)
                        total_fees += fee
                        cost = shares * price + fee
                        if cost <= cash:
                            cash -= cost
                            positions[sym] = shares
                            trades += 1

            equity_curve.append((date, port_val))

        # 最终估值
        final = cash
        for sym, sh in positions.items():
            if sym in data:
                final += sh * data[sym]['Close'].iloc[-1]

        total_ret = (final / capital) - 1
        annual = (1 + total_ret) ** (1 / years) - 1

        # 回撤
        peak = capital
        max_dd = 0
        for _, v in equity_curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

        # 夏普
        if len(equity_curve) > 1:
            vals = [e[1] for e in equity_curve]
            rets = pd.Series(vals).pct_change().dropna()
            sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
        else:
            sharpe = 0

        results[strat_name] = {
            'desc': cfg['desc'],
            'annual': annual,
            'total_ret': total_ret,
            'final': final,
            'max_dd': max_dd,
            'sharpe': sharpe,
            'trades': trades,
            'fees': total_fees,
            'beats_spy': bool(annual > spy_annual),
            'beats_qqq': bool(annual > qqq_annual),
        }

        print(f"{'✅' if annual > spy_annual else '❌'} {cfg['desc']:<25} 年化:{annual:.2%} 回撤:{max_dd:.1%} 夏普:{sharpe:.2f} 交易:{trades} 费:${total_fees:,.0f}")

    # 汇总
    print(f"\n{'='*60}")
    print(f"📊 策略对比 (SPY={spy_annual:.2%} QQQ={qqq_annual:.2%})")
    print(f"{'='*60}")
    print(f"{'策略':<28} {'年化':>8} {'回撤':>8} {'夏普':>6} {'vs SPY':>8} {'vs QQQ':>8}")
    print("-" * 68)

    best = None
    for name, r in sorted(results.items(), key=lambda x: -x[1]['annual']):
        m1 = "✅" if r['beats_spy'] else "❌"
        print(f"{m1} {r['desc']:<26} {r['annual']:>7.2%} {r['max_dd']:>7.1%} {r['sharpe']:>6.2f} {r['annual']-spy_annual:>+7.2%} {r['annual']-qqq_annual:>+7.2%}")
        if best is None and r['beats_spy']:
            best = (name, r)

    if best:
        print(f"\n🏆 最佳: {best[1]['desc']} → {best[1]['annual']:.2%}")

    # 保存
    output = {
        'timestamp': pd.Timestamp.now().isoformat(),
        'period': f"{start_date} → {end_date}",
        'years': round(years, 1),
        'spy_annual': round(spy_annual * 100, 2),
        'qqq_annual': round(qqq_annual * 100, 2),
        'strategies': {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in results.items()},
        'recommendation': best[0] if best else 'QQQ_100',
    }
    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"💾 保存: {RESULT_FILE}")

    return results


if __name__ == '__main__':
    run_v4()
