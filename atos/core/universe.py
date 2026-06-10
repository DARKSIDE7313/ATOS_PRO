"""
ATOS PRO v2 — 扩展标的池
========================
按行业和市值精选的流动性美股。按风格分成长/价值/混合。
支持动态过滤（成交量、波动率）。
"""

# === 完整标的池（50只，跨行业） ===
UNIVERSE_FULL: dict[str, list[str]] = {
    # 科技大盘
    "tech_mega":   ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA"],
    # 半导体
    "semiconductor": ["AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "AMAT"],
    # 金融
    "financials":  ["JPM", "BAC", "GS", "MS", "V", "MA", "BLK", "SCHW"],
    # 医疗健康
    "healthcare":  ["JNJ", "UNH", "PFE", "ABBV", "MRK", "TMO", "DHR"],
    # 消费
    "consumer":    ["COST", "WMT", "HD", "NKE", "SBUX", "MCD", "DIS"],
    # 工业/能源
    "industrial":  ["CAT", "BA", "GE", "HON", "UPS", "XOM", "CVX"],
    # 防御/ETF
    "defensive":   ["SPY", "QQQ", "IWM", "TLT", "GLD", "SLV", "USO"],
    # 新增: 生物科技/云计算/清洁能源（扩展覆盖面，增加候选池）
    "extended":    ["IBB", "CRM", "ADBE", "NFLX", "PANW"],
}

# 展平为全列表
ALL_SYMBOLS: list[str] = sorted(set(
    sym for group in UNIVERSE_FULL.values() for sym in group
))

# === 策略分配 ===
LONG_TERM_SYMBOLS: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "JPM", "V", "UNH", "JNJ",
    "COST", "WMT", "SPY", "QQQ", "BRK-B", "XOM", "CAT",
]

SHORT_TERM_SYMBOLS: list[str] = [
    "NVDA", "TSLA", "AMD", "META", "AVGO", "MU", "GS", "BA",
    "NKE", "SBUX", "DIS", "IWM", "TLT",
]


def filter_by_volume(signals: dict, min_volume_ratio: float = 0.5) -> list[str]:
    """过滤掉成交量太低的标的"""
    return [
        sym for sym, s in signals.items()
        if s.get("volume_ratio", 1.0) >= min_volume_ratio
    ]


def filter_by_trend(signals: dict, allowed: tuple = ("UP", "NEUTRAL")) -> list[str]:
    """只保留趋势向上或中性的标的"""
    return [
        sym for sym, s in signals.items()
        if s.get("trend", "DOWN") in allowed
    ]


def get_active_symbols(signals: dict) -> dict[str, list[str]]:
    """根据信号质量分级：优质标的 / 观察标的 / 回避标的"""
    active = {"quality": [], "watch": [], "avoid": []}
    for sym, s in signals.items():
        trend = s.get("trend", "NEUTRAL")
        rsi = s.get("rsi", 50)
        vol_r = s.get("volume_ratio", 1.0)
        if trend == "DOWN" or rsi > 85:  # 放宽: 80→85, 只有极度超买才回避
            active["avoid"].append(sym)
        elif trend == "UP" and 35 <= rsi <= 75 and vol_r >= 0.5:  # 放宽: RSI范围40-70→35-75, vol_r 0.7→0.5
            active["quality"].append(sym)
        else:
            active["watch"].append(sym)
    return active
