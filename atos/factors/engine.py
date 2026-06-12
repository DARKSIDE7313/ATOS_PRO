"""
ATOS PRO v2 — 因子合成引擎
===========================
综合价值、动量、质量、技术、多时间框架因子，
根据市场环境动态调整权重，输出最终排名。
支持 IC 分析检验因子有效性。
"""
import math
from typing import Optional
from atos.core.logging import get_logger
from atos.core.universe import ALL_SYMBOLS

logger = get_logger("factors.engine")

# 默认权重（会根据市场状态动态调整）
DEFAULT_WEIGHTS = {
    "value":      0.05,   # 价值（短线不看估值 — 降权）
    "momentum":   0.25,   # 动量（提升 — 趋势跟踪）
    "quality":    0.05,   # 质量（短线不看重基本面 — 降权）
    "technical":  0.30,   # 技术面（主力 — RSI/MACD/趋势/Bollinger）
    "multiframe": 0.15,   # 多时间框架（提升 — 多时间确认）
    "mean_rev":   0.20,   # 均值回归（提升 — CAUTIOUS/BULL_WEAK 中回归策略有效）
}

# IC 历史记录：{regime: {"last_ic": float, "weight_adjustments": {...}}}
_ic_history: dict[str, dict] = {}
_ic_lock = __import__('threading').Lock()

# 每个因子的 IC 记录：{regime: {factor: ic_value}}
_per_factor_ic: dict[str, dict[str, float]] = {}

# 不同市场环境下的权重调整
# v4 重大修改：
#   - BEAR: 动量权重大幅提升(0.08→0.30)，均值回归降至0.05（下跌趋势中抄底=找死）
#   - BULL_STRONG: 动量为主，均值回归辅助(0.17→0.12)
#   - HIGH_VOL: 质量和价值为主，均值回归降至0.12
#   - 所有环境下：技术面保持主力地位
REGIME_WEIGHTS = {
    "BULL_STRONG": {"momentum": 0.30, "technical": 0.25, "value": 0.05, "quality": 0.05, "multiframe": 0.20, "mean_rev": 0.15},
    "BULL_WEAK":   {"momentum": 0.22, "technical": 0.30, "value": 0.05, "quality": 0.05, "multiframe": 0.18, "mean_rev": 0.20},
    "HIGH_VOL":    {"momentum": 0.20, "technical": 0.25, "value": 0.10, "quality": 0.10, "multiframe": 0.18, "mean_rev": 0.17},  # 降低 mean_rev 防高波动抄底
    "BEAR":        {"momentum": 0.18, "technical": 0.22, "value": 0.12, "quality": 0.18, "multiframe": 0.20, "mean_rev": 0.10},  # BEAR: 降低 mean_rev 0.15→0.10，提升 quality 防踩雷
    "SIDEWAYS":    {"momentum": 0.20, "technical": 0.28, "value": 0.08, "quality": 0.08, "multiframe": 0.16, "mean_rev": 0.20},
    "UNKNOWN":     {"momentum": 0.25, "technical": 0.30, "value": 0.07, "quality": 0.08, "multiframe": 0.15, "mean_rev": 0.15},
}


def _tech_score(signal: dict) -> float:
    """将技术信号转为 0-1 得分"""
    if not signal:
        return 0.5
    score = 0.5
    trend = signal.get("trend", "NEUTRAL")
    rsi = signal.get("rsi", 50)
    vol_r = signal.get("volume_ratio", 1.0)
    bb = signal.get("bollinger", {})

    # 趋势加分
    trend_map = {"UP": 0.2, "WEAK_UP": 0.1, "NEUTRAL": 0.0, "WEAK_DOWN": -0.1, "DOWN": -0.2}
    score += trend_map.get(trend, 0)

    # RSI: 40-70 健康，过买过卖扣分
    if 40 <= rsi <= 70:
        score += 0.1
    elif rsi < 30:
        score -= 0.15
    elif rsi > 80:
        score -= 0.15

    # 放量加分
    if 1.2 <= vol_r <= 3.0:
        score += 0.1
    elif vol_r < 0.5:
        score -= 0.1

    # 布林带位置
    pct_b = bb.get("pct_b", 0.5)
    if 0.2 <= pct_b <= 0.8:
        score += 0.05
    elif pct_b < 0.1 or pct_b > 0.9:
        score -= 0.1

    return max(0.05, min(0.95, score))


