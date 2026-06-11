import yfinance as yf
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atos.market.regime.regime_engine import RegimeEngine


def run_backtest(ticker="TSLA"):
    print("ATOS PRO --- 机构级策略启动")
    print(f"正在下载 {ticker} 近2年数据...")
    df = yf.download(ticker, period="2y", interval="1d", progress=False)
    if df.empty:
        print(f"[错误] 无法获取 {ticker} 的数据，请检查股票代码")
        return

    closes = df["Close"].squeeze().tolist()
    print(f"数据下载完成，共 {len(closes)} 根K线")

    cash = 100000.0
    position = 0
    entry_price = 0.0
    trades = []
    regime_engine = RegimeEngine()
    short_ma = []
    long_ma = []
    short_window = 50
    long_window = 200

    print(f"开始回测，初始资金: $100,000.00")
    print("-" * 50)

    for i in range(len(closes)):
        price = float(closes[i])
        regime_engine.update(price)

        if i >= long_window - 1:
            s = sum(closes[i-short_window+1:i+1]) / short_window
            l = sum(closes[i-long_window+1:i+1]) / long_window
            short_ma.append(s)
            long_ma.append(l)

            if len(short_ma) >= 2:
                regime = regime_engine.get_regime()
                risk_mult = regime["risk_multiplier"]
                golden_cross = short_ma[-1] > long_ma[-1] and short_ma[-2] <= long_ma[-2]
                death_cross = short_ma[-1] < long_ma[-1] and short_ma[-2] >= long_ma[-2]

                if golden_cross and position == 0 and risk_mult > 0:
                    qty = int((cash * 0.1 * risk_mult) / price)
                    if qty > 0:
                        position = qty
                        entry_price = price
                        cash -= qty * price
                        date = df.index[i].strftime('%Y-%m-%d')
                        print(f"[{date}] BUY  {qty:>4}股 @ ${price:>8.2f} | 市场状态: {regime['regime']}")

                elif death_cross and position > 0:
                    pnl = (price - entry_price) * position
                    cash += position * price
                    trades.append(pnl)
                    date = df.index[i].strftime('%Y-%m-%d')
                    label = "盈利" if pnl > 0 else "亏损"
                    print(f"[{date}] SELL {position:>4}股 @ ${price:>8.2f} | {label}: ${pnl:>8.2f}")
                    position = 0

    print("-" * 50)
    total_pnl = sum(trades)
    win_trades = [t for t in trades if t > 0]
    print(f"总交易次数 : {len(trades)}")
    print(f"盈利次数   : {len(win_trades)}")
    if trades:
        print(f"胜率       : {len(win_trades)/len(trades)*100:.1f}%")
    else:
        print("胜率       : N/A")
    print(f"总盈亏     : ${total_pnl:,.2f}")
    print(f"最终现金   : ${cash:,.2f}")
    print(f"总资产     : ${cash + position * float(closes[-1]):,.2f}")
    if not trades:
        print("[报告] 本次回测期间内无金叉信号触发")
