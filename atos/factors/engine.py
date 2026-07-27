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

# 默认权重 — 基金级校准
# 2026-06-24: 当前市场处于 SPY<MA20 回调期，动量因子普遍失效。
# 调降 momentum（0.32→0.20），调升 value（0.05→0.15）和技术（0.25→0.28）
# MA200 偏离过滤会筛掉过热股票，技术分主要用于识别趋势健康者
DEFAULT_WEIGHTS = {
    "value":      0.18,   # v10: 价值因子权重提高
    "momentum":   0.25,   # v10: 动量权重提回（不要过度惩罚）
    "quality":    0.10,   # 质量权重保持
    "technical":  0.25,   # v10: 技术指标回调正常水平
    "multiframe": 0.08,   # v10: 多时间框架降低
    "mean_rev":   0.14,   # 均值回归保持
}

# IC 历史记录
_ic_history: dict[str, dict] = {}
_ic_lock = __import__('threading').Lock()
_per_factor_ic: dict[str, dict[str, float]] = {}

# 真正的 IC 滑动窗口存储
_ic_history_window: dict[str, list[float]] = {}  # {regime: [ic1, ic2, ...]}

# v5 基金级：动态 IC 加权（专业基金核心）
# 每周期根据最近 N 个 IC 自动调整因子权重
# 2026-06-24 深度审计修复：bootstrap 中性化
#   - IC_WINDOW 从 20 → 12（更快适应真实 IC）
#   - IC_MIN_OBS 从 8 → 4（4 个真实周期后即可生效）
#   - Bootstrap 值中心化在 0.0，含正负混合（避免正偏压）
IC_WINDOW = 10
IC_MIN_OBS = 3          # 从 4 降到 3 — 更快适应真实 IC
DYNAMIC_IC_ALPHA = 0.35  # v10: 从 0.5 降到 0.35 — 更信任固定权重，减少IC波动影响

# ── v17: 因子衰减 (Factor Half-Life) ──
# 旧数据应该衰减权重，新数据权重更高
# 参考: Qlib 的滚动窗口 + 指数衰减，AQR 的半衰期方法
FACTOR_HALF_LIFE = 20     # 因子半衰期（周期数）— 超过此周期数据权重减半
FACTOR_DECAY_RATE = 0.5 ** (1.0 / FACTOR_HALF_LIFE)  # 每周期衰减系数
_factor_age: dict[str, int] = {}  # {symbol: cycles_since_last_update}
_factor_freshness: dict[str, float] = {}  # {symbol: freshness_weight (0-1)}


def _bootstrap_ic_window():
    """基金级：用默认权重预填充 IC 滑动窗口，让动态权重从第一天就生效。
    
    在无实盘交易数据时，假设各因子有轻微正向预测力（IC~0.05）。
    当真实 IC 数据积累到 IC_MIN_OBS 后，bootstrap 值自动被覆盖。
    
    2026-06-24 修复：同时预填充 _ic_history（单值存储），
    避免 adjust_weights_from_ic 在首次调用时因 _ic_history[regime] 为空而短路。
    """
    # 2026-06-25 深度修复：完全中性化 bootstrap
    # 旧值 [0.02, -0.01, 0.03, 0.01] 均值=+0.0125（正偏压→动态权重上调过度）
    # 新值 均值=0.0，完全中性，让真实数据主导方向
    bootstrap_ics = [0.005, -0.005, 0.008, -0.008]
    for regime in REGIME_WEIGHTS.keys():
        if regime not in _ic_history_window:
            _ic_history_window[regime] = list(bootstrap_ics)
        if regime not in _ic_history:
            _ic_history[regime] = {"last_ic": 0.0, "weight_adjustments": {}}
        if regime not in _per_factor_ic:
            _per_factor_ic[regime] = {f: 0.0 for f in DEFAULT_WEIGHTS}
    logger.info(f"[IC Bootstrap] 预填充 {len(REGIME_WEIGHTS)} 个市场状态 (完全中性化)")