def _mean_rev_score(signal: dict) -> float:
    """均值回归评分——从 Bollinger Bands + RSI 判断价格是否偏离均值。

    核心逻辑（受 je-suis-tm/quant-trading 的 W-Bottom 策略启发）：
      - 价格接近下轨 + RSI 低 → 高均值回归买入信号
      - 价格接近上轨 + RSI 高 → 高均值回归卖出信号
      - 价格在均值附近 → 中性

    返回 0-1 分，越高表示越值得买入（均值回归视角）。
    """
    if not signal:
        return 0.5

    rsi = signal.get("rsi", 50)
    bb = signal.get("bollinger", {})
    pct_b = bb.get("pct_b", 0.5)
    trend = signal.get("trend", "NEUTRAL")

    # %B = (price - lower) / (upper - lower)
    # %B < 0.2 = 接近下轨（可能超卖），%B > 0.8 = 接近上轨（可能超买）
    score = 0.5

    # RSI 均值回归信号
    if rsi < 30:
        score += 0.25  # 极度超卖 → 强回归买入信号
    elif rsi < 35:
        score += 0.15
    elif rsi < 40:
        score += 0.08
    elif rsi > 75:
        score -= 0.20  # 极度超买 → 均值回归卖出信号
    elif rsi > 70:
        score -= 0.12
    elif rsi > 65:
        score -= 0.05

    # Bollinger %B 位置
    if pct_b < 0.15:
        score += 0.20  # 明显低于下轨 → 回归买入
    elif pct_b < 0.30:
        score += 0.10
    elif pct_b > 0.85:
        score -= 0.20  # 明显高于上轨 → 回归卖出
    elif pct_b > 0.70:
        score -= 0.10

    # 趋势负向时均值回归信号更强
    if trend == "DOWN" and rsi < 35:
        score += 0.10  # 跌势中超卖 → 更强回归信号
    elif trend == "UP" and rsi > 70:
        score -= 0.10  # 涨势中超买 → 更强回归卖出

    return max(0.05, min(0.95, score))


def adjust_weights_from_ic(regime: str) -> dict:
    """
    根据历史 IC 调整因子权重。

    规则：
      - last_ic > 0.05  (正预测能力) → 提升 IC 最高的因子
      - last_ic < -0.05 (负预测能力) → 缩减 IC 最低的因子
      - 其余情况 → 使用默认权重

    返回调整后的权重 dict {factor: weight}。
    """
    with _ic_lock:
        history = _ic_history.get(regime)
        if not history:
            return dict(REGIME_WEIGHTS.get(regime, DEFAULT_WEIGHTS))
        last_ic = history.get("last_ic", 0.0)
        per_factor = dict(_per_factor_ic.get(regime, {}))
    
    base_weights = dict(REGIME_WEIGHTS.get(regime, DEFAULT_WEIGHTS))

    if last_ic > 0.05:
        # 正相关 → 提升 IC 最高的因子（如果 per-factor IC 数据可用）
        if per_factor:
            best_factor = max(per_factor, key=per_factor.get)
            logger.info(f"IC={last_ic:.4f} > 0.05: 提升因子 {best_factor} (pi={per_factor[best_factor]:.4f})")
        else:
            best_factor = max(base_weights, key=base_weights.get)
        boost = base_weights[best_factor] * 0.10  # 提升 10%
        base_weights[best_factor] = round(base_weights[best_factor] + boost, 4)
        logger.info(f"IC={last_ic:.4f} > 0.05: 提升 {best_factor} 权重至 {base_weights[best_factor]:.3f}")
    elif last_ic < -0.05:
        # 负相关 → 缩减 IC 最低的因子（如果 per-factor IC 数据可用）
        if per_factor:
            worst_factor = min(per_factor, key=per_factor.get)
            logger.info(f"IC={last_ic:.4f} < -0.05: 缩减因子 {worst_factor} (pi={per_factor[worst_factor]:.4f})")
        else:
            worst_factor = max(base_weights, key=base_weights.get)
        reduction = base_weights[worst_factor] * 0.20
        base_weights[worst_factor] = round(base_weights[worst_factor] - reduction, 4)
        logger.info(f"IC={last_ic:.4f} < -0.05: 缩减 {worst_factor} 权重至 {base_weights[worst_factor]:.3f}")
    else:
        logger.info(f"IC={last_ic:.4f} 在 [-0.05, 0.05] 内，使用默认权重（AI将在CIO阶段动态调权）")

    # 归一化确保权重总和为 1.0
    total = sum(base_weights.values())
    if total > 0:
        base_weights = {k: round(v / total, 4) for k, v in base_weights.items()}

    return base_weights


