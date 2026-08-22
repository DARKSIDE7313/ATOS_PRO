#!/usr/bin/env python3
"""
ATOS Backtest Engine v2 — Trend Momentum Strategy
==================================================
策略核心: QQQ 核心仓 + 动量个股 alpha

1. 60% QQQ/SPY 核心仓 (趋势跟随)
   - SPY > MA50: 持有 QQQ
   - SPY < MA50: 换成 TLT/现金
   - 移动止损保护

2. 40% 动量个股 alpha 仓
   - 每 2 周从股票池选 3-5 只最强动量股
   - 持有 2-4 周，让利润跑
   - 严格止损

3. 风控: 5 层安全系统
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.fee_model import futu_buy_fee, futu_sell_fee

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULT_FILE = os.path.join(BASE, "data", "backtest_v2_result.json")

# ── 动量股票池 ──
MOMENTUM_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD",
    "AVGO", "CRM", "ADBE", "NFLX", "PLTR", "MU",
    "JPM", "GS", "MS", "V", "MA", "BLK", "COIN",
    "UNH", "ABBV", "TMO",
    "COST", "HD", "NKE", "MCD",
    "CAT", "GE", "HON", "XOM",
]

CORE_ETFS = ["QQQ", "SPY"]


def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def run_backtest_v2(start_date='2018-01-01', end_date='2026-08-01', initial_capital=300000):
    import yfinance as yf

    print("=" * 60)
    print("📊 ATOS 回测 v2 — Trend Momentum Strategy")
    print(f"   期间: {start_date} → {end_date}")
    print(f"   策略: 60% QQQ核心 + 40% 动量个股")
    print(f"   初始: ${initial_capital:,.0f}")
    print("=" * 60)

    # 下载数据
    print("\n📥 下载数据...")
    all_syms = list(set(MOMENTUM_UNIVERSE + CORE_ETFS + ['TLT', 'GLD', '^VIX']))
    data = {}
    for sym in all_syms:
        try:
            hist = yf.Ticker(sym).history(start=start_date, end=end_date)
            if len(hist) > 100:
                data[sym] = hist
        except Exception:
            pass
    print(f"   成功: {len(data)}/{len(all_syms)}")

    spy = data.get('SPY')
    qqq = data.get('QQQ')
    if spy is None or qqq is None:
        print("❌ 缺少 SPY/QQQ 数据")
        return None

    # 预处理指标
    print("🔧 计算指标...")
    spy_ma50 = spy['Close'].rolling(50).mean()
    qqq_ma20 = qqq['Close'].rolling(20).mean()
    qqq_ma50 = qqq['Close'].rolling(50).mean()
    qqq_rsi = compute_rsi(qqq['Close'])

    # 个股动量 (3个月收益率)
    mom_data = {}
    for sym in MOMENTUM_UNIVERSE:
        if sym in data:
            df = data[sym]
            df = df.copy()
            df['mom_3m'] = df['Close'].pct_change(63)  # 3个月
            df['mom_1m'] = df['Close'].pct_change(21)  # 1个月
            df['rsi'] = compute_rsi(df['Close'])
            df['ma50'] = df['Close'].rolling(50).mean()
            df['ma20'] = df['Close'].rolling(20).mean()
            df['volume_ratio'] = df['Volume'] / df['Volume'].rolling(20).mean()
            # 综合动量分
            df['mom_score'] = df['mom_3m'] * 0.5 + df['mom_1m'] * 0.3 + (df['rsi'] / 100) * 0.2
            mom_data[sym] = df

    # 基准
    spy_ret = (spy['Close'].iloc[-1] / spy['Close'].iloc[0]) - 1
    years = len(spy) / 252
    spy_annual = (1 + spy_ret) ** (1 / years) - 1
    qqq_ret = (qqq['Close'].iloc[-1] / qqq['Close'].iloc[0]) - 1
    qqq_annual = (1 + qqq_ret) ** (1 / years) - 1
    print(f"\n📈 基准: SPY={spy_annual:.2%} | QQQ={qqq_annual:.2%}")

    # ── 模拟交易 ──
    print("\n📈 模拟交易...")
    cash = initial_capital
    positions = {}  # {sym: {shares, avg_price, buy_date, peak_price, type}}
    trades = []
    equity_curve = []
    total_fees = 0.0

    trading_days = spy.index
    last_rebalance = 0
    core_etf = 'QQQ'  # 默认核心持仓

    for i, date in enumerate(trading_days):
        if i < 63:  # 需要 3 个月数据
            continue

        # 估值
        port_value = cash
        for sym, pos in positions.items():
            if sym in data and date in data[sym].index:
                port_value += pos['shares'] * data[sym].loc[date, 'Close']

        spy_price = spy['Close'].iloc[:i+1].iloc[-1]
        spy_ma = spy_ma50.iloc[i] if i < len(spy_ma50) else spy_price
        bull = spy_price > spy_ma
        qqq_price = qqq['Close'].iloc[:i+1].iloc[-1] if i < len(qqq) else 0

        # ── 核心仓管理 (每 10 天再平衡) ──
        if i - last_rebalance >= 10 and i > 63:
            last_rebalance = i

            target_core_pct = 0.60 if bull else 0.0
            target_core_value = port_value * target_core_pct

            # 当前核心仓价值
            core_value = 0
            for etf in CORE_ETFS + ['TLT']:
                if etf in positions:
                    price = data[etf].loc[date, 'Close'] if date in data[etf].index else 0
                    core_value += positions[etf]['shares'] * price

            if bull:
                # 应该持有 QQQ
                if core_etf not in positions and target_core_value > 1000:
                    # 买入 QQQ
                    price = qqq_price
                    shares = max(1, int(target_core_value / price))
                    fee = futu_buy_fee(shares, price)
                    cost = shares * price + fee
                    if cost <= cash:
                        cash -= cost
                        total_fees += fee
                        positions[core_etf] = {
                            'shares': shares, 'avg_price': price,
                            'buy_date': date, 'peak_price': price, 'type': 'core',
                        }
                        trades.append({
                            'date': str(date.date()), 'sym': core_etf, 'action': 'BUY',
                            'price': round(price, 2), 'shares': shares, 'reason': '核心仓(BULL)',
                        })
            else:
                # BEAR: 卖核心仓
                for etf in list(positions.keys()):
                    if positions[etf].get('type') == 'core':
                        price = data[etf].loc[date, 'Close'] if date in data[etf].index else positions[etf]['avg_price']
                        fee = futu_sell_fee(positions[etf]['shares'], price)
                        total_fees += fee
                        pnl_pct = (price - positions[etf]['avg_price']) / positions[etf]['avg_price']
                        cash += positions[etf]['shares'] * price - fee
                        trades.append({
                            'date': str(date.date()), 'sym': etf, 'action': 'SELL',
                            'price': round(price, 2), 'shares': positions[etf]['shares'],
                            'reason': f'核心仓(BEAR) PnL={pnl_pct:.1%}',
                            'pnl_pct': round(pnl_pct * 100, 2),
                        })
                        del positions[etf]

        # ── 个股动量仓管理 ──
        # 卖出检查
        for sym in list(positions.keys()):
            if positions[sym].get('type') == 'core':
                continue  # 核心仓已处理
            if sym not in mom_data or date not in mom_data[sym].index:
                continue

            row = mom_data[sym].loc[date]
            pos = positions[sym]
            price = row['Close']
            pnl_pct = (price - pos['avg_price']) / pos['avg_price']

            if price > pos.get('peak_price', pos['avg_price']):
                pos['peak_price'] = price

            sell_reason = None

            # 止损 5%
            if pnl_pct <= -0.05:
                sell_reason = f"止损{pnl_pct:.1%}"
            # 移动止损 8%（从峰值）
            elif pos['peak_price'] > pos['avg_price'] * 1.05:
                trailing = (price - pos['peak_price']) / pos['peak_price']
                if trailing <= -0.08:
                    sell_reason = f"移动止损{trailing:.1%}"
            # 动量消失（3月动量转负）
            elif row['mom_3m'] < -0.05 and pnl_pct > 0:
                sell_reason = f"动量消失 mom={row['mom_3m']:.1%}"
            # 持有超 40 天 + 有利润
            elif (date - pos['buy_date']).days > 40 and pnl_pct > 0.03:
                sell_reason = f"超时{40}d PnL={pnl_pct:.1%}"
            # BEAR 市强制卖
            elif not bull and pnl_pct < 0.02:
                sell_reason = f"BEAR退出 PnL={pnl_pct:.1%}"

            if sell_reason:
                fee = futu_sell_fee(pos['shares'], price)
                total_fees += fee
                cash += pos['shares'] * price - fee
                trades.append({
                    'date': str(date.date()), 'sym': sym, 'action': 'SELL',
                    'price': round(price, 2), 'shares': pos['shares'],
                    'reason': sell_reason, 'pnl_pct': round(pnl_pct * 100, 2),
                })
                del positions[sym]

        # 买入检查（每 10 天）
        if i % 10 != 0 or not bull:
            equity_curve.append((date, port_value))
            continue

        alpha_positions = sum(1 for p in positions.values() if p.get('type') != 'core')
        max_alpha = 5
        if alpha_positions >= max_alpha:
            equity_curve.append((date, port_value))
            continue

        # 排名动量股
        candidates = []
        for sym in MOMENTUM_UNIVERSE:
            if sym in positions or sym not in mom_data or date not in mom_data[sym].index:
                continue
            row = mom_data[sym].loc[date]
            if pd.isna(row['mom_score']) or pd.isna(row['rsi']):
                continue

            # 过滤
            if row['rsi'] > 75:  # 超买
                continue
            if row['Close'] < row['ma50']:  # 必须在 MA50 上方
                continue
            if row['mom_3m'] < 0.02:  # 3月动量必须 > 2%
                continue

            score = row['mom_score']
            # 均线多头加分
            if row['Close'] > row['ma20'] > row['ma50']:
                score += 0.05

            candidates.append((sym, score, row['Close']))

        candidates.sort(key=lambda x: -x[1])
        n_buys = min(2, max_alpha - alpha_positions)
        alpha_budget = port_value * 0.40  # 40% alpha 仓

        for sym, score, price in candidates[:n_buys]:
            pos_value = alpha_budget / max_alpha
            shares = max(1, int(pos_value / price))
            fee = futu_buy_fee(shares, price)
            cost = shares * price + fee

            if cost > cash * 0.90:
                shares = max(1, int(cash * 0.85 / price))
                fee = futu_buy_fee(shares, price)
                cost = shares * price + fee
                if shares <= 0:
                    continue

            cash -= cost
            total_fees += fee
            positions[sym] = {
                'shares': shares, 'avg_price': price,
                'buy_date': date, 'peak_price': price, 'type': 'alpha',
            }
            trades.append({
                'date': str(date.date()), 'sym': sym, 'action': 'BUY',
                'price': round(price, 2), 'shares': shares,
                'reason': f'动量={score:.3f}',
            })

        equity_curve.append((date, port_value))

    # ── 结果 ──
    print("\n📊 计算结果...")
    final_value = cash
    for sym, pos in positions.items():
        if sym in data:
            final_value += pos['shares'] * data[sym]['Close'].iloc[-1]

    total_ret = (final_value / initial_capital) - 1
    annual_ret = (1 + total_ret) ** (1 / years) - 1

    # 夏普
    if len(equity_curve) > 1:
        eq_vals = [e[1] for e in equity_curve]
        rets = pd.Series(eq_vals).pct_change().dropna()
        sharpe = rets.mean() / rets.std() * np.sqrt(252 / 5) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # 最大回撤
    peak = initial_capital
    max_dd = 0
    for _, val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak
        if dd > max_dd:
            max_dd = dd

    sell_trades = [t for t in trades if t['action'] == 'SELL' and 'pnl_pct' in t]
    wins = [t for t in sell_trades if t.get('pnl_pct', 0) > 0]
    win_rate = len(wins) / len(sell_trades) if sell_trades else 0

    avg_win = np.mean([t['pnl_pct'] for t in wins]) if wins else 0
    losses = [t for t in sell_trades if t.get('pnl_pct', 0) <= 0]
    avg_loss = np.mean([abs(t['pnl_pct']) for t in losses]) if losses else 1

    beats_spy = bool(annual_ret > spy_annual)
    beats_qqq = bool(annual_ret > qqq_annual)

    result = {
        'timestamp': datetime.now().isoformat(),
        'strategy': 'Trend Momentum v2 (60% QQQ core + 40% alpha)',
        'period': f"{start_date} → {end_date}",
        'years': round(years, 1),
        'initial': initial_capital,
        'final': round(final_value, 2),
        'total_return': round(total_ret * 100, 2),
        'annual_return': round(annual_ret * 100, 2),
        'spy_annual': round(spy_annual * 100, 2),
        'qqq_annual': round(qqq_annual * 100, 2),
        'beats_spy': beats_spy,
        'beats_qqq': beats_qqq,
        'excess_vs_spy': round((annual_ret - spy_annual) * 100, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd * 100, 2),
        'total_trades': len(trades),
        'win_rate': round(win_rate * 100, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'total_fees': round(total_fees, 2),
        'fee_drag_pct': round(total_fees / initial_capital * 100, 3),
    }

    print("\n" + "=" * 60)
    print("📊 回测 v2 结果")
    print("=" * 60)
    print(f"  期间: {result['period']} ({years:.1f}年)")
    print(f"  初始: ${initial_capital:,.0f} → 最终: ${final_value:,.0f}")
    print(f"  总回报: {total_ret:.1%} | 年化: {annual_ret:.2%}")
    print(f"  SPY: {spy_annual:.2%} | QQQ: {qqq_annual:.2%}")
    print(f"  超额(vs SPY): {(annual_ret - spy_annual):+.2%}")
    print(f"  {'✅ 跑赢 SPY!' if beats_spy else '❌ 未跑赢 SPY'}")
    print(f"  {'✅ 跑赢 QQQ!' if beats_qqq else '❌ 未跑赢 QQQ'}")
    print(f"  夏普: {sharpe:.2f} | 回撤: {max_dd:.1%}")
    print(f"  交易: {len(trades)}笔 ({len(sell_trades)}卖出)")
    print(f"  胜率: {win_rate:.1%} | 均赢:{avg_win:.1f}% 均亏:{avg_loss:.1f}%")
    print(f"  费用: ${total_fees:,.2f} ({total_fees/initial_capital*100:.3f}%)")

    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"\n💾 保存: {RESULT_FILE}")

    # 保存交易记录
    trades_file = os.path.join(BASE, "data", "backtest_v2_trades.json")
    with open(trades_file, 'w') as f:
        json.dump(trades[-100:], f, indent=2)

    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2018-01-01')
    parser.add_argument('--end', default='2026-08-01')
    parser.add_argument('--capital', type=int, default=300000)
    args = parser.parse_args()

    result = run_backtest_v2(args.start, args.end, args.capital)
    sys.exit(0 if result and result['beats_spy'] else 1)
