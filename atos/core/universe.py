"""
ATOS PRO v2 — 扩展标的池
========================
按行业和市值精选的流动性美股。按风格分成长/价值/混合。
支持动态过滤（成交量、波动率）。
"""

# === 完整标的池（扩展为70只，增加AI/半导体/高波动标的） ===
UNIVERSE_FULL: dict[str, list[str]] = {
    # 科技大盘
    "tech_mega":   ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA"],
    # 半导体
    "semiconductor": ["AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "AMAT", "ARM", "MRVL"],
    # 金融
    "financials":  ["JPM", "BAC", "GS", "MS", "V", "MA", "BLK", "SCHW", "COIN"],
    # 医疗健康
    "healthcare":  ["JNJ", "UNH", "PFE", "ABBV", "MRK", "TMO", "DHR"],
    # 消费
    "consumer":    ["COST", "WMT", "HD", "NKE", "SBUX", "MCD", "DIS"],
    # 工业/能源
    "industrial":  ["CAT", "BA", "GE", "HON", "UPS", "XOM", "CVX"],
    # 防御/ETF
    "defensive":   ["SPY", "QQQ", "IWM", "TLT", "GLD", "SLV", "USO"],
    # 高波动/AI题材（进攻型新增）— NVDA 已存在于 tech_mega，此组不再重复
    "high_beta":   ["MARA", "PLTR", "SOFI", "RKLB", "ASTS", "TSLL", "IONQ", "SMCI"],
    # 扩展
    "extended":    ["IBB", "CRM", "ADBE", "NFLX", "PANW"],
}

# 展平为全列表（已去重，NVDA只出现1次）
ALL_SYMBOLS: list[str] = []
seen = set()
for sym in [s for g in UNIVERSE_FULL.values() for s in g]:
    if sym not in seen:
        ALL_SYMBOLS.append(sym)
        seen.add(sym)
ALL_SYMBOLS_DEDUPED: list[str] = ALL_SYMBOLS[:]  # 都是去重的，alias

# === 策略分配 ===
LONG_TERM_SYMBOLS: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "JPM", "V", "UNH", "JNJ",
    "COST", "WMT", "SPY", "QQQ", "BRK-B", "XOM", "CAT",
]

# v16: Shadow 聚焦科技+金融+周期，移除与 LongTerm 重叠的防御股（MRK,JNJ,DIS）
SHORT_TERM_SYMBOLS: list[str] = [
    "NVDA", "TSLA", "AMD", "META", "AVGO", "MU", "GS", "BA",
    "NKE", "SBUX", "IWM", "TLT", "MS", "CAT", "UPS", "JPM",
    "COIN", "PLTR", "SOFI", "MARA",
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
