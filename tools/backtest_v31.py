#!/usr/bin/env python3
"""ATOS Backtest v31 — v28/v29 策略优化候选验证.

在 engine_v7 (60/40 QQQ+动量alpha, 63d再平衡) 基础上, 网格验证三类候选:
  A) 组合级波动率目标 (vol targeting): 按 20d 实现波动反向缩放敞口
  B) QQQ 趋势过滤 (MA200): 破位时降核心仓
  C) alpha 宇宙分散化 (加入非科技/防御标的)
并输出 胜率/盈亏比/最大回撤/年化, 优先高胜率低回撤。

输出: data/backtest_v31_result.json
"""
import os, sys, json, time, pickle, itertools
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atos.core.fee_model import futu_buy_fee, futu_sell_fee

INITIAL = 1_000_000.0
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'bt_cache_v31.pkl')

TECH = ['NVDA','AAPL','MSFT','GOOGL','META','AMZN','AVGO','AMD','CRM','NFLX','PLTR','MU','TSLA']
DIVERS = ['GLD','TLT','XLP','XLV','XLF','XLE','COST','JNJ','WMT','UNH']
ALL = ['QQQ','SPY'] + TECH + DIVERS


def _prepare_data():
    import yfinance as yf
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            return pickle.load(f)
    data = {}
    t0 = time.time()
    for sym in ALL:
        df = yf.download(sym, start='2016-01-01', end='2026-08-01',
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        data[sym] = df
    for sym in data:
        df = data[sym]
        df['rsi'] = 100 - 100/(1 + df['Close'].diff().clip(lower=0).rolling(14).mean()
                              / df['Close'].diff().clip(upper=0).abs().rolling(14).mean())
        df['ma50'] = df['Close'].rolling(50).mean()
        df['ma200'] = df['Close'].rolling(200).mean()
        df['dist_high'] = (df['Close'] / df['Close'].rolling(20).max() - 1) * 100
        df['vol20'] = df['Close'].pct_change().rolling(20).std() * np.sqrt(252)
        for lb in (21, 63, 126, 252):
            df[f'mom_{lb}'] = df['Close'].pct_change(lb)
    print(f"[{time.time()-t0:.0f}s] downloaded {len(data)} syms", flush=True)
    with open(CACHE, 'wb') as f:
        pickle.dump(data, f)
    return data


data = _prepare_data()


def _next_open(sym, dates, i):
    df = data[sym]
    if i + 1 < len(dates) and dates[i+1] in df.index:
        return float(df.loc[dates[i+1], 'Open'])
    return None


def run(universe, core_pct, alpha_pct, n_stocks, rebalance, mom_lb, w_mom,
        stop_loss=0.12, qqq_stop=0.25, vol_target=None, trend_filter=False,
        trend_core_scale=0.5):
    cash = INITIAL
    positions = {}
    total_fees = 0.0
    curve = []
    wins = 0; losses = 0
    gross_win = 0.0; gross_loss = 0.0
    dates = data['SPY'].index[252:]

    def _close(sym, p_or_none, i):
        nonlocal cash, total_fees, wins, losses, gross_win, gross_loss
        qty, avg = positions[sym]
        p = _next_open(sym, dates, i) if p_or_none is None else p_or_none
        if p is None:
            return
        fee = futu_sell_fee(qty, p)
        pnl = qty * (p - avg) - fee
        cash += qty * p - fee
        total_fees += fee
        if pnl >= 0:
            wins += 1; gross_win += pnl
        else:
            losses += 1; gross_loss += -pnl
        del positions[sym]

    for i, date in enumerate(dates):
        pv = cash + sum(q * data[s].loc[date, 'Close']
                        for s, (q, _) in positions.items()
                        if s in data and date in data[s].index)
        curve.append(pv)

        # 每日硬止损 (T+1 开盘卖)
        for sym in list(positions.keys()):
            if sym not in data or date not in data[sym].index:
                continue
            qty, avg = positions[sym]
            if avg <= 0:
                continue
            close_px = float(data[sym].loc[date, 'Close'])
            pnl_pct = (close_px - avg) / avg
            limit = qqq_stop if sym == 'QQQ' else stop_loss
            if pnl_pct <= -limit:
                _close(sym, None, i)

        if i % rebalance != 0:
            continue

        # ── 敞口缩放 (vol targeting / 趋势过滤) ──
        scale = 1.0
        if vol_target is not None:
            v = data['QQQ'].loc[date, 'vol20'] if date in data['QQQ'].index else np.nan
            if pd.notna(v) and v > 0:
                scale = min(1.0, vol_target / float(v))
        if trend_filter:
            qdf = data['QQQ']
            if date in qdf.index:
                c = float(qdf.loc[date, 'Close']); m = qdf.loc[date, 'ma200']
                if pd.notna(m) and c < float(m):
                    scale *= trend_core_scale

        # 选股
        cands = []
        for sym in universe:
            if sym not in data or date not in data[sym].index:
                continue
            row = data[sym].loc[date]
            p = float(row['Close'])
            if p <= 0:
                continue
            rsi = row.get('rsi', 50)
            rsi = 50 if pd.isna(rsi) else float(rsi)
            if rsi > 78:
                continue
            ma50 = row.get('ma50', 0)
            if pd.notna(ma50) and float(ma50) > 0 and p < float(ma50) * 0.92:
                continue
            dh = row.get('dist_high', -10)
            dh = -10.0 if pd.isna(dh) else float(dh)
            trend = max(0.0, 1 + dh / 20)
            m = row.get(f'mom_{mom_lb}', 0)
            m = 0.0 if pd.isna(m) else float(m)
            mom = max(0.0, min(1.0, (m * 100 + 5) / 10))
            cands.append((sym, mom * w_mom + trend * (1 - w_mom), p))
        cands.sort(key=lambda x: -x[1])
        top = cands[:n_stocks]

        core_val = pv * core_pct * scale
        alpha_val = pv * alpha_pct * scale
        targets = {'QQQ': core_val}
        per = alpha_val / len(top) if top else 0.0
        for sym, sc, p in top:
            targets[sym] = per

        for sym in list(positions.keys()):
            if sym not in targets:
                _close(sym, None, i)

        for sym, tv in targets.items():
            if sym not in data or date not in data[sym].index:
                continue
            p = _next_open(sym, dates, i)
            if p is None:
                continue
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
                max_sp = cash - pv * 0.0
                if cost > max_sp:
                    qty = int(max_sp / p)
                    if qty <= 0:
                        continue
                    fee = futu_buy_fee(qty, p)
                    cost = qty * p + fee
                cash -= cost; total_fees += fee
                oq, oa = positions.get(sym, (0, 0))
                nq = oq + qty
                positions[sym] = (nq, (oq * oa + qty * p) / nq if nq > 0 else p)
            else:
                sq = min(cq, int(-diff / p))
                if sq <= 0:
                    continue
                fee = futu_sell_fee(sq, p)
                cash += sq * p - fee; total_fees += fee
                nq = cq - sq
                if nq > 0:
                    positions[sym] = (nq, positions[sym][1])
                else:
                    del positions[sym]

    fv = cash + sum(q * data[s].iloc[-1]['Close']
                    for s, (q, _) in positions.items() if s in data)
    yrs = len(dates) / 252
    ar = ((fv / INITIAL) ** (1 / yrs) - 1) * 100
    vals = pd.Series(curve)
    mdd = ((vals - vals.expanding().max()) / vals.expanding().max()).min() * 100
    rets = vals.pct_change().dropna()
    sr = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    tot = wins + losses
    wr = (wins / tot * 100) if tot else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else 99
    return {'universe': 'tech' if universe is TECH else 'div',
            'core': core_pct, 'alpha': alpha_pct, 'N': n_stocks, 'rb': rebalance,
            'mom': mom_lb, 'w': w_mom, 'sl': stop_loss,
            'vol_target': vol_target, 'trend': trend_filter,
            'annual': round(ar, 2), 'max_dd': round(float(mdd), 1),
            'sharpe': round(float(sr), 2), 'win_rate': round(wr, 1),
            'pf': round(float(pf), 2), 'trades': tot,
            'fees': round(total_fees), 'final': round(fv)}


def main():
    t0 = time.time()
    spy = data['SPY']
    spy_yrs = (len(spy) - 252) / 252
    spy_ann = ((spy['Close'].iloc[-1] / spy['Close'].iloc[252]) ** (1/spy_yrs) - 1) * 100
    print(f"SPY benchmark: {spy_ann:.2f}%/yr", flush=True)

    results = []
    # 基线: 当前生产参数 (60/40, 63d, mom21, w0.6, N7, sl12%)
    base = run(TECH, 0.60, 0.40, 7, 63, 21, 0.6)
    base['label'] = 'BASELINE(prod)'
    results.append(base)
    print(f"BASELINE: annual={base['annual']}% dd={base['max_dd']}% wr={base['win_rate']}% pf={base['pf']}", flush=True)

    # A) 波动率目标
    for vt in (0.12, 0.15, 0.18, 0.20, 0.25):
        r = run(TECH, 0.60, 0.40, 7, 63, 21, 0.6, vol_target=vt)
        r['label'] = f'VOLTARGET({vt})'
        results.append(r); print(f"VOLTARGET {vt}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']} sharpe={r['sharpe']}", flush=True)

    # B) 趋势过滤
    for ts in (0.3, 0.5, 0.7):
        r = run(TECH, 0.60, 0.40, 7, 63, 21, 0.6, trend_filter=True, trend_core_scale=ts)
        r['label'] = f'TREND({ts})'
        results.append(r); print(f"TREND {ts}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']} sharpe={r['sharpe']}", flush=True)

    # C) 分散化宇宙
    div_u = TECH + DIVERS
    for ns in (7, 10):
        r = run(div_u, 0.60, 0.40, ns, 63, 21, 0.6)
        r['label'] = f'DIVERS(N{ns})'
        results.append(r); print(f"DIVERS N{ns}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']} sharpe={r['sharpe']}", flush=True)

    # D) 组合: 分散化 + vol target / trend
    for vt in (0.15, 0.18, 0.20):
        r = run(div_u, 0.60, 0.40, 7, 63, 21, 0.6, vol_target=vt)
        r['label'] = f'DIVERS+VOLTARGET({vt})'
        results.append(r); print(f"DIV+VT {vt}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']} sharpe={r['sharpe']}", flush=True)
    r = run(div_u, 0.60, 0.40, 7, 63, 21, 0.6, trend_filter=True, trend_core_scale=0.5)
    r['label'] = 'DIVERS+TREND(0.5)'
    results.append(r); print(f"DIV+TREND: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']}", flush=True)

    # E) 再平衡频率 / 动量窗 (基线宇宙, 无新机制)
    for rb in (21, 42, 126):
        r = run(TECH, 0.60, 0.40, 7, rb, 21, 0.6)
        r['label'] = f'RB({rb})'; results.append(r)
        print(f"RB {rb}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']}", flush=True)
    for ml in (63, 126):
        r = run(TECH, 0.60, 0.40, 7, 63, ml, 0.6)
        r['label'] = f'MOM({ml})'; results.append(r)
        print(f"MOM {ml}: ann={r['annual']}% dd={r['max_dd']}% wr={r['win_rate']}% pf={r['pf']}", flush=True)

    out = {'timestamp': str(pd.Timestamp.now()), 'spy_annual': round(spy_ann, 2),
           'results': results}
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'backtest_v31_result.json')
    with open(p, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nsaved {p} ({time.time()-t0:.0f}s)")

    print(f"\n{'label':22} {'ann%':>7} {'dd%':>6} {'sharpe':>7} {'WR%':>6} {'PF':>6} {'trades':>7}")
    for r in sorted(results, key=lambda x: -x['annual']):
        print(f"{r.get('label',''):22} {r['annual']:>7.2f} {r['max_dd']:>6.1f} {r['sharpe']:>7.2f} {r['win_rate']:>6.1f} {r['pf']:>6.2f} {r['trades']:>7}")


if __name__ == '__main__':
    main()
