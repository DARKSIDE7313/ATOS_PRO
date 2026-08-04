"""
ATOS PRO v17 — 统一绩效追踪 (Unified Performance Tracker)
==========================================================
每周期自动计算完整绩效指标并持久化。

参考:
  - Qlib: SignalRecord, PositionRecord
  - Freqtrade: FreqaiDataKitchen
  - Lean: AlgorithmManager.RuntimeStatistics

指标:
  - 夏普比率 (Sharpe) — 风险调整后收益
  - 索提诺比率 (Sortino) — 只惩罚下行波动
  - 卡玛比率 (Calmar) — 收益/最大回撤
  - 最大回撤 (Max DD)
  - 胜率 (Win Rate)
  - 盈亏比 (Profit Factor)
  - 期望值 (Expectancy)
  - 月收益/年化收益
  - 波动率 (年化)
  - 信息比率 (IR)
"""

import json
import math
import os
import numpy as np
from typing import Optional
from atos.core.logging import get_logger

logger = get_logger("performance")

PERF_FILE: str = None  # 初始化时设置


def init_tracker(base_dir: str):
    """初始化绩效追踪"""
    global PERF_FILE
    PERF_FILE = os.path.join(base_dir, "data", "performance.json")


def _safe_div(a, b, default=0.0):
    return a / b if b != 0 else default


class PerformanceTracker:
    """统一绩效追踪器 — 每周期更新"""

    def __init__(self):
        self.returns: list[float] = []      # 每周期收益率
        self.equity: list[float] = []       # 净值序列
        self.trades: list[float] = []       # 每笔盈亏 ($)
        self.trade_dates: list[str] = []
        self.cycles: int = 0
        self.peak_equity: float = 0
        self.max_dd: float = 0
        self.max_dd_duration: int = 0       # 最长回撤持续周期数
        self._current_dd_start: int = 0
        self._in_drawdown: bool = False

    def update(self, equity: float, cycle_return: float = None) -> dict:
        """记录新数据点，返回当前指标"""
        self.cycles += 1
        self.equity.append(equity)

        if cycle_return is not None:
            self.returns.append(cycle_return)

        # 更新峰值和回撤
        if equity > self.peak_equity:
            self.peak_equity = equity
            if self._in_drawdown:
                self._in_drawdown = False
        current_dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0
        if current_dd > self.max_dd:
            self.max_dd = current_dd
        if current_dd > 0.001 and not self._in_drawdown:
            self._in_drawdown = True
            self._current_dd_start = self.cycles
        if self._in_drawdown and current_dd < 0.001:
            duration = self.cycles - self._current_dd_start
            if duration > self.max_dd_duration:
                self.max_dd_duration = duration
            self._in_drawdown = False

        return self.get_metrics()

    def add_trade(self, pnl: float, date: str = ""):
        """记录一笔交易盈亏"""
        self.trades.append(pnl)
        if date:
            self.trade_dates.append(date)

    def get_metrics(self) -> dict:
        """计算所有绩效指标"""
        m = {"cycles": self.cycles, "peak_equity": round(self.peak_equity, 2),
             "max_drawdown": round(self.max_dd * 100, 2),
             "max_dd_duration": self.max_dd_duration}

        # 收益率统计
        if len(self.returns) > 1:
            arr = np.array(self.returns)
            avg_ret = float(np.mean(arr))
            std_ret = float(np.std(arr))
            ann_ret = avg_ret * 252 * 78  # 252天 * 78周期/天 (5分钟周期)
            ann_vol = std_ret * math.sqrt(252 * 78)
            m["annual_return"] = round(ann_ret * 100, 2)
            m["annual_volatility"] = round(ann_vol * 100, 2)

            # Sharpe (假设无风险利率4%)
            m["sharpe"] = round(_safe_div(ann_ret - 0.04, ann_vol), 3)

            # Sortino (只惩罚下行)
            downside = arr[arr < 0]
            down_std = float(np.std(downside)) * math.sqrt(252 * 78) if len(downside) > 0 else ann_vol
            m["sortino"] = round(_safe_div(ann_ret - 0.04, down_std), 3)

            # Calmar
            m["calmar"] = round(_safe_div(ann_ret, max(self.max_dd, 0.0001)), 3)
        else:
            m["sharpe"] = 0
            m["sortino"] = 0
            m["calmar"] = 0

        # 交易统计
        if self.trades:
            wins = [t for t in self.trades if t > 0]
            losses = [t for t in self.trades if t < 0]
            m["total_trades"] = len(self.trades)
            m["win_rate"] = round(len(wins) / len(self.trades) * 100, 2) if self.trades else 0
            m["avg_win"] = round(sum(wins) / len(wins), 2) if wins else 0
            m["avg_loss"] = round(sum(losses) / len(losses), 2) if losses else 0
            m["profit_factor"] = round(_safe_div(sum(wins), abs(sum(losses)), 999), 2)
            m["expectancy"] = round(m["win_rate"] / 100 * m["avg_win"] - (1 - m["win_rate"] / 100) * abs(m["avg_loss"]), 2)
            m["largest_win"] = round(max(wins), 2) if wins else 0
            m["largest_loss"] = round(min(losses), 2) if losses else 0
        else:
            m["total_trades"] = 0
            m["win_rate"] = 0
            m["profit_factor"] = 0

        # 月收益
        if len(self.returns) > 20:
            monthly = [sum(self.returns[i:i+20]) for i in range(0, len(self.returns), 20)]
            m["best_month"] = round(max(monthly) * 100, 2)
            m["worst_month"] = round(min(monthly) * 100, 2)
            m["positive_months"] = round(sum(1 for r in monthly if r > 0) / len(monthly) * 100, 1)

        # 综合评级
        if m.get("sharpe", 0) >= 1.0 and m.get("profit_factor", 0) >= 1.5 and m.get("win_rate", 0) >= 45:
            m["grade"] = "A"
        elif m.get("sharpe", 0) >= 0.5 and m.get("profit_factor", 0) >= 1.2:
            m["grade"] = "B"
        elif m.get("sharpe", 0) >= 0:
            m["grade"] = "C"
        else:
            m["grade"] = "D"

        return m

    def save(self):
        """持久化到文件"""
        if PERF_FILE:
            try:
                data = {"metrics": self.get_metrics(), "equity": self.equity[-500:],
                        "returns": self.returns[-500:], "trades": self.trades[-100:]}
                with open(PERF_FILE, 'w') as f:
                    json.dump(data, f)
            except Exception as e:
                logger.debug(f"绩效保存失败: {e}")


# 全局单例
_perf_tracker: Optional[PerformanceTracker] = None


def get_tracker() -> PerformanceTracker:
    global _perf_tracker
    if _perf_tracker is None:
        _perf_tracker = PerformanceTracker()
        # 尝试恢复
        if PERF_FILE and os.path.exists(PERF_FILE):
            try:
                with open(PERF_FILE) as f:
                    data = json.load(f)
                _perf_tracker.equity = data.get("equity", [])
                _perf_tracker.returns = data.get("returns", [])
                _perf_tracker.trades = data.get("trades", [])
                if _perf_tracker.equity:
                    _perf_tracker.peak_equity = max(_perf_tracker.equity)
                logger.info(f"绩效追踪恢复: {_perf_tracker.cycles}周期")
            except Exception:
                pass
    return _perf_tracker
