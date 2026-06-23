"""
ATOS PRO v2 — 因子合成引擎
==========================
综合价值、动量、质量、技术、多时间框架因子，
根据市场环境动态调整权重，输出最终排名。
支持 IC 分析检验因子有效性。

v5 收益修复:
  - 缺失数据默认分 0.5→0.0 (避免假阳性排名)
  - 基准分 0.5→0.0 (增大信号分化)
  - BEAR 模式权重更激进防守
  - 趋势+反转去冗余 (趋势用MA，反转用RSI+BB)
"""
import math
from typing import Optional
from atos.core.logging import get_logger
from atos.core.universe import ALL_SYMBOLS

logger = get_logger("factors.engine")

# 默认权重
DEFAULT_WEIGHTS = {
    "value":      0.12,   # v6 进攻性：价值因子基础权重提升"
    "momentum":   0.25,
    "quality":    0.05,
    "technical":  0.30,
    "multiframe": 0.15,
    "mean_rev":   0.20,
}

# IC 历史记录
_ic_history: dict[str, dict] = {}
_ic_lock = __import__('threading').Lock()
_per_factor_ic: dict[str, dict[str, float]] = {}

# 真正的 IC 滑动窗口存储
_ic_history_window: dict[str, list[float]] = {}  # {regime: [ic1, ic2, ...]}

# v5 基金级：动态 IC 加权（专业基金核心）
# 每周期根据最近 N 个 IC 自动调整因子权重
IC_WINDOW = 20          # 用最近20个周期的 IC
IC_MIN_OBS = 8          # 至少8个观测才启用动态权重
DYNAMIC_IC_ALPHA = 0.6  # 动态权重占 60%，固定权重占 40%


def _bootstrap_ic_window():
    """基金级：用默认权重预填充 IC 滑动窗口，让动态权重从第一天就生效。
    
    在无实盘交易数据时，假设各因子有轻微正向预测力（IC~0.05）。
    当真实 IC 数据积累到 IC_MIN_OBS 后，bootstrap 值自动被覆盖。
    """
    bootstrap_ics = [0.05, 0.04, 0.06, 0.03, 0.05, 0.04, 0.06, 0.05]
    for regime in REGIME_WEIGHTS.keys():
        if regime not in _ic_history_window:
            _ic_history_window[regime] = list(bootstrap_ics)
    logger.info(f"[IC Bootstrap] 已预填充 {len(REGIME_WEIGHTS)} 个市场状态的 IC 窗口")


# 模块加载时自动填充 bootstrap IC
_bootstrap_ic_window()

# v5: BEAR模式下动量大幅降低、质量大幅提升（真正切换防守）
# HIGH_VOL下降低动量+均值回归，提升趋势+突破（避免高波动抄底）
REGIME_WEIGHTS = {
    "BULL_STRONG": {"momentum": 0.32, "technical": 0.25, "value": 0.05, "quality": 0.05, "multiframe": 0.18, "mean_rev": 0.15},
    "BULL_WEAK":   {"momentum": 0.22, "technical": 0.28, "value": 0.05, "quality": 0.08, "multiframe": 0.18, "mean_rev": 0.19},
    "HIGH_VOL":    {"momentum": 0.12, "technical": 0.22, "value": 0.12, "quality": 0.15, "multiframe": 0.20, "mean_rev": 0.19},
    "BEAR":        {"momentum": 0.06, "technical": 0.18, "value": 0.15, "quality": 0.30, "multiframe": 0.18, "mean_rev": 0.13},
    "SIDEWAYS":    {"momentum": 0.18, "technical": 0.26, "value": 0.10, "quality": 0.10, "multiframe": 0.16, "mean_rev": 0.20},
    "UNKNOWN":     {"momentum": 0.22, "technical": 0.28, "value": 0.10, "quality": 0.10, "multiframe": 0.15, "mean_rev": 0.15},
}


def _tech_score(signal: dict) -> float:
    """将技术信号转为 0-1 得分。v5: 从 0.0 起步，纯增量评分。"""
    if not signal:
        return 0.0
    score = 0.0
    trend = signal.get("trend", "NEUTRAL")
    rsi = signal.get("rsi", 50)
    vol_r = signal.get("volume_ratio", 1.0)
    bb = signal.get("bollinger", {})

    # 趋势加分 — v5: 不与 mean_rev 重叠（mean_rev 只负责 RSI+BB）
    trend_map = {"UP": 0.25, "WEAK_UP": 0.15, "NEUTRAL": 0.0, "WEAK_DOWN": -0.15, "DOWN": -0.25}
    score += trend_map.get(trend, 0)

    # RSI: 40-70 健康
    if 40 <= rsi <= 70:
        score += 0.10
    elif rsi < 30:
        score -= 0.10
    elif rsi > 80:
        score -= 0.15

    # 放量加分
    if 1.2 <= vol_r <= 3.0:
        score += 0.10
    elif vol_r < 0.5:
        score -= 0.10

    # 布林带中位健康
    pct_b = bb.get("pct_b", 0.5)
    if 0.2 <= pct_b <= 0.8:
        score += 0.05
    elif pct_b < 0.1 or pct_b > 0.9:
        score -= 0.10

    return max(0.0, min(1.0, score))


