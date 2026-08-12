#!/usr/bin/env python3
"""
ATOS Backtest v5 — Strategy Optimization
=========================================
测试多种策略变体，找最优组合:
A) v28 原版: 60% QQQ + 40% 动量(1日涨跌)
B) 相对强度: 60% QQQ + 40% 3月相对强度
C) 波动率加权: 60% QQQ + 40% 动量(逆波动率加权)
D) 双动量: QQQ/TLT 切换 + 动量股
E) 行业动量: 60% QQQ + 40% 最强行业股
"""
import yfinance as yf
import pandas as pd
import numpy as np
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

ALPHA_POOL = ['NVDA','AAPL','MSFT','GOOGL','META','AMZN','AVGO','AMD','CRM','NFLX','PLTR','MU','TSLA']
ETFS = ['QQQ','SPY','TLT','GLD']

def fetch(symbols, start='2016-01-01', end='2026-08-01'):
    data = {}
    for s in symbols:
        try:
            df = yf.download(s, start=start, end=end, progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                data[s] = df
        except:
            pass
    return data

def calc_indicators(df):
    df = df.copy()
    df['mom_63'] = df['Close'].pct_change(63)  # 3月动量
    df['mom_21'] = df['Close'].pct_change(21)  # 1月动量
    df['rsi'] = 100 - 100/(1 + df['Close'].diff().clip(lower=0).rolling(14).mean() / df['Close'].diff().clip(upper=0).abs().rolling(14).mean())
    df['ma50'] = df['Close'].rolling(50).mean()
    df['ma200'] = df['Close'].rolling(200).mean()
    df['vol20'] = df['Close'].pct_change().rolling(20).std() * np.sqrt(252)
    return df

def run_strategy(data, dates, name, core_pct=0.60, n_stocks=5, use_rs=False, vol_weight=False, dual_mom=False, sector_mom=False):
    cash = 300000.0
    positions = {}  # sym -> (qty, avg_price)
    last_rebal = None
    total_fees = 0
    trades = 0
    equity_curve = []
    
    for i, date in enumerate(dates):
        port_val = cash + sum(q * data[s].loc[date, 'Close'] for s, (q, _) in positions.items() if s in data and date in data[s].index)
        equity_curve.append((date, port_val))
        
        if i < 200 or i % 63 != 0:  # 季度再平衡
            continue
        
        spy = data.get('SPY')
        if spy is None or date not in spy.index:
            continue
        spy_row = spy.loc[date]
        
        # 双动量: SPY < MA200 → TLT, 否则 QQQ
        if dual_mom:
            core_sym = 'QQQ' if spy_row['Close'] > spy_row['ma50'] else 'TLT'
        else:
            core_sym = 'QQQ'
        
        # 卖出所有非目标持仓
        target_core_val = port_val * core_pct
        target_alpha_val = port_val * (1 - core_pct)
        
        # 计算 alpha 候选
        candidates = []
        for sym in ALPHA_POOL:
            if sym not in data or date not in data[sym].index:
                continue
            row = data[sym].loc[date]
            price = row['Close']
            if price <= 0 or row.get('rsi', 50) > 78:
                continue
            if row.get('ma50', 0) > 0 and price < row['ma50'] * 0.92:
                continue
            
            if use_rs:
                # 相对强度: 3月动量 - SPY 3月动量
                rs = row.get('mom_63', 0) - spy_row.get('mom_63', 0)
                if rs <= 0:
                    continue
                score = rs
            elif sector_mom:
                # 行业动量: 1月+3月综合
                score = row.get('mom_21', 0) * 0.4 + row.get('mom_63', 0) * 0.6
            else:
                score = row.get('mom_63', 0)
            
            vol = row.get('vol20', 0.3)
            weight = 1.0 / max(vol, 0.1) if vol_weight else 1.0
            candidates.append((sym, score, price, weight))
        
        candidates.sort(key=lambda x: -x[1])
        top = candidates[:n_stocks]
        
        # 目标持仓
        targets = {core_sym: target_core_val}
        if top:
            total_w = sum(c[3] for c in top)
            for sym, score, price, w in top:
                targets[sym] = target_alpha_val * (w / total_w)
        
        # 卖出不在目标的
        for sym in list(positions.keys()):
            if sym not in targets:
                qty, avg = positions[sym]
                price = data[sym].loc[date, 'Close']
                fee = futu_sell_fee(qty, price)
                cash += qty * price - fee
                total_fees += fee
                trades += 1
                del positions[sym]
        
        # 调整目标持仓
        for sym, target_val in targets.items():
            if sym not in data or date not in data[sym].index:
                continue
            price = data[sym].loc[date, 'Close']
            current_qty = positions.get(sym, (0, 0))[0]
            current_val = current_qty * price
            diff = target_val - current_val
            
            if abs(diff) < 1000:  # 太小不交易
                continue
            
            if diff > 0:  # 买入
                qty = int(diff / price)
                if qty <= 0:
                    continue
                fee = futu_buy_fee(qty, price)
                cost = qty * price + fee
                if cost > cash * 0.98:
                    qty = int(cash * 0.98 / price)
                    if qty <= 0:
                        continue
                    fee = futu_buy_fee(qty, price)
                    cost = qty * price + fee
                cash -= cost
                total_fees += fee
                old_qty, old_avg = positions.get(sym, (0, 0))
                new_qty = old_qty + qty
                new_avg = (old_qty * old_avg + qty * price) / new_qty if new_qty > 0 else price
                positions[sym] = (new_qty, new_avg)
                trades += 1
            else:  # 卖出部分
                sell_qty = min(current_qty, int(-diff / price))
                if sell_qty <= 0:
                    continue
                fee = futu_sell_fee(sell_qty, price)
                cash += sell_qty * price - fee
                total_fees += fee
                new_qty = current_qty - sell_qty
                if new_qty > 0:
                    positions[sym] = (new_qty, positions[sym][1])
                else:
                    del positions[sym]
                trades += 1
        
        last_rebal = date
    
    # 最终结算
    final_val = cash + sum(q * data[s].iloc[-1]['Close'] for s, (q, _) in positions.items() if s in data)
    years = len(dates) / 252
    total_ret = (final_val / 300000 - 1) * 100
    annual_ret = ((final_val / 300000) ** (1/years) - 1) * 100 if years > 0 else 0
    
    # 回撤
    vals = pd.Series([v for _, v in equity_curve])
    peak = vals.expanding().max()
    dd = (vals - peak) / peak
    max_dd = dd.min() * 100
    
    # 夏普
    rets = vals.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    
    return {
        'name': name, 'annual': round(annual_ret, 2), 'total': round(total_ret, 1),
        'max_dd': round(max_dd, 1), 'sharpe': round(sharpe, 2),
        'trades': trades, 'fees': round(total_fees),
        'fee_pct': round(total_fees / 300000 / years * 100, 2),
        'final': round(final_val),
    }

def main():
    print("📥 下载数据...")
    all_syms = list(set(ALPHA_POOL + ETFS))
    data = fetch(all_syms)
    print(f"✅ {len(data)} 只标的")
    
    # 计算指标
    for sym in data:
        data[sym] = calc_indicators(data[sym])
    
    dates = data['SPY'].index[200:]  # 跳过前200天(指标预热)
    
    strategies = [
        ('A: v28 原版 (60%QQQ+动量)', dict()),
        ('B: 相对强度 (RS>SPY)', dict(use_rs=True)),
        ('C: 波动率加权', dict(vol_weight=True)),
        ('D: 双动量 (QQQ/TLT切换)', dict(dual_mom=True)),
        ('E: 行业动量 (1月+3月)', dict(sector_mom=True)),
        ('F: RS+波动率加权', dict(use_rs=True, vol_weight=True)),
        ('G: 双动量+RS+波动率', dict(dual_mom=True, use_rs=True, vol_weight=True)),
        ('H: 70%QQQ+3只RS', dict(core_pct=0.70, n_stocks=3, use_rs=True)),
        ('I: 80%QQQ+3只RS+波动率', dict(core_pct=0.80, n_stocks=3, use_rs=True, vol_weight=True)),
    ]
    
    results = []
    for name, kwargs in strategies:
        print(f"  回测: {name}...")
        r = run_strategy(data, dates, name, **kwargs)
        results.append(r)
        print(f"    年化={r['annual']}% 回撤={r['max_dd']}% 夏普={r['sharpe']} 费用={r['fee_pct']}%/年")
    
    # 打印排行
    results.sort(key=lambda x: -x['annual'])
    print(f"\n{'═'*80}")
    print(f"  📊 策略排行 (按年化回报)")
    print(f"{'═'*80}")
    print(f"{'策略':>30} {'年化':>8} {'回撤':>7} {'夏普':>6} {'费用/年':>8} {'交易':>6}")
    print(f"{'─'*80}")
    for r in results:
        print(f"{r['name']:>30} {r['annual']:>7.2f}% {r['max_dd']:>6.1f}% {r['sharpe']:>6.2f} {r['fee_pct']:>7.2f}% {r['trades']:>6}")
    
    # 保存
    out = {'timestamp': str(pd.Timestamp.now()), 'results': results}
    with open(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backtest_v5_result.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n✅ 结果已保存到 data/backtest_v5_result.json")

if __name__ == '__main__':
    main()