# v5: BEAR模式下动量大幅降低、质量大幅提升（真正切换防守）
# HIGH_VOL下降低动量+均值回归，提升趋势+突破（避免高波动抄底）
REGIME_WEIGHTS = {
    # v19: 全面提高质量因子权重 — 6%→20% in BULL_STRONG, 防止追涨垃圾股
    "BULL_STRONG": {"momentum": 0.25, "technical": 0.20, "value": 0.15, "quality": 0.20, "multiframe": 0.10, "mean_rev": 0.10},
    "BULL_WEAK":   {"momentum": 0.20, "technical": 0.22, "value": 0.18, "quality": 0.18, "multiframe": 0.08, "mean_rev": 0.14},
    "HIGH_VOL":    {"momentum": 0.10, "technical": 0.22, "value": 0.15, "quality": 0.20, "multiframe": 0.15, "mean_rev": 0.18},
    "BEAR":        {"momentum": 0.05, "technical": 0.18, "value": 0.18, "quality": 0.32, "multiframe": 0.12, "mean_rev": 0.15},
    "SIDEWAYS":    {"momentum": 0.18, "technical": 0.22, "value": 0.18, "quality": 0.18, "multiframe": 0.10, "mean_rev": 0.14},
    "UNKNOWN":     {"momentum": 0.20, "technical": 0.22, "value": 0.18, "quality": 0.18, "multiframe": 0.10, "mean_rev": 0.12},
}

# 模块加载时自动填充 bootstrap IC（必须在 REGIME_WEIGHTS 定义之后调用）
_bootstrap_ic_window()


def _tech_score(signal: dict) -> float:
    """技术信号评分 v11 — 增加评分区分度。

    v8 的问题: 中性趋势=0, RSI 60-70=0, 再加缩量惩罚后大部分股票得分<0.15, 没有区分度。
    v11: 扩充分数范围, 让好股票得 0.40+, 中性股票 0.12-0.25, 只对极差的扣分。
    """
    if not signal:
        return 0.0

    trend = signal.get("trend", "NEUTRAL")
    rsi = signal.get("rsi", 50)
    vol_r = signal.get("volume_ratio", 1.0)
    bb = signal.get("bollinger", {})
    price = signal.get("price", 0)
    ma50 = signal.get("ma50", price)

    # 🔴 趋势DOWN → 立刻返回最低分（不允许逆势买入）
    if trend == "DOWN":
        return 0.0

    score = 0.0

    # 趋势加分 (v11: 扩大区分)
    trend_map = {"UP": 0.40, "WEAK_UP": 0.25, "NEUTRAL": 0.10, "WEAK_DOWN": -0.05}
    score += trend_map.get(trend, 0.05)

    # RSI (v11: 更细的区分, 减少极端惩罚)
    if 40 <= rsi <= 60:
        score += 0.18       # 最佳买入区间
    elif 30 <= rsi < 40:
        score += 0.14       # 超卖区域（好）
    elif 60 < rsi <= 70:
        score += 0.08       # 中性偏高 → 给正分而非0
    elif 20 <= rsi < 30:
        score += 0.05       # 极端超卖 → 小正分
    elif rsi > 75:
        score -= 0.25       # 🔴 强超买
    elif rsi > 70:
        score -= 0.12       # 🟠 超买 → 从-0.25减到-0.12

    # 放量确认
    if 1.2 <= vol_r <= 3.0:
        score += 0.15       # 放量确认
    elif 0.8 <= vol_r < 1.2:
        score += 0.06       # 正常量
    elif 0.5 <= vol_r < 0.8:
        score += 0.02       # 略缩量, 中性
    elif vol_r < 0.3:
        score -= 0.10       # 严重缩量

    # 价格 vs MA50
    if ma50 > 0 and price > 0:
        dev = (price - ma50) / ma50
        if -0.05 <= dev <= 0.05:
            score += 0.05   # 贴近MA50 → 好买点
        elif dev > 0.12:
            score -= 0.06   # 偏离MA50 12%以上
        elif dev < -0.12:
            score += 0.08   # 低于MA50 → 可能有反弹机会

    # 布林带位置
    pct_b = bb.get("pct_b", 0.5)
    if 0.2 <= pct_b <= 0.8:
        score += 0.06
    elif pct_b < 0.2:
        score += 0.03       # 下轨区域 → 可能是机会
    elif pct_b > 0.9:
        score -= 0.12       # 上轨 — 超买

    return max(-0.3, min(1.0, score))  # 🆕 允许轻微负分


