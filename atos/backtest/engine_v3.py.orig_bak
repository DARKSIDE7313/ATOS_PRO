#!/usr/bin/env python3
"""
ATOS Backtest v3 — Pure QQQ Trend Strategy
==========================================
最简策略: 只持有 QQQ，用趋势过滤控制回撤。

规则:
- SPY > MA50 且 QQQ > MA20: 100% QQQ
- SPY < MA50: 减仓到 50% QQQ
- SPY < MA200: 清仓
- QQQ 移动止损 10%

对比: SPY buy-hold, QQQ buy-hold
"""

import json
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULT_FILE = os.path.join(BASE, "data", "backtest_v3_result.json")


def run_v3(start_date='2016-01-01', end_date='2026-08-01', capital=300000):
    import yfinance as yf

    print("=" * 60)
    print("📊 回测 v3 — Pure QQQ Trend")
    print("=" * 60)

    spy = yf.Ticker('SPY').history(start=start_date, end=end_date)
    qqq = yf.Ticker('QQQ').history(start=start_date, end=end_date)
    tlt = yf.Ticker('TLT').history(start=start_date, end=end_date)

    spy_ma50 = spy['Close'].rolling(50).mean()
    spy_ma200 = spy['Close'].rolling(200).mean()
    qqq_ma20 = qqq['Close'].rolling(20).mean()

    years = len(spy) / 252
    spy_ret = (spy['Close'].iloc[-1] / spy['Close'].iloc[0]) - 1
    spy_annual = (1 + spy_ret) ** (1 / years) - 1
    qqq_ret = (qqq['Close'].iloc[-1] / qqq['Close'].iloc[0]) - 1
    qqq_annual = (1 + qqq_ret) ** (1 / years) - 1

    print(f"基准: SPY={spy_annual:.2%} QQQ={qqq_annual:.2%}")

    # 模拟
    cash = capital
    shares = 0
    avg_price = 0
    peak_price = 0
    total_fees = 0
    trades = []
    equity_curve = []

    # 策略变体
    strategies = {
        'QQQ_buy_hold': {'desc': 'QQQ 买入持有', 'trend_filter': False, 'trailing_stop': 0},
        'QQQ_MA50': {'desc': 'QQQ + SPY>MA50', 'trend_filter': 'ma50', 'trailing_stop': 0},
        'QQQ_MA50_TS10': {'desc': 'QQQ + MA50 + 移动止损10%', 'trend_filter': 'ma50', 'trailing_stop': 0.10},
        'QQQ_MA50_MA200': {'desc': 'QQQ + MA50/MA200双层', 'trend_filter': 'dual', 'trailing_stop': 0.10},
        'QQQ_MA50_TS10_TLT': {'desc': 'QQQ + MA50 + TS10% + BEAR换TLT', 'trend_filter': 'ma50', 'trailing_stop': 0.10, 'bear_asset': 'TLT'},
    }

    results = {}

    for strat_name, cfg in strategies.items():
        cash = capital
        shares = 0
        avg_price = 0
        peak_price = 0
        total_fees = 0
        trades = []
        equity_curve = []
        in_position = False
        bear_shares = 0  # BEAR 替代资产

        for i, date in enumerate(spy.index):
            if i < 200:
                continue
            if date not in qqq.index:
                continue

            q_price = qqq.loc[date, 'Close']
            s_price = spy.loc[date, 'Close']
            s_ma50 = spy_ma50.iloc[i] if not pd.isna(spy_ma50.iloc[i]) else s_price
            s_ma200 = spy_ma200.iloc[i] if not pd.isna(spy_ma200.iloc[i]) else s_price

            # 趋势判断
            if cfg['trend_filter'] == 'dual':
                bull = s_price > s_ma50 and s_price > s_ma200
            elif cfg['trend_filter'] == 'ma50':
                bull = s_price > s_ma50
            else:
                bull = True  # 不过滤

            # 移动止损检查
            if in_position and shares > 0:
                if q_price > peak_price:
                    peak_price = q_price
                if cfg.get('trailing_stop', 0) > 0:
                    ts_drop = (peak_price - q_price) / peak_price
                    if ts_drop >= cfg['trailing_stop']:
                        # 止损卖出
                        fee = futu_sell_fee(shares, q_price)
                        total_fees += fee
                        cash += shares * q_price - fee
                        pnl = (q_price - avg_price) / avg_price
                        trades.append({'date': str(date.date()), 'action': 'SELL', 'price': round(q_price, 2), 'reason': f'TS{ts_drop:.1%}', 'pnl': round(pnl * 100, 2)})
                        shares = 0
                        in_position = False
                        peak_price = 0

            # 趋势过滤
            if not bull and in_position and shares > 0:
                # 卖出 QQQ
                fee = futu_sell_fee(shares, q_price)
                total_fees += fee
                cash += shares * q_price - fee
                pnl = (q_price - avg_price) / avg_price
                trades.append({'date': str(date.date()), 'action': 'SELL', 'price': round(q_price, 2), 'reason': 'BEAR', 'pnl': round(pnl * 100, 2)})
                shares = 0
                in_position = False
                peak_price = 0

                # BEAR 替代资产
                if cfg.get('bear_asset') and cfg['bear_asset'] in ('TLT',) and date in tlt.index:
                    t_price = tlt.loc[date, 'Close']
                    bear_shares = max(1, int(cash * 0.95 / t_price))
                    fee = futu_buy_fee(bear_shares, t_price)
                    total_fees += fee
                    cash -= bear_shares * t_price + fee

            elif not bull and bear_shares > 0:
                pass  # 持有 TLT

            if bull and bear_shares > 0:
                # 卖 TLT 换 QQQ
                if date in tlt.index:
                    t_price = tlt.loc[date, 'Close']
                    fee = futu_sell_fee(bear_shares, t_price)
                    total_fees += fee
                    cash += bear_shares * t_price - fee
                    bear_shares = 0

            if bull and not in_position and shares == 0 and bear_shares == 0:
                # 买入 QQQ
                shares = max(1, int(cash * 0.95 / q_price))
                fee = futu_buy_fee(shares, q_price)
                total_fees += fee
                cost = shares * q_price + fee
                if cost <= cash:
                    cash -= cost
                    avg_price = q_price
                    peak_price = q_price
                    in_position = True
                    trades.append({'date': str(date.date()), 'action': 'BUY', 'price': round(q_price, 2), 'shares': shares})
                else:
                    shares = 0

            # 估值
            val = cash
            if shares > 0:
                val += shares * q_price
            if bear_shares > 0 and date in tlt.index:
                val += bear_shares * tlt.loc[date, 'Close']
            equity_curve.append((date, val))

        # 最终估值
        final = cash
        if shares > 0:
            final += shares * qqq['Close'].iloc[-1]
        if bear_shares > 0:
            final += bear_shares * tlt['Close'].iloc[-1]

        total_ret = (final / capital) - 1
        annual = (1 + total_ret) ** (1 / years) - 1

        # 回撤
        peak_val = capital
        max_dd = 0
        for _, v in equity_curve:
            if v > peak_val:
                peak_val = v
            dd = (peak_val - v) / peak_val
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
            'trades': len(trades),
            'fees': total_fees,
            'beats_spy': bool(annual > spy_annual),
            'beats_qqq': bool(annual > qqq_annual),
        }

        print(f"\n{'✅' if annual > spy_annual else '❌'} {cfg['desc']}")
        print(f"   年化: {annual:.2%} | 回撤: {max_dd:.1%} | 夏普: {sharpe:.2f} | 交易: {len(trades)}笔 | 费用: ${total_fees:,.0f}")

    # 汇总
    print("\n" + "=" * 60)
    print("📊 策略对比汇总")
    print("=" * 60)
    print(f"{'策略':<30} {'年化':>8} {'回撤':>8} {'夏普':>6} {'vs SPY':>8}")
    print("-" * 60)
    print(f"{'SPY buy-hold':<30} {spy_annual:>7.2%} {'—':>8} {'—':>6} {'—':>8}")
    print(f"{'QQQ buy-hold':<30} {qqq_annual:>7.2%} {'—':>8} {'—':>6} {'—':>8}")

    best = None
    for name, r in sorted(results.items(), key=lambda x: -x[1]['annual']):
        marker = "✅" if r['beats_spy'] else "❌"
        print(f"{marker} {r['desc']:<28} {r['annual']:>7.2%} {r['max_dd']:>7.1%} {r['sharpe']:>6.2f} {r['annual']-spy_annual:>+7.2%}")
        if best is None and r['beats_spy']:
            best = (name, r)

    if best:
        print(f"\n🏆 最佳策略: {best[1]['desc']} → {best[1]['annual']:.2%} (SPY +{best[1]['annual']-spy_annual:.2%})")
    else:
        print(f"\n⚠️ 没有策略跑赢 SPY。QQQ buy-hold ({qqq_annual:.2%}) 是最佳简单选择。")

    # 保存
    output = {
        'timestamp': pd.Timestamp.now().isoformat(),
        'period': f"{start_date} → {end_date}",
        'years': round(years, 1),
        'spy_annual': round(spy_annual * 100, 2),
        'qqq_annual': round(qqq_annual * 100, 2),
        'strategies': {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in results.items()},
    }
    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n💾 保存: {RESULT_FILE}")

    return results


if __name__ == '__main__':
    run_v3()
