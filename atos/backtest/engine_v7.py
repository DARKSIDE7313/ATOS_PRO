#!/usr/bin/env python3
"""ATOS Backtest v7 — v28i parameter grid search.

Optimizes the v28i strategy (QQQ core + sector momentum alpha) over:
  - rebalance frequency (trading days)
  - momentum lookback window
  - momentum vs trend-strength scoring weight
  - number of alpha stocks
Fixes core/alpha/cash at 60/40/0 (proven best in v6).
"""
import yfinance as yf
import pandas as pd
import numpy as np
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

POOL = ['NVDA','AAPL','MSFT','GOOGL','META','AMZN','AVGO','AMD','CRM','NFLX','PLTR','MU','TSLA']
ALL = ['QQQ','SPY'] + POOL
INITIAL = 300000.0

t0 = time.time()
data = {}
for sym in ALL:
    df = yf.download(sym, start='2016-01-01', end='2026-08-01', progress=False)
    if not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        data[sym] = df
print(f"[{time.time()-t0:.0f}s] Downloaded {len(data)} symbols", flush=True)

MOM_LBS = [21, 63, 126, 252]
for sym in data:
    df = data[sym]
    df['rsi'] = 100 - 100/(1 + df['Close'].diff().clip(lower=0).rolling(14).mean()
                              / df['Close'].diff().clip(upper=0).abs().rolling(14).mean())
    df['ma50'] = df['Close'].rolling(50).mean()
    df['dist_high'] = (df['Close'] / df['Close'].rolling(20).max() - 1) * 100
    for lb in MOM_LBS:
        df[f'mom_{lb}'] = df['Close'].pct_change(lb)

def run(core_pct, alpha_pct, cash_pct, n_stocks, rebalance, mom_lb, w_mom):
    cash = INITIAL
    positions = {}  # sym -> (qty, avg)
    total_fees = 0.0; trades = 0
    curve = []
    dates = data['SPY'].index[252:]  # warmup for mom_252

    for i, date in enumerate(dates):
        pv = cash + sum(q * data[s].loc[date, 'Close']
                        for s, (q, _) in positions.items()
                        if s in data and date in data[s].index)
        curve.append(pv)
        if i % rebalance != 0:
            continue

        # candidate selection
        cands = []
        for sym in POOL:
            if sym not in data or date not in data[sym].index:
                continue
            row = data[sym].loc[date]
            p = row['Close']
            if p <= 0:
                continue
            rsi = row.get('rsi', 50)
            if pd.isna(rsi):
                rsi = 50
            if rsi > 78:
                continue
            ma50 = row.get('ma50', 0)
            if pd.notna(ma50) and ma50 > 0 and p < ma50 * 0.92:
                continue
            dh = row.get('dist_high', -10)
            if pd.isna(dh):
                dh = -10
            trend = max(0.0, 1 + dh / 20)
            m = row.get(f'mom_{mom_lb}', 0)
            if pd.isna(m):
                m = 0
            mom = max(0.0, min(1.0, (m * 100 + 5) / 10))
            score = mom * w_mom + trend * (1 - w_mom)
            cands.append((sym, score, p))
        cands.sort(key=lambda x: -x[1])
        top = cands[:n_stocks]

        targets = {'QQQ': pv * core_pct}
        per = pv * alpha_pct / len(top) if top else 0.0
        for sym, sc, p in top:
            targets[sym] = per

        # sell non-targets
        for sym in list(positions.keys()):
            if sym not in targets:
                qty, avg = positions[sym]
                p = data[sym].loc[date, 'Close']
                fee = futu_sell_fee(qty, p)
                cash += qty * p - fee
                total_fees += fee; trades += 1
                del positions[sym]

        # adjust to targets
        for sym, tv in targets.items():
            if sym not in data or date not in data[sym].index:
                continue
            p = data[sym].loc[date, 'Close']
            cq = positions.get(sym, (0, 0))[0]
            diff = tv - cq * p
            if abs(diff) < 1000:
                continue
            if diff > 0:
                qty = int(diff / p)
                if qty <= 0:
                    continue
                fee = futu_buy_fee(qty, p)
                cost = qty * p + fee
                max_sp = cash - pv * cash_pct
                if cost > max_sp:
                    qty = int(max_sp / p)
                    if qty <= 0:
                        continue
                    fee = futu_buy_fee(qty, p)
                    cost = qty * p + fee
                cash -= cost; total_fees += fee; trades += 1
                oq, oa = positions.get(sym, (0, 0))
                nq = oq + qty
                positions[sym] = (nq, (oq * oa + qty * p) / nq if nq > 0 else p)
            else:
                sq = min(cq, int(-diff / p))
                if sq <= 0:
                    continue
                fee = futu_sell_fee(sq, p)
                cash += sq * p - fee; total_fees += fee; trades += 1
                nq = cq - sq
                if nq > 0:
                    positions[sym] = (nq, positions[sym][1])
                else:
                    del positions[sym]

    fv = cash + sum(q * data[s].iloc[-1]['Close'] for s, (q, _) in positions.items() if s in data)
    yrs = len(dates) / 252
    ar = ((fv / INITIAL) ** (1 / yrs) - 1) * 100
    vals = pd.Series(curve)
    mdd = ((vals - vals.expanding().max()) / vals.expanding().max()).min() * 100
    rets = vals.pct_change().dropna()
    sr = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    return {'rebalance': rebalance, 'mom_lb': mom_lb, 'w_mom': w_mom, 'n_stocks': n_stocks,
            'annual': round(ar, 2), 'max_dd': round(float(mdd), 1), 'sharpe': round(sr, 2),
            'trades': trades, 'fees': round(total_fees),
            'fee_yr': round(total_fees / INITIAL / yrs * 100, 2), 'final': round(fv)}