def combine(signals: dict, value_factors: dict, momentum_factors: dict,
            quality_factors: dict, regime: str = "UNKNOWN",
            multiframe_factors: Optional[dict] = None,
            use_v3_signals: bool = False) -> dict:
    """
    综合所有因子，输出每只标的的综合评分。

    参数:
        signals: 技术信号 {symbol: {price, rsi, trend, ...}}
        value_factors: 价值因子 {symbol: {composite, ...}}
        momentum_factors: 动量因子 {symbol: {composite, ...}}
        quality_factors: 质量因子 {symbol: {composite, ...}}
        regime: 市场状态
        multiframe_factors: 多时间框架因子 {symbol: {composite, ...}}
                           （来自 strategy_v3.get_v3_signals）

    返回:
        {
            "rankings": [(symbol, score), ...],   # 排名
            "scores": {symbol: 0.75, ...},         # 原始分数
            "breakdown": {symbol: {value:0.8, ...}}, # 分解
            "weights": {value: 0.2, ...},           # 实际权重
            "ic": 0.15,                             # 当前IC
        }
    """
    # 通过 IC 反馈循环调整权重
    weights = adjust_weights_from_ic(regime)
    scores = {}
    breakdown = {}

    if multiframe_factors is None:
        multiframe_factors = {}

    # 如果启用 v3 信号，从已有的 signal_engine 数据中计算多时间框架信号
    # BUGFIX 2026-06-11: 不再单独调 yf.download()（太慢），所有数据从 signals 提取
    if use_v3_signals:
        try:
            from atos.live.strategy_v3 import composite_signal
            v3_factors = {}
            for sym in list(signals.keys()):
                result = composite_signal(sym, regime, signals[sym])
                v3_factors[sym] = {
                    "composite": result["score"],
                    "decision": result["decision"],
                    "multi_timeframe": result.get("multi_timeframe", {}),
                    "reasons": result.get("reasons", []),
                }
            multiframe_factors = v3_factors
            logger.info(f"v3多时间框架信号计算完成: {len(v3_factors)} 只标的")
        except Exception as e:
            logger.warning(f"v3信号计算失败: {e}，使用默认 multiframe_factors")

    for sym in ALL_SYMBOLS:
        if sym not in signals:
            continue

        v_score = value_factors.get(sym, {}).get("composite", 0.5)
        m_score = momentum_factors.get(sym, {}).get("composite", 0.5)
        q_score = quality_factors.get(sym, {}).get("composite", 0.5)
        t_score = _tech_score(signals[sym])
        f_score = multiframe_factors.get(sym, {}).get("composite", 0.5)
        r_score = _mean_rev_score(signals[sym])  # 🆕 均值回归
        smc_score_raw = signals[sym].get("smc_score", {}).get("smc_score", 0.0)  # 🆕 SMC聪明钱

        # SMC得分范围-0.6~0.6，映射到0~1区间（中线0.5）
        # smc_score_raw > 0 → 看涨信号 → 加分
        # smc_score_raw < 0 → 看跌信号 → 减分
        smc_normalized = 0.5 + smc_score_raw  # -0.6~0.6 → -0.1~1.1, clamp to 0~1
        smc_normalized = max(0.0, min(1.0, smc_normalized))

        # 核心因子加权（不包含SMC——SMC作为独立修正）
        core_weights = {k: weights.get(k, 0) for k in ["value", "momentum", "quality", "technical", "multiframe", "mean_rev"]}
        core_sum = sum(core_weights.values())
        if core_sum > 0:
            # 归一化核心权重到总和为1，然后加权
            total = (
                v_score * core_weights["value"] / core_sum +
                m_score * core_weights["momentum"] / core_sum +
                q_score * core_weights["quality"] / core_sum +
                t_score * core_weights["technical"] / core_sum +
                f_score * core_weights["multiframe"] / core_sum +
                r_score * core_weights["mean_rev"] / core_sum
            )
        else:
            total = 0.5  # Fallback
        
        # SMC 作为独立修正因子（5%权重）
        total = total * 0.95 + smc_normalized * 0.05
        total = max(0.0, min(1.0, total))

        scores[sym] = round(total, 4)
        breakdown[sym] = {
            "value": round(v_score, 3),
            "momentum": round(m_score, 3),
            "quality": round(q_score, 3),
            "technical": round(t_score, 3),
            "multiframe": round(f_score, 3),
            "mean_rev": round(r_score, 3),  # 🆕
            "total": round(total, 3),
        }

    # 按分数排名
    rankings = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    logger.info(
        f"因子合成完成: {len(scores)} 只 | "
        f"Top3: {rankings[:3]} | 权重={weights}"
    )

    # 存储权重（供后续 IC 反馈循环使用）— 加锁
    with _ic_lock:
        _ic_history[regime] = {
            "last_ic": _ic_history.get(regime, {}).get("last_ic", 0.0),
            "weight_adjustments": dict(weights),
        }

    return {
        "rankings": rankings,
        "scores": scores,
        "breakdown": breakdown,
        "weights": weights,
    }


