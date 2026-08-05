#!/usr/bin/env python3
"""
ATOS Backtest Engine
====================
用真实历史数据回测 ATOS 策略，对比 S&P 500 基准。

回测方法:
1. 用 yfinance 获取 10 年日线数据
2. 模拟 ATOS 核心策略（动量+均值回归+趋势过滤）
3. 集成 Futu 费用模型
4. 对比 SPY buy-and-hold
5. 输出: 年化回报/夏普/最大回撤/胜率
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
RESULT_FILE = os.path.join(BASE, "data", "backtest_result.json")

# ── 回测标的（与 ATOS 持仓一致的核心股票池）──
UNIVERSE = [
    # 科技
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD",
    "AVGO", "QCOM", "MU", "CRM", "ADBE", "NFLX", "PLTR",
    # 金融
    "JPM", "BAC", "GS", "MS", "V", "MA", "BLK", "COIN",
    # 医疗
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "TMO",
    # 消费
    "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "DIS",
    # 工业/能源
    "CAT", "BA", "GE", "HON", "XOM", "CVX",
    # ETF
    "SPY", "QQQ", "IWM", "TLT", "GLD", "IBB",
]

# ── 策略参数（v28 优化目标）──
PARAMS = {
    # 入场
    'rsi_oversold': 35,          # RSI < 此值 = 超卖
    'rsi_overbought': 72,        # RSI > 此值 = 超买
    'momentum_lookback': 20,     # 动量回看天数
    'ma_fast': 20,               # 快速均线
    'ma_slow': 50,               # 慢速均线
    'volume_ratio_min': 0.5,     # 最低量比
    'score_threshold': 0.30,     # 最低综合分

    # 出场
    'take_profit': 0.08,         # 止盈 8%
    'stop_loss': 0.04,           # 止损 4%
    'trailing_stop': 0.05,       # 移动止损 5%
    'max_hold_days': 20,         # 最大持有天数

    # 仓位
    'max_positions': 12,         # 最大持仓数
    'single_position_pct': 0.10, # 单仓最大 10%
    'min_cash_pct': 0.05,        # 最低现金 5%

    # 风控
    'max_drawdown_exit': 0.15,   # 最大回撤 15% 清仓
    'vix_high': 25,              # VIX > 此值减仓
    'spy_ma_trend': 50,          # SPY 趋势均线

    # 费用
    'use_fees': True,
}


def compute_rsi(prices, period=14):
    """计算 RSI"""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def compute_signals(df):
    """计算技术指标"""
    df = df.copy()
    df['rsi'] = compute_rsi(df['Close'])
    df['ma20'] = df['Close'].rolling(20).mean()
    df['ma50'] = df['Close'].rolling(50).mean()
    df['momentum'] = df['Close'].pct_change(PARAMS['momentum_lookback'])
    df['vol_ratio'] = df['Volume'] / df['Volume'].rolling(20).mean()
    df['high_20d'] = df['Close'].rolling(20).max()

    # MACD
    ema12 = df['Close'].ewm(span=12).mean()
    ema26 = df['Close'].ewm(span=26).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # ATR
    high_low = df['High'] - df['Low']
    high_close = abs(df['High'] - df['Close'].shift())
    low_close = abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()

    # 综合评分 (简化版 ATOS 因子模型)
    df['score'] = 0.0
    # 动量分: 价格 > MA20 > MA50 = 多头
    df.loc[(df['Close'] > df['ma20']) & (df['ma20'] > df['ma50']), 'score'] += 0.15
    # RSI 超卖回调: RSI 30-45 + 价格 > MA50 = 买入机会
    df.loc[(df['rsi'] >= 30) & (df['rsi'] <= 45) & (df['Close'] > df['ma50']), 'score'] += 0.15
    # MACD 正向
    df.loc[df['macd_hist'] > 0, 'score'] += 0.10
    # 量能配合
    df.loc[df['vol_ratio'] > 1.2, 'score'] += 0.05
    # 接近20日高点
    df.loc[df['Close'] >= df['high_20d'] * 0.98, 'score'] += 0.05
    # 均线上方
    df.loc[df['Close'] > df['ma50'], 'score'] += 0.10

    return df


def get_spy_trend(spy_df, date):
    """判断 SPY 趋势 (BULL/CAUTIOUS/BEAR)"""
    mask = spy_df.index <= date
    if mask.sum() < 50:
        return "CAUTIOUS"
    spy_close = spy_df.loc[mask, 'Close']
    ma50 = spy_close.rolling(50).mean().iloc[-1]
    current = spy_close.iloc[-1]
    if pd.isna(ma50):
        return "CAUTIOUS"
    if current > ma50 * 1.03:
        return "BULL"
    elif current < ma50 * 0.97:
        return "BEAR"
    return "CAUTIOUS"


def run_backtest(start_date='2018-01-01', end_date='2026-08-01', initial_capital=300000):
    """运行回测"""
    import yfinance as yf

    print("=" * 60)
    print("📊 ATOS 回测引擎 v28")
    print(f"   期间: {start_date} → {end_date}")
    print(f"   初始资金: ${initial_capital:,.0f}")
    print(f"   标的池: {len(UNIVERSE)} 只")
    print(f"   费用: Futu 真实费率")
    print("=" * 60)

    # 下载数据
    print("\n📥 下载历史数据...")
    all_symbols = list(set(UNIVERSE + ['SPY', '^VIX']))
    data = {}
    failed = []
    for sym in all_symbols:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(start=start_date, end=end_date)
            if len(hist) > 100:
                data[sym] = hist
            else:
                failed.append(sym)
        except Exception:
            failed.append(sym)

    print(f"   成功: {len(data)}/{len(all_symbols)} 只")
    if failed:
        print(f"   失败: {failed}")

    # 计算信号
    print("\n🔧 计算技术指标...")
    signals = {}
    for sym in data:
        if sym in ('SPY', '^VIX'):
            continue
        try:
            signals[sym] = compute_signals(data[sym])
        except Exception:
            pass

    spy_data = data.get('SPY')
    if spy_data is None:
        print("❌ 无 SPY 数据，无法回测")
        return None

    # SPY buy-and-hold 基准
    spy_start = spy_data['Close'].iloc[0]
    spy_end = spy_data['Close'].iloc[-1]
    spy_total_ret = (spy_end / spy_start) - 1
    years = len(spy_data) / 252
    spy_annual = (1 + spy_total_ret) ** (1 / years) - 1

    # ── 模拟交易 ──
    print("\n📈 模拟交易...")
    cash = initial_capital
    positions = {}  # {sym: {shares, avg_price, buy_date, high_since_buy}}
    trades = []
    equity_curve = []
    total_fees = 0.0

    trading_days = spy_data.index
    monthly_rebalance = 0

    for i, date in enumerate(trading_days):
        if i < 60:  # 需要足够数据计算指标
            continue

        # 组合估值
        port_value = cash
        for sym, pos in positions.items():
            if sym in data and date in data[sym].index:
                port_value += pos['shares'] * data[sym].loc[date, 'Close']

        # SPY 趋势
        trend = get_spy_trend(spy_data, date)

        # 最大回撤检查
        if len(equity_curve) > 0:
            peak = max(e[1] for e in equity_curve)
            dd = (peak - port_value) / peak
            if dd > PARAMS['max_drawdown_exit']:
                # 清仓
                for sym in list(positions.keys()):
                    if sym in data and date in data[sym].index:
                        price = data[sym].loc[date, 'Close']
                        fee = futu_sell_fee(positions[sym]['shares'], price) if PARAMS['use_fees'] else 0
                        total_fees += fee
                        cash += positions[sym]['shares'] * price - fee
                        trades.append({
                            'date': str(date.date()), 'sym': sym, 'action': 'SELL',
                            'price': round(price, 2), 'reason': f'DD>{PARAMS["max_drawdown_exit"]:.0%}',
                            'shares': positions[sym]['shares'],
                        })
                        del positions[sym]
                trend = "BEAR"  # 强制保守

        # ── 卖出检查 ──
        for sym in list(positions.keys()):
            if sym not in signals or date not in signals[sym].index:
                continue
            row = signals[sym].loc[date]
            pos = positions[sym]
            price = row['Close']
            pnl_pct = (price - pos['avg_price']) / pos['avg_price']

            # 更新最高价
            if price > pos.get('high_since_buy', pos['avg_price']):
                pos['high_since_buy'] = price

            sell_reason = None
            # 止盈
            if pnl_pct >= PARAMS['take_profit']:
                sell_reason = f"止盈+{pnl_pct:.1%}"
            # 止损
            elif pnl_pct <= -PARAMS['stop_loss']:
                sell_reason = f"止损{pnl_pct:.1%}"
            # 移动止损
            elif pos.get('high_since_buy', pos['avg_price']) > pos['avg_price'] * 1.03:
                trailing = (price - pos['high_since_buy']) / pos['high_since_buy']
                if trailing <= -PARAMS['trailing_stop']:
                    sell_reason = f"移动止损{trailing:.1%}"
            # 超时卖出
            elif (date - pos['buy_date']).days > PARAMS['max_hold_days']:
                if pnl_pct > 0.01:  # 有微利才卖
                    sell_reason = f"超时{PARAMS['max_hold_days']}d"

            # BEAR 市止损更紧
            if trend == "BEAR" and pnl_pct <= -0.02:
                sell_reason = f"BEAR止损{pnl_pct:.1%}"

            if sell_reason:
                fee = futu_sell_fee(pos['shares'], price) if PARAMS['use_fees'] else 0
                total_fees += fee
                cash += pos['shares'] * price - fee
                trades.append({
                    'date': str(date.date()), 'sym': sym, 'action': 'SELL',
                    'price': round(price, 2), 'reason': sell_reason,
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'shares': pos['shares'],
                })
                del positions[sym]

        # ── 买入检查（每周检查一次，避免过度交易）──
        if i % 5 != 0:  # 每 5 天检查一次买入
            equity_curve.append((date, port_value))
            continue

        if len(positions) >= PARAMS['max_positions']:
            equity_curve.append((date, port_value))
            continue

        # 排名候选
        candidates = []
        for sym in signals:
            if sym in positions or date not in signals[sym].index:
                continue
            row = signals[sym].loc[date]
            if pd.isna(row['score']) or pd.isna(row['rsi']):
                continue

            score = row['score']

            # 趋势过滤
            if trend == "BULL":
                if row['rsi'] > PARAMS['rsi_overbought']:
                    continue
                if row['vol_ratio'] < 0.3:
                    continue
            elif trend == "CAUTIOUS":
                if row['rsi'] > 65 or row['rsi'] < 30:
                    continue
                if row['vol_ratio'] < 0.5:
                    continue
                if row['macd_hist'] < -1.0:
                    continue
            else:  # BEAR
                if row['rsi'] > 55:
                    continue
                if row['Close'] < row['ma50']:
                    continue
                if row['macd_hist'] < 0:
                    continue
                score *= 1.2  # BEAR 市要求更高分

            if score < PARAMS['score_threshold']:
                continue

            candidates.append((sym, score, row['Close']))

        # 买入前 N 名
        candidates.sort(key=lambda x: -x[1])
        n_buys = min(3, PARAMS['max_positions'] - len(positions))
        for sym, score, price in candidates[:n_buys]:
            pos_value = port_value * PARAMS['single_position_pct']
            shares = max(1, int(pos_value / price))
            fee = futu_buy_fee(shares, price) if PARAMS['use_fees'] else 0
            cost = shares * price + fee

            if cost > cash * (1 - PARAMS['min_cash_pct']):
                shares = max(1, int((cash * (1 - PARAMS['min_cash_pct'])) / price))
                fee = futu_buy_fee(shares, price) if PARAMS['use_fees'] else 0
                cost = shares * price + fee
                if shares <= 0:
                    continue

            cash -= cost
            total_fees += fee
            positions[sym] = {
                'shares': shares, 'avg_price': price,
                'buy_date': date, 'high_since_buy': price,
            }
            trades.append({
                'date': str(date.date()), 'sym': sym, 'action': 'BUY',
                'price': round(price, 2), 'score': round(score, 3),
                'shares': shares,
            })

        equity_curve.append((date, port_value))

    # ── 计算结果 ──
    print("\n📊 计算结果...")

    final_value = cash
    for sym, pos in positions.items():
        last_price = data[sym]['Close'].iloc[-1]
        final_value += pos['shares'] * last_price

    total_ret = (final_value / initial_capital) - 1
    years = len(spy_data) / 252
    annual_ret = (1 + total_ret) ** (1 / years) - 1

    # 夏普比率
    if len(equity_curve) > 1:
        eq_values = [e[1] for e in equity_curve]
        returns = pd.Series(eq_values).pct_change().dropna()
        sharpe = returns.mean() / returns.std() * np.sqrt(252 / 5) if returns.std() > 0 else 0  # 每周采样
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

    # 胜率
    sell_trades = [t for t in trades if t['action'] == 'SELL' and 'pnl_pct' in t]
    wins = [t for t in sell_trades if t.get('pnl_pct', 0) > 0]
    win_rate = len(wins) / len(sell_trades) if sell_trades else 0

    # 盈亏比
    avg_win = np.mean([t['pnl_pct'] for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t['pnl_pct']) for t in sell_trades if t.get('pnl_pct', 0) <= 0]) if any(t.get('pnl_pct', 0) <= 0 for t in sell_trades) else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    beats_spy = annual_ret > spy_annual

    result = {
        'timestamp': datetime.now().isoformat(),
        'period': f"{start_date} → {end_date}",
        'years': round(years, 1),
        'initial_capital': initial_capital,
        'final_value': round(final_value, 2),
        'total_return': round(total_ret * 100, 2),
        'annual_return': round(annual_ret * 100, 2),
        'spy_annual_return': round(spy_annual * 100, 2),
        'beats_spy': beats_spy,
        'excess_return': round((annual_ret - spy_annual) * 100, 2),
        'sharpe_ratio': round(sharpe, 2),
        'max_drawdown': round(max_dd * 100, 2),
        'total_trades': len(trades),
        'sell_trades': len(sell_trades),
        'win_rate': round(win_rate * 100, 1),
        'profit_factor': round(profit_factor, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'total_fees': round(total_fees, 2),
        'fee_drag': round(total_fees / initial_capital * 100, 3),
        'open_positions': len(positions),
        'params': PARAMS,
    }

    # 打印结果
    print("\n" + "=" * 60)
    print("📊 回测结果")
    print("=" * 60)
    print(f"  期间: {result['period']} ({years:.1f} 年)")
    print(f"  初始: ${initial_capital:,.0f} → 最终: ${final_value:,.0f}")
    print(f"  总回报: {total_ret:.1%} | 年化: {annual_ret:.2%}")
    print(f"  SPY 年化: {spy_annual:.2%} | 超额: {(annual_ret - spy_annual):.2%}")
    print(f"  {'✅ 跑赢 SPY!' if beats_spy else '❌ 未跑赢 SPY'}")
    print(f"  夏普: {sharpe:.2f} | 最大回撤: {max_dd:.1%}")
    print(f"  交易: {len(trades)} 笔 ({len(sell_trades)} 卖出)")
    print(f"  胜率: {win_rate:.1%} | 盈亏比: {profit_factor:.2f}")
    print(f"  费用: ${total_fees:,.2f} ({total_fees/initial_capital*100:.3f}%)")

    # 保存
    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"\n💾 结果保存: {RESULT_FILE}")

    # 保存交易记录
    trades_file = os.path.join(BASE, "data", "backtest_trades.json")
    with open(trades_file, 'w') as f:
        json.dump(trades[-100:], f, indent=2)  # 最近 100 笔

    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2018-01-01')
    parser.add_argument('--end', default='2026-08-01')
    parser.add_argument('--capital', type=int, default=300000)
    args = parser.parse_args()

    result = run_backtest(args.start, args.end, args.capital)
    if result:
        sys.exit(0 if result['beats_spy'] else 1)
    sys.exit(2)
