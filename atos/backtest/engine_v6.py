#!/usr/bin/env python3
"""ATOS Backtest v6 — Unified Strategy Comparison"""
import yfinance as yf
import pandas as pd
import numpy as np
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

POOL = ['NVDA','AAPL','MSFT','GOOGL','META','AMZN','AVGO','AMD','CRM','NFLX','PLTR','MU','TSLA']
ALL = ['QQQ','SPY'] + POOL

data = {}
for sym in ALL:
    df = yf.download(sym, start='2016-01-01', end='2026-08-01', progress=False)
    if not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        data[sym] = df
print(f"Downloaded {len(data)} symbols")

for sym in data:
    df = data[sym]
    df['mom_21'] = df['Close'].pct_change(21)
    df['rsi'] = 100 - 100/(1 + df['Close'].diff().clip(lower=0).rolling(14).mean() / df['Close'].diff().clip(upper=0).abs().rolling(14).mean())
    df['ma50'] = df['Close'].rolling(50).mean()
    df['dist_high'] = (df['Close'] / df['Close'].rolling(20).max() - 1) * 100

def run(core_pct, alpha_pct, cash_pct, n_stocks, label):
    cash = 300000.0
    positions = {}
    total_fees = 0
    trades = 0
    curve = []
    dates = data['SPY'].index[200:]

    for i, date in enumerate(dates):
        pv = cash + sum(q * data[s].loc[date,'Close'] for s,(q,_) in positions.items() if s in data and date in data[s].index)
        curve.append((date, pv))
        if i < 200 or i % 63 != 0:
            continue

        # Alpha candidates
        cands = []
        for sym in POOL:
            if sym not in data or date not in data[sym].index: continue
            row = data[sym].loc[date]
            p = row['Close']
            if p <= 0 or row.get('rsi',50) > 78: continue
            if row.get('ma50',0) > 0 and p < row['ma50']*0.92: continue
            trend = max(0, 1 + row.get('dist_high',-10)/20)
            mom = max(0, min(1, (row.get('mom_21',0)*100+5)/10))
            cands.append((sym, mom*0.4+trend*0.6, p))
        cands.sort(key=lambda x:-x[1])
        top = cands[:n_stocks]

        targets = {'QQQ': pv * core_pct}
        per = pv * alpha_pct / len(top) if top else 0
        for sym,sc,p in top: targets[sym] = per

        # Sell non-target
        for sym in list(positions.keys()):
            if sym not in targets:
                qty,avg = positions[sym]
                p = data[sym].loc[date,'Close']
                fee = futu_sell_fee(qty,p)
                cash += qty*p - fee
                total_fees += fee; trades += 1
                del positions[sym]

        # Adjust
        for sym, tv in targets.items():
            if sym not in data or date not in data[sym].index: continue
            p = data[sym].loc[date,'Close']
            cq = positions.get(sym,(0,0))[0]
            diff = tv - cq*p
            if abs(diff) < 1000: continue
            if diff > 0:
                qty = int(diff/p)
                if qty <= 0: continue
                fee = futu_buy_fee(qty,p)
                cost = qty*p+fee
                max_sp = cash - pv*cash_pct
                if cost > max_sp:
                    qty = int(max_sp/p)
                    if qty <= 0: continue
                    fee = futu_buy_fee(qty,p)
                    cost = qty*p+fee
                cash -= cost; total_fees += fee; trades += 1
                oq,oa = positions.get(sym,(0,0))
                nq = oq+qty
                positions[sym] = (nq, (oq*oa+qty*p)/nq if nq>0 else p)
            else:
                sq = min(cq, int(-diff/p))
                if sq <= 0: continue
                fee = futu_sell_fee(sq,p)
                cash += sq*p-fee; total_fees += fee; trades += 1
                nq = cq-sq
                if nq > 0: positions[sym] = (nq, positions[sym][1])
                else: del positions[sym]

    fv = cash + sum(q*data[s].iloc[-1]['Close'] for s,(q,_) in positions.items() if s in data)
    yrs = len(dates)/252
    ar = ((fv/300000)**(1/yrs)-1)*100
    vals = pd.Series([v for _,v in curve])
    mdd = ((vals-vals.expanding().max())/vals.expanding().max()).min()*100
    rets = vals.pct_change().dropna()
    sr = rets.mean()/rets.std()*np.sqrt(252) if rets.std()>0 else 0
    return {'label':label,'annual':round(ar,2),'max_dd':round(mdd,1),'sharpe':round(sr,2),
            'trades':trades,'fees':round(total_fees),'fee_yr':round(total_fees/300000/yrs*100,2),'final':round(fv)}

configs = [
    (0.60, 0.40, 0.00, 5, 'A: v28i (60/40/0)'),
    (0.60, 0.30, 0.10, 5, 'B: v29 unified (60/30/10)'),
    (0.65, 0.25, 0.10, 5, 'C: defensive (65/25/10)'),
    (0.55, 0.35, 0.10, 5, 'D: aggressive (55/35/10)'),
    (0.60, 0.30, 0.10, 7, 'E: diversified 7 (60/30/10)'),
    (0.50, 0.40, 0.10, 5, 'F: balanced (50/40/10)'),
    (0.70, 0.20, 0.10, 5, 'G: conservative (70/20/10)'),
]

results = []
for core, alpha, cb, n, label in configs:
    r = run(core, alpha, cb, n, label)
    results.append(r)
    print(f"  {label}: {r['annual']}% dd={r['max_dd']}% sharpe={r['sharpe']}")

results.sort(key=lambda x:-x['annual'])
print(f"\n{'='*70}")
print(f"{'Strategy':>38} {'Annual':>8} {'MaxDD':>7} {'Sharpe':>6} {'Fee/yr':>7}")
print(f"{'-'*70}")
for r in results:
    print(f"{r['label']:>38} {r['annual']:>7.2f}% {r['max_dd']:>6.1f}% {r['sharpe']:>6.2f} {r['fee_yr']:>6.2f}%")

out = {'timestamp': str(pd.Timestamp.now()), 'results': results}
with open(os.path.join(os.path.dirname(__file__),'..','..','data','backtest_v6_result.json'),'w') as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nSaved to data/backtest_v6_result.json")
