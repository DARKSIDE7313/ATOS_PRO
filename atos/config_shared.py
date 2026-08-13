"""
ATOS PRO — 统一资金配置 (Single Source of Truth)

所有模块的资金分配从这里读取，不再各自硬编码。
修复 #1: 消除 config_shared / phoenix config / dashboard 三套账本。
"""

# === 总资金 ===
TOTAL_CAPITAL = 1_000_000  # $1M paper trading

# === 资金分配 ===
ALLOCATION = {
    "short_term": 1_000_000,  # v30: 统一单一策略资金 $100万 (长线已移除)
    "long_term":  0,           # v30: 长线组合已移除 (v29归档)
    "reserve":    0,           # v30: 现金储备并入统一资金
}

# === 单策略限制 ===
MAX_POSITION_PCT = {
    "long_term":  0.15,      # 长期单仓 ≤15%
    "short_term": 0.15,      # 短期单仓 ≤15% (从20%降为15%)
}

MAX_POSITIONS = {
    "long_term":  12,        # 长期最多12只
    "short_term": 10,        # 短期最多10只
}

# === 风控全局阈值 ===
RISK = {
    "max_daily_loss_pct":   0.02,   # 日亏损2%熔断
    "max_drawdown_pct":     0.15,   # 最大回撤15%
    "max_consecutive_losses": 5,    # 连续5次亏损降频
    "stop_loss_pct":        0.07,   # 硬止损7% (v6 进攻性, 从8%收紧)
    "take_profit_pct":      0.11,   # 止盈11% (v6 进攻性, 从12%收紧)
    "cooldown_cycles":      48,     # 冷却周期
}

# === 仪表盘配置 ===
DASHBOARD = {
    "initial_capital": TOTAL_CAPITAL,  # 与总资金一致
}


def get_short_term_capital() -> float:
    return ALLOCATION["short_term"]


def get_long_term_capital() -> float:
    return ALLOCATION["long_term"]


def get_reserve() -> float:
    return ALLOCATION["reserve"]
