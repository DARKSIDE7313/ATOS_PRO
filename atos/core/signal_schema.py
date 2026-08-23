"""
ATOS PRO — 信号 Schema 契约 (Phase 5 框架重塑)
=================================================
报告 §8.2 契约: signal_engine 输出的每个标的信号 dict 遵守字段白名单。
历史教训 (幽灵字段): 消费方读取信号引擎从不输出的字段
(ma20/open/volume/high_20d/score_momentum/rsi14)，过滤器静默失效
(Pattern: phantom-field filters; v29 momentum scoring bug)。

本模块定义唯一权威白名单 + 校验函数 (只审计告警, 不修改信号值)。

核心字段 (CORE_FIELDS, 13 个) — 每个标的必须产出:
  price, ma50, ma200, rsi, macd_hist, trend, volume_ratio, atr,
  bollinger(dict), mom_21, dist_20d_high, news_score, smc_score

元字段 (META_FIELDS) — get_realtime_signals 阶段附加, 允许存在:
  data_source, realtime_price

除此之外的字段视为违规 (可能是幽灵字段的生产侧)。
"""

CORE_FIELDS = (
    "price", "ma50", "ma200", "rsi", "macd_hist", "trend",
    "volume_ratio", "atr", "bollinger",
    "mom_21", "dist_20d_high", "news_score", "smc_score",
)

META_FIELDS = ("data_source", "realtime_price")

ALLOWED_FIELDS = frozenset(CORE_FIELDS + META_FIELDS)

# trend 字段合法取值 (报告 §8.2)
TREND_VALUES = ("UP", "WEAK_UP", "DOWN", "WEAK_DOWN", "NEUTRAL")


def validate_signal(symbol: str, sig: dict) -> list:
    """校验单个标的的信号 dict。返回问题列表 (空列表 = 合规)。"""
    issues = []
    if not isinstance(sig, dict):
        return [f"{symbol}: signal is not a dict"]
    for f in CORE_FIELDS:
        if f not in sig:
            issues.append(f"{symbol}: missing core field '{f}'")
    for f in sig:
        if f not in ALLOWED_FIELDS:
            issues.append(f"{symbol}: unknown field '{f}' (phantom risk)")
    trend = sig.get("trend")
    if trend is not None and trend not in TREND_VALUES:
        issues.append(f"{symbol}: trend '{trend}' not in {TREND_VALUES}")
    if "bollinger" in sig and not isinstance(sig["bollinger"], dict):
        issues.append(f"{symbol}: bollinger should be dict, got {type(sig['bollinger']).__name__}")
    return issues


def audit_signals(signals: dict, logger=None, max_report: int = 5) -> dict:
    """审计整份信号 dict, 汇总违规情况。只告警, 不修改信号。

    Returns:
        {"ok": bool, "missing": {sym: [...]}, "unknown": {sym: [...]}, "checked": int}
    """
    missing = {}
    unknown = {}
    for sym, sig in signals.items():
        for issue in validate_signal(sym, sig):
            if "missing core field" in issue:
                missing.setdefault(sym, []).append(issue)
            else:
                unknown.setdefault(sym, []).append(issue)
    ok = not missing and not unknown
    if logger is not None and not ok:
        for sym, iss in list(missing.items())[:max_report]:
            logger.warning(f"📐 信号契约违规(缺字段): {iss}")
        for sym, iss in list(unknown.items())[:max_report]:
            logger.warning(f"📐 信号契约违规(未知字段): {iss}")
    return {"ok": ok, "missing": missing, "unknown": unknown,
            "checked": len(signals)}