# SPY benchmark
spy = data['SPY']
spy_ret = (spy['Close'].iloc[-1] / spy['Close'].iloc[252] - 1) * 100
spy_yrs = (len(spy) - 252) / 252
spy_ann = ((spy['Close'].iloc[-1] / spy['Close'].iloc[252]) ** (1 / spy_yrs) - 1) * 100
print(f"\nSPY benchmark: {spy_ann:.2f}%/yr (buy & hold)\n", flush=True)

configs = []
for rebalance in [21, 42, 63, 126]:
    for mom_lb in MOM_LBS:
        for w_mom in [0.3, 0.4, 0.5, 0.6]:
            for n_stocks in [5, 7]:
                configs.append((rebalance, mom_lb, w_mom, n_stocks))

results = []
t1 = time.time()
for idx, (rb, lb, wm, ns) in enumerate(configs):
    r = run(0.60, 0.40, 0.00, ns, rb, lb, wm)
    results.append(r)
    if (idx + 1) % 32 == 0:
        print(f"  [{time.time()-t1:.0f}s] {idx+1}/{len(configs)} done", flush=True)

results.sort(key=lambda x: -x['annual'])
print(f"\n{'='*80}")
print(f"Top 12 by annual return (SPY benchmark {spy_ann:.2f}%):")
print(f"{'rb(d)':>6} {'mom':>4} {'wMom':>5} {'N':>3} {'Annual':>8} {'MaxDD':>7} {'Sharpe':>7} {'Fee/yr':>7} {'Final':>10}")
print('-'*80)
for r in results[:12]:
    print(f"{r['rebalance']:>6} {r['mom_lb']:>4} {r['w_mom']:>5.2f} {r['n_stocks']:>3} "
          f"{r['annual']:>7.2f}% {r['max_dd']:>6.1f}% {r['sharpe']:>7.2f} {r['fee_yr']:>6.2f}% {r['final']:>10,}")

# current v28i baseline for comparison
base = [r for r in results if r['rebalance'] == 63 and r['mom_lb'] == 21 and abs(r['w_mom'] - 0.4) < 0.001 and r['n_stocks'] == 5]
print(f"\nCurrent v28i baseline (63d/21d/0.4/5): {base[0]['annual'] if base else 'N/A'}%/yr")

out = {'timestamp': str(pd.Timestamp.now()), 'spy_annual': round(spy_ann, 2),
       'results': results, 'n_configs': len(configs)}
with open(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'backtest_v7_result.json'), 'w') as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nSaved data/backtest_v7_result.json ({len(configs)} configs, {time.time()-t0:.0f}s total)")