def _mean_rev_score(signal: dict) -> float:
    """均值回归评分 — v5: 只用 RSI + BB，不再重复评判趋势（趋势由 _tech_score 负责）。"""
    if not signal:
        return 0.0

    rsi = signal.get("rsi", 50)
    bb = signal.get("bollinger", {})
    pct_b = bb.get("pct_b", 0.5)

    rsi_score = 0.0  # 计算 RSI 部分的总分
    bb_score = 0.0   # 计算 BB 部分的总分

    # RSI 均值回归信号
    if rsi < 25:
        rsi_score = 0.35
    elif rsi < 30:
        rsi_score = 0.25
    elif rsi < 35:
        rsi_score = 0.15
    elif rsi < 40:
        rsi_score = 0.08
    elif rsi > 75:
        rsi_score = -0.25
    elif rsi > 70:
        rsi_score = -0.15
    elif rsi > 65:
        rsi_score = -0.08

    # Bollinger %B 位置
    if pct_b < 0.10:
        bb_score = 0.25
    elif pct_b < 0.20:
        bb_score = 0.15
    elif pct_b < 0.30:
        bb_score = 0.08
    elif pct_b > 0.90:
        bb_score = -0.25
    elif pct_b > 0.80:
        bb_score = -0.15
    elif pct_b > 0.70:
        bb_score = -0.08

    # 核心创新: RSI 和 BB 信号取最强者（不叠加）
    if abs(rsi_score) >= abs(bb_score):
        score = rsi_score
    else:
        score = bb_score

    return max(0.0, min(1.0, score))


