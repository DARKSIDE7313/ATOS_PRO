import pandas as pd
import numpy as np
from datetime import datetime

class PerformanceReporter:
    def __init__(self):
        self.trades = []
        self.equity_curve = []

    def record_trade(self, ticker, side, qty, price, pnl):
        self.trades.append({
            "time": datetime.utcnow(), "ticker": ticker,
            "side": side, "qty": qty, "price": price, "pnl": pnl
        })

    def record_equity(self, equity):
        self.equity_curve.append({"time": datetime.utcnow(), "equity": equity})

    def generate_report(self):
        if not self.trades:
            return """\
[报告] 本次运行无完整交易（需要金叉信号触发）
"""

        df = pd.DataFrame(self.trades)
        eq = pd.DataFrame(self.equity_curve)

        total = len(df)
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        win_rate = len(wins) / total if total > 0 else 0
        avg_win = wins["pnl"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["pnl"].mean()) if len(losses) > 0 else 1
        wl_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        total_pnl = df["pnl"].sum()

        sharpe, drawdown = 0, 0
        if len(eq) > 1:
            rets = eq["equity"].pct_change().dropna()
            sharpe = (rets.mean() / (rets.std() + 1e-10)) * np.sqrt(252)
            drawdown = ((eq["equity"] - eq["equity"].cummax()) / eq["equity"].cummax()).min()

        return f"""
╔══════════════════════════════════════╗
║     ATOS PRO — 机构级绩效报告        ║
╠══════════════════════════════════════╣
  时间：       {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}
  总交易次数： {total}
  胜率：       {win_rate:.1%}
  盈亏比：     {wl_ratio:.2f}
  总 PnL：     ${total_pnl:,.2f}
  Sharpe：     {sharpe:.2f}
  最大回撤：   {drawdown:.1%}
╚══════════════════════════════════════╝
"""