def ic_analysis(prev_scores: dict, current_returns: dict, regime: str = "UNKNOWN",
                prev_breakdown: Optional[dict] = None) -> dict:
    """
    信息系数分析。
    IC = 上一期因子得分 与 当期实际收益 的秩相关系数。
    |IC| > 0.05 说明因子有效；|IC| > 0.1 说明因子很强。

    分析结果自动回写到 _ic_history，供下次 combine() 使用。
    """
    common = set(prev_scores.keys()) & set(current_returns.keys())
    if len(common) < 10:
        result = {"ic": 0.0, "n": len(common), "verdict": "数据不足"}
        # 即使数据不足，也更新 IC 历史（IC=0 不会触发调整）
        _ic_history.setdefault(regime, {})["last_ic"] = 0.0
        return result

    scores_list = []
    returns_list = []
    for sym in common:
        scores_list.append(prev_scores[sym])
        returns_list.append(current_returns[sym])

    # Spearman 秩相关
    ic = _spearman(scores_list, returns_list)

    if abs(ic) > 0.1:
        verdict = "因子很强 ✅"
    elif abs(ic) > 0.05:
        verdict = "因子有效 🟡"
    elif abs(ic) > 0.02:
        verdict = "因子较弱 🟠"
    else:
        verdict = "因子无效 ❌"

    logger.info(f"IC = {ic:.4f} | {verdict} (n={len(common)})")

    result = {"ic": round(ic, 4), "n": len(common), "verdict": verdict}

    # 回写 IC 到历史（加锁）
    with _ic_lock:
        _ic_history.setdefault(regime, {})["last_ic"] = result["ic"]

    # 计算每个因子的 IC（如果 breakdown 数据可用）
    if prev_breakdown and len(common) >= 10:
        factor_names = ["value", "momentum", "quality", "technical", "multiframe"]
        per_factor_ics = {}
        for factor in factor_names:
            factor_scores = []
            for sym in common:
                bd = prev_breakdown.get(sym, {})
                score = bd.get(factor, None)
                if score is not None:
                    factor_scores.append((sym, score))
            if len(factor_scores) >= 10:
                fs, factor_vals = zip(*factor_scores)
                factor_returns = [current_returns[s] for s in fs]
                pi = _spearman(list(factor_vals), factor_returns)
                per_factor_ics[factor] = round(pi, 4)
        if per_factor_ics:
            with _ic_lock:
                _per_factor_ic[regime] = per_factor_ics
            best_f = max(per_factor_ics, key=per_factor_ics.get)
            worst_f = min(per_factor_ics, key=per_factor_ics.get)
            logger.info(f"每个因子 IC: {per_factor_ics} | 最佳={best_f}={per_factor_ics[best_f]:.4f}, 最差={worst_f}={per_factor_ics[worst_f]:.4f}")

    return result


def get_top_picks(combine_result: dict, n: int = 10,
                  min_score: float = 0.55) -> list[dict]:
    """从综合结果中提取 Top N 推荐标的"""
    rankings = combine_result["rankings"]
    breakdown = combine_result["breakdown"]
    picks = []
    for sym, score in rankings:
        if len(picks) >= n:
            break
        if score < min_score:
            continue
        picks.append({
            "symbol": sym,
            "score": score,
            "breakdown": breakdown.get(sym, {}),
        })
    return picks


def _spearman(x: list, y: list) -> float:
    """Spearman 秩相关系数"""
    n = len(x)
    if n < 3:
        return 0.0
    # 排名
    def rank(vals):
        sorted_vals = sorted(enumerate(vals), key=lambda kv: kv[1])
        ranks = [0] * len(vals)
        for i, (idx, _) in enumerate(sorted_vals):
            ranks[idx] = i + 1
        return ranks
    rx = rank(x)
    ry = rank(y)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n**2 - 1))
