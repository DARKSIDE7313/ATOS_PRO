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

# === 仓位上限 (Phase6 P1-2: 单一真源) ===
# risk_gate.CAPS 与 account.max_single_pct/ETF_MAX_PCT/min_cash_pct 统一从这里读。
# 修正: 原 MAX_POSITION_PCT["short_term"]=0.15 是误导性死配置, 实际单仓上限 = 12%。
POSITION_CAPS = {
    "single_stock_pct":   0.12,       # 个股单仓上限 12% (专业基金标准)
    "etf_pct":            0.65,       # ETF 单仓上限 65% (QQQ 核心仓设计)
    "total_position_pct": 0.98,       # 总仓位上限 98% (留 2% 现金缓冲)
    "min_cash_pct":       0.02,       # 最低现金 2%
    "price_collar_pct":   0.05,       # 价格 collar ±5%
    "max_order_notional": 200_000,    # 单笔名义上限 $200K
}

MAX_POSITIONS = {
    "long_term":  12,        # 长期最多12只
    "short_term": 10,        # 短期最多10只
}

# === 风控全局阈值 ===
RISK = {
    "max_daily_loss_pct":   0.025,  # 日亏损2.5%熔断
    "max_drawdown_pct":     0.10,   # 最大回撤10%
    "max_consecutive_losses": 3,    # 连续3次亏损降频
    "stop_loss_pct":        0.05,   # 硬止损5% (v16: 从6%收紧)
    "take_profit_pct":      0.18,   # 止盈18% (让赢家奔跑)
    "cooldown_cycles":      24,     # 冷却周期 (v9: 从48→24)
}

# === 仪表盘配置 ===
DASHBOARD = {
    "initial_capital": TOTAL_CAPITAL,  # 与总资金一致
}

# === Futu 交易配置（L6: 硬编码账号集中到配置） ===
FUTU = {
    "acc_id": 19489722,
    "trd_env": "SIMULATE",  # "SIMULATE" | "REAL"
    "host": "127.0.0.1",
    "port": 11111,
}


def get_short_term_capital() -> float:
    return ALLOCATION["short_term"]


def get_long_term_capital() -> float:
    return ALLOCATION["long_term"]


def get_reserve() -> float:
    return ALLOCATION["reserve"]
