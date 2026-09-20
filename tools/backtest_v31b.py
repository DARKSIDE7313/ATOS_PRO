#!/usr/bin/env python3
"""Backtest v31b — 聚焦前沿搜索: 分散化宇宙 × 趋势过滤 × 仓位. """
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.backtest_v31 import run, data, TECH, DIVERS, INITIAL
import pandas as pd

DEFONLY = TECH + ['XLP', 'XLV', 'XLF', 'COST', 'JNJ', 'WMT', 'UNH']
BOND = TECH + ['GLD', 'TLT']
DIVERSEG = TECH + ['GLD', 'TLT', 'XLP', 'XLV', 'XLF']

results = []
def add(label, u, **kw):
    r = run(u, kw.pop('core', 0.60), kw.pop('alpha', 0.40), kw.pop('n', 7), kw.pop('rb', 63),
            kw.pop('mom', 21), kw.pop('w', 0.6), **kw)
    r['label'] = label
    r['universe_name'] = 'tech' if u == TECH else ('defonly' if u == DEFONLY else ('bond' if u == BOND else ('diverseg' if u == DIVERSEG else 'divers')))
    results.append(r)
    print(f"{label:30} ann={r['annual']:>6.2f}% dd={r['max_dd']:>6.1f}% sh={r['sharpe']:>5.2f} WR={r['win_rate']:>5.1f}% PF={r['pf']:>5.2f} n={r['trades']}", flush=True)
    return r

# 基线 (对照)
add('BASELINE(prod tech/N7)', TECH, n=7)
# 分散化 + 趋势
add('DIVERS N7 +TREND0.7', DIVERS, n=7, trend_filter=True, trend_core_scale=0.7)
add('DIVERS N10+TREND0.7', DIVERS, n=10, trend_filter=True, trend_core_scale=0.7)
add('DIVERS N7 +TREND0.6', DIVERS, n=7, trend_filter=True, trend_core_scale=0.6)
add('DEFONLY N7 +TREND0.7', DEFONLY, n=7, trend_filter=True, trend_core_scale=0.7)
add('DEFONLY N7', DEFONLY, n=7)
add('DEFONLY N10+TREND0.7', DEFONLY, n=10, trend_filter=True, trend_core_scale=0.7)
add('BOND N7 +TREND0.7', BOND, n=7, trend_filter=True, trend_core_scale=0.7)
add('DIVERSEG N7+TREND0.7', DIVERSEG, n=7, trend_filter=True, trend_core_scale=0.7)
# 纯技术宇宙 + 趋势/仓位
add('TECH N7 +TREND0.8', TECH, n=7, trend_filter=True, trend_core_scale=0.8)
add('TECH N5 +TREND0.7', TECH, n=5, trend_filter=True, trend_core_scale=0.7)
add('TECH N10+TREND0.7', TECH, n=10, trend_filter=True, trend_core_scale=0.7)
# 核心/alpha 比例
add('TECH N7 TREND0.7 core.5', TECH, n=7, core=0.50, alpha=0.50, trend_filter=True, trend_core_scale=0.7)
add('DIVERS N7 TREND0.7 core.5', DIVERS, n=7, core=0.50, alpha=0.50, trend_filter=True, trend_core_scale=0.7)
# 止损放宽
add('DIVERS N7 TREND.7 sl10', DIVERS, n=7, stop_loss=0.10, trend_filter=True, trend_core_scale=0.7)
add('DIVERS N7 TREND.7 sl15', DIVERS, n=7, stop_loss=0.15, trend_filter=True, trend_core_scale=0.7)

# 汇总: 按 胜率*PF / 回撤 排序
print("\n" + "=" * 100)
print(f"{'label':28} {'ann%':>7} {'dd%':>6} {'sharpe':>7} {'WR%':>6} {'PF':>6} {'trades':>7}")
for r in sorted(results, key=lambda x: -x['win_rate']):
    print(f"{r['label']:28} {r['annual']:>7.2f} {r['max_dd']:>6.1f} {r['sharpe']:>7.2f} {r['win_rate']:>6.1f} {r['pf']:>6.2f} {r['trades']:>7}")

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'backtest_v31b_result.json')
with open(p, 'w') as f:
    json.dump({'timestamp': str(pd.Timestamp.now()), 'results': results}, f, indent=2, default=str)
print(f"\nsaved {p}")
