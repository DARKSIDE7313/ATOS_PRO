#!/usr/bin/env python3
"""Backtest v31c — 定稿: 防御分散宇宙 × 趋势去险强度 × 核心比例."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.backtest_v31 import run, TECH
import pandas as pd

DEFONLY = TECH + ['XLP', 'XLV', 'XLF', 'COST', 'JNJ', 'WMT', 'UNH']
DEFONLY8 = TECH + ['XLP', 'XLV', 'XLF', 'COST', 'JNJ', 'WMT', 'UNH', 'XLE']

results = []
def add(label, u, **kw):
    r = run(u, kw.pop('core', 0.60), kw.pop('alpha', 0.40), kw.pop('n', 7), kw.pop('rb', 63),
            kw.pop('mom', 21), kw.pop('w', 0.6), **kw)
    r['label'] = label; results.append(r)
    print(f"{label:32} ann={r['annual']:>6.2f}% dd={r['max_dd']:>6.1f}% sh={r['sharpe']:>5.2f} WR={r['win_rate']:>5.1f}% PF={r['pf']:>5.2f} n={r['trades']}", flush=True)
    return r

add('BASELINE(prod)', TECH, n=7)
add('DEFONLY N7 (no trend)', DEFONLY, n=7)
for ts in (0.8, 0.9, 1.0):
    add(f'DEFONLY N7 TREND{ts}', DEFONLY, n=7, trend_filter=True, trend_core_scale=ts)
add('DEFONLY N7 core.5 TREND0.8', DEFONLY, n=7, core=0.50, alpha=0.50, trend_filter=True, trend_core_scale=0.8)
add('DEFONLY8 N7 TREND0.8', DEFONLY8, n=7, trend_filter=True, trend_core_scale=0.8)
add('DEFONLY N8 TREND0.8', DEFONLY, n=8, trend_filter=True, trend_core_scale=0.8)

print("\n" + "=" * 100)
print(f"{'label':30} {'ann%':>7} {'dd%':>6} {'sharpe':>7} {'WR%':>6} {'PF':>6} {'trades':>7}")
for r in sorted(results, key=lambda x: -x['win_rate']):
    print(f"{r['label']:30} {r['annual']:>7.2f} {r['max_dd']:>6.1f} {r['sharpe']:>7.2f} {r['win_rate']:>6.1f} {r['pf']:>6.2f} {r['trades']:>7}")

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'backtest_v31c_result.json')
with open(p, 'w') as f:
    json.dump({'timestamp': str(pd.Timestamp.now()), 'results': results}, f, indent=2, default=str)
print(f"\nsaved {p}")