def adjust_weights_from_ic(regime: str) -> dict:
    """根据历史 IC 滑动窗口调整因子权重。基金级实现。"""
    base_weights = dict(REGIME_WEIGHTS.get(regime, DEFAULT_WEIGHTS))

    with _ic_lock:
        history = _ic_history.get(regime)
        if not history:
            return base_weights
        last_ic = history.get("last_ic", 0.0)
        per_factor = dict(_per_factor_ic.get(regime, {}))

    # 更新滑动窗口
    if regime not in _ic_history_window:
        _ic_history_window[regime] = []
    _ic_history_window[regime].append(last_ic)
    if len(_ic_history_window[regime]) > IC_WINDOW:
        _ic_history_window[regime] = _ic_history_window[regime][-IC_WINDOW:]

    window = _ic_history_window[regime]

    # 如果窗口数据不足，直接返回基础权重
    if len(window) < IC_MIN_OBS:
        return base_weights

    # 用窗口 median IC 评估整体因子表现
    window_ic = sorted(window)[len(window) // 2]  # median

    # 计算动态权重（基于 per_factor IC 的滑动均值）
    dynamic_weights = dict(base_weights)

    if per_factor and len(window) >= 5:
        total_ic_abs = sum(abs(v) for v in per_factor.values())
        if total_ic_abs > 0:
            for factor, ic_val in per_factor.items():
                if factor in dynamic_weights:
                    # IC > 0: 放大（最高 +25%），IC < 0: 惩罚（最低保留 30%）
                    if ic_val > 0.02:
                        boost = 1.0 + min(ic_val * 2.0, 0.25)
                        dynamic_weights[factor] = base_weights[factor] * boost
                    elif ic_val < -0.02:
                        penalty = max(0.30, 1.0 - abs(ic_val) * 3.0)
                        dynamic_weights[factor] = base_weights[factor] * penalty

    # 归一化动态权重
    dyn_total = sum(dynamic_weights.values())
    if dyn_total > 0:
        dynamic_weights = {k: v / dyn_total for k, v in dynamic_weights.items()}

    # DYNAMIC_IC_ALPHA 混合: 60% 动态 + 40% 固定
    final_weights = {}
    for factor in base_weights:
        final_weights[factor] = (
            DYNAMIC_IC_ALPHA * dynamic_weights.get(factor, base_weights[factor]) +
            (1 - DYNAMIC_IC_ALPHA) * base_weights[factor]
        )

    # 最终归一化
    final_total = sum(final_weights.values())
    if final_total > 0:
        final_weights = {k: round(v / final_total, 4) for k, v in final_weights.items()}

    if abs(window_ic) > 0.03:
        best_f = max(final_weights, key=final_weights.get)
        logger.info(f"IC窗口 median={window_ic:.4f} n={len(window)}: 动态权重已生效, top={best_f}={final_weights[best_f]:.3f}")

    return final_weights


def combine(signals: dict, value_factors: dict, momentum_factors: dict,
            quality_factors: dict, regime: str = "UNKNOWN",
            multiframe_factors: Optional[dict] = None,
            use_v3_signals: bool = False) -> dict:
    """
    综合所有因子，输出每只标的的综合评分。

    v5 修复:
      - 缺失因子默认 0.0 (而非 0.5)，避免无数据标的假性高分
      - multiframe 因子由 composite_signal 按实际字段计算
    """
    weights = adjust_weights_from_ic(regime)
    scores = {}
    breakdown = {}

    if multiframe_factors is None:
        multiframe_factors = {}

    # 如果启用 v3 信号，从 signal_engine 数据中实时计算（修复字段名）
    if use_v3_signals:
        try:
            from atos.live.strategy_v3 import composite_signal
            v3_factors = {}
            for sym in list(signals.keys()):
                result = composite_signal(sym, regime, signals[sym])
                v3_factors[sym] = {
                    "composite": result["score"],
                    "decision": result["decision"],
                    "reasons": result.get("reasons", []),
                }
            multiframe_factors = v3_factors
            logger.info(f"v3多时间框架信号计算完成: {len(v3_factors)} 只标的")
        except Exception as e:
            logger.warning(f"v3信号计算失败: {e}")

    for sym in ALL_SYMBOLS:
        if sym not in signals:
            continue

        # v5: 缺失数据默认 0.0 (而非 0.5) — 避免无数据标的假性高分
        v_score = value_factors.get(sym, {}).get("composite", 0.0)
        m_score = momentum_factors.get(sym, {}).get("composite", 0.0)
        q_score = quality_factors.get(sym, {}).get("composite", 0.0)
        t_score = _tech_score(signals[sym])
        f_score = multiframe_factors.get(sym, {}).get("composite", 0.0)
        r_score = _mean_rev_score(signals[sym])
        smc_score_raw = signals[sym].get("smc_score", {}).get("smc_score", 0.0)

        smc_normalized = smc_score_raw  # SMC 直接从 0 起步，缺失数据得 0 分
        smc_normalized = max(0.0, min(1.0, smc_normalized))

        core_weights = {k: weights.get(k, 0) for k in ["value", "momentum", "quality", "technical", "multiframe", "mean_rev"]}
        core_sum = sum(core_weights.values())
        if core_sum > 0:
            total = (
                v_score * core_weights["value"] / core_sum +
                m_score * core_weights["momentum"] / core_sum +
                q_score * core_weights["quality"] / core_sum +
                t_score * core_weights["technical"] / core_sum +
                f_score * core_weights["multiframe"] / core_sum +
                r_score * core_weights["mean_rev"] / core_sum
            )
        else:
            total = 0.0

        total = total * 0.95 + smc_normalized * 0.05
        total = max(0.0, min(1.0, total))

        scores[sym] = round(total, 4)
        breakdown[sym] = {
            "value": round(v_score, 3),
            "momentum": round(m_score, 3),
            "quality": round(q_score, 3),
            "technical": round(t_score, 3),
            "multiframe": round(f_score, 3),
            "mean_rev": round(r_score, 3),
            "total": round(total, 3),
        }

    rankings = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    logger.info(
        f"因子合成完成: {len(scores)} 只 | "
        f"Top3: {rankings[:3]} | 权重={weights}"
    )

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
    """信息系数分析。"""
    common = set(prev_scores.keys()) & set(current_returns.keys())
    if len(common) < 10:
        result = {"ic": 0.0, "n": len(common), "verdict": "数据不足"}
        _ic_history.setdefault(regime, {})["last_ic"] = 0.0
        return result

    scores_list = []
    returns_list = []
    for sym in common:
        scores_list.append(prev_scores[sym])
        returns_list.append(current_returns[sym])

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

    with _ic_lock:
        _ic_history.setdefault(regime, {})["last_ic"] = result["ic"]

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
            logger.info(f"每个因子 IC: {per_factor_ics} | 最佳={best_f}={per_factor_ics[best_f]:.4f}")

    return result


def get_top_picks(combine_result: dict, n: int = 10,
                  min_score: float = 0.20) -> list[dict]:
    """从综合结果中提取 Top N 推荐标的。v7 基金级校准：阈值 0.20（匹配新评分体系）。"""
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