def _mean_rev_score(signal: dict) -> float:
    """均值回归评分 v11 — 修复 RSI 40-65 死区。

    之前的 bug: RSI 在 40-65 的正常区间完全没有分支，rsi_score 永远为 0.0。
    Bollinger %B 在 0.30-0.70 也没有分支。结果大多数正常股票从 mean_rev 得到 0 分。
    v11: 加入正常区间的中性正分，确保所有股票都有贡献。
    """
    if not signal:
        return 0.0

    rsi = signal.get("rsi", 50)
    bb = signal.get("bollinger", {})
    pct_b = bb.get("pct_b", 0.5)

    # RSI 评分 — v11: 补全所有区间
    if rsi < 20:
        rsi_score = 0.40   # 极端超卖 → 强反弹信号
    elif rsi < 30:
        rsi_score = 0.25   # 超卖
    elif rsi < 40:
        rsi_score = 0.12   # 偏超卖
    elif rsi <= 60:
        rsi_score = 0.06   # v11: 正常区间 → 轻微正分
    elif rsi <= 70:
        rsi_score = -0.05  # 偏超买
    elif rsi > 80:
        rsi_score = -0.30  # 极端超买
    elif rsi > 75:
        rsi_score = -0.20
    else:  # 70 < rsi <= 75
        rsi_score = -0.12

    # Bollinger %B 评分 — v11: 补全所有区间
    if pct_b < 0.10:
        bb_score = 0.30   # 布林下轨 → 强反弹
    elif pct_b < 0.20:
        bb_score = 0.18
    elif pct_b < 0.30:
        bb_score = 0.08
    elif pct_b <= 0.70:
        bb_score = 0.04   # v11: 布林中轨 → 轻微正分
    elif pct_b > 0.90:
        bb_score = -0.25  # 布林上轨 → 超买
    elif pct_b > 0.80:
        bb_score = -0.12
    else:  # 0.70 < pct_b <= 0.80
        bb_score = -0.04

    # 取两者中绝对值更大者
    if abs(rsi_score) >= abs(bb_score):
        score = rsi_score
    else:
        score = bb_score

    # 🆕 允许负分（表达做空信号），clamp到 [-0.5, 1.0]
    return max(-0.5, min(1.0, score))


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
        # v5默认分检测 — 从3放宽到4，减少误杀
        near_default_count = sum(1 for s in [v_score, m_score, q_score, f_score] if 0.45 <= s <= 0.55)
        if near_default_count >= 4:
            continue
        smc_score_raw = signals[sym].get("smc_score", {}).get("smc_score", 0.0)

        smc_normalized = smc_score_raw  # SMC 直接从 0 起步，缺失数据得 0 分
        # v7: SMC负分传递惩罚 — 强烈看空(-0.60)比无数据(0.0)更差
        smc_adjusted = smc_normalized  # 保留原始范围 [-0.60, +0.60]

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

        # ── v24: AQR QMJ (Quality Minus Junk) 质量-动量交叉过滤 ──
        # Asness, Frazzini, Pedersen (2019): 高质量+高动量是最强组合
        # 高质量股票获得动量加分，低质量股票的动量信号打折
        if q_score >= 0.6 and m_score >= 0.5:
            total = total * 1.12  # 高质量+高动量 → 增强12%
        elif q_score < 0.3 and m_score >= 0.5:
            total = total * 0.85  # 低质量+高动量 → 惩罚15%（追涨垃圾股）

        # ── v24: AQR BAB (Betting Against Beta) 低波动偏好 ──
        # Frazzini & Pedersen (2014): 低beta股票风险调整后收益更高
        atr_val = signals[sym].get("atr", 0)
        price_val = signals[sym].get("price", 1)
        if price_val > 0 and atr_val > 0:
            atr_pct = atr_val / price_val  # ATR占价格比例 = 波动率代理
            if atr_pct < 0.015:
                total = total * 1.08  # 低波动 → 加分8%
            elif atr_pct > 0.035:
                total = total * 0.90  # 高波动 → 减分10%

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
                  min_score: float = 0.15) -> list[dict]:
    """从综合结果中提取 Top N 推荐标的。v10：阈值 0.15（从 0.20 放宽）。"""
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


# ── v17: 因子衰减 (Factor Decay / Freshness Weight) ──
# 参考: Qlib 滚动窗口 + AQR 因子半衰期方法
# 作用: 陈旧因子数据权重降低，新数据权重高
def apply_factor_decay(symbol: str, base_score: float, cycles_since_update: int = 0) -> float:
    """对因子评分应用时间衰减。

    Args:
        symbol: 标的代码
        base_score: 原始因子评分
        cycles_since_update: 距离上次更新的周期数

    Returns:
        衰减后的评分
    """
    global _factor_age, _factor_freshness

    # 更新年龄
    old_age = _factor_age.get(symbol, 0)
    if cycles_since_update > 0:
        _factor_age[symbol] = cycles_since_update
    else:
        _factor_age[symbol] = old_age + 1

    age = _factor_age[symbol]

    # 指数衰减: weight = e^(-λ * age), λ = ln(2) / half_life
    freshness = math.exp(-math.log(2) / FACTOR_HALF_LIFE * age)
    _factor_freshness[symbol] = freshness

    # 对低质量信号加大衰减
    if base_score < 0.30:
        freshness *= 0.7  # 弱信号衰减更快

    return base_score * freshness


def reset_factor_age(symbol: str):
    """重置因子年龄（新数据到达时调用）"""
    global _factor_age
    _factor_age[symbol] = 0
