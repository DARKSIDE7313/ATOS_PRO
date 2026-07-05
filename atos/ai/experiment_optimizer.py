"""
ATOS PRO v5 — AI 实验优化器（移植自 QuantDinger）
==================================================
核心创新：多轮 LLM 驱动的策略参数优化
  - 第1轮: LLM 提出 N 组参数 → 回测 → 评分
  - 第2轮: LLM 学习上一轮结果 → 提出改进参数 → 回测 → 评分
  - 第3轮: 持续优化直到收敛或达到目标分数

QuantDinger 参考:
  - experiment/runner.py: 多轮 AI pipeline + OOS 验证
  - experiment/prompts.py: LLM 提示词构建
  - experiment/scoring.py: 体制感知评分

使用方法:
  from atos.ai.experiment_optimizer import optimize_strategy
  result = optimize_strategy(strategy_config, market_data)
"""

import json
import os
import math
import copy
import time
import datetime
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

from atos.core.logging import get_logger

logger = get_logger("ai.experiment")


# ════════════════════════════════════════════════════════════
# 1. 体制感知评分系统（移植自 QuantDinger scoring.py）
# ════════════════════════════════════════════════════════════

# 默认权重
DEFAULT_SCORE_WEIGHTS = {
    "return": 0.22,        # 总收益
    "annual_return": 0.12, # 年化收益
    "sharpe": 0.18,        # 夏普比率
    "profit_factor": 0.14, # 盈亏比
    "win_rate": 0.09,      # 胜率
    "drawdown": 0.15,      # 回撤（反向）
    "stability": 0.10,     # 稳定性（权益曲线单调性）
}

# 体制特定权重（移植自 QuantDinger REGIME_WEIGHTS）
REGIME_SCORE_WEIGHTS = {
    "bull_trend": {
        "return": 0.30, "annual_return": 0.18, "sharpe": 0.16,
        "profit_factor": 0.10, "win_rate": 0.06, "drawdown": 0.12, "stability": 0.08,
    },
    "bear_trend": {
        "return": 0.16, "annual_return": 0.10, "sharpe": 0.20,
        "profit_factor": 0.16, "win_rate": 0.06, "drawdown": 0.22, "stability": 0.10,
    },
    "range_compression": {
        "return": 0.10, "annual_return": 0.06, "sharpe": 0.14,
        "profit_factor": 0.18, "win_rate": 0.20, "drawdown": 0.12, "stability": 0.20,
    },
    "high_volatility": {
        "return": 0.14, "annual_return": 0.08, "sharpe": 0.16,
        "profit_factor": 0.18, "win_rate": 0.06, "drawdown": 0.26, "stability": 0.12,
    },
}

# 映射 ATOS 体制名到 QuantDinger 体制名
REGIME_MAP = {
    "BULL": "bull_trend",
    "BULL_STRONG": "bull_trend",
    "BEAR": "bear_trend",
    "HIGH_VOL": "high_volatility",
    "SIDEWAYS": "range_compression",
    "NEUTRAL": "range_compression",
}


def _bounded_score(value: float, floor: float, ceiling: float) -> float:
    """将值映射到 0-100 分数"""
    if ceiling <= floor:
        return 50.0
    ratio = (value - floor) / (ceiling - floor)
    return max(0.0, min(100.0, ratio * 100.0))


def _inverse_score(value: float, floor: float, ceiling: float) -> float:
    """反向映射（越高越差，如回撤）"""
    if ceiling <= floor:
        return 50.0
    ratio = (value - floor) / (ceiling - floor)
    return max(0.0, min(100.0, (1.0 - ratio) * 100.0))


def _stability_score(equity_curve: List[float]) -> float:
    """计算权益曲线稳定性（单调性）"""
    if len(equity_curve) < 3:
        return 45.0
    positive_steps = sum(1 for prev, curr in zip(equity_curve, equity_curve[1:]) if curr >= prev)
    total_steps = max(len(equity_curve) - 1, 1)
    monotonicity = positive_steps / total_steps
    return max(0.0, min(100.0, monotonicity * 100.0))


def _score_grade(score: float) -> str:
    """分数 → 等级"""
    if score >= 85:
        return "A"
    if score >= 72:
        return "B"
    if score >= 60:
        return "C"
    if score >= 45:
        return "D"
    return "E"


def score_backtest_result(result: Dict[str, Any], regime: str = None) -> Dict[str, Any]:
    """对回测结果进行多维度评分（移植自 QuantDinger score_result）

    参数:
        result: 回测结果字典，含 totalReturn, maxDrawdown, sharpeRatio, profitFactor, winRate, totalTrades
        regime: 市场体制（用于选择权重）

    返回:
        {"overallScore": 0-100, "grade": "A-F", "components": {...}, "weights": {...}}
    """
    result = result or {}
    total_return = float(result.get("totalReturn", 0) or 0)
    annual_return = float(result.get("annualReturn", 0) or 0)
    max_drawdown = abs(float(result.get("maxDrawdown", 0) or 0))
    sharpe = float(result.get("sharpeRatio", 0) or 0)
    profit_factor = float(result.get("profitFactor", 0) or 0)
    win_rate = float(result.get("winRate", 0) or 0)
    total_trades = int(float(result.get("totalTrades", 0) or 0))

    # 各维度评分
    components = {
        "returnScore": _bounded_score(total_return, floor=-20.0, ceiling=80.0),
        "annualReturnScore": _bounded_score(annual_return, floor=-20.0, ceiling=120.0),
        "sharpeScore": _bounded_score(sharpe, floor=-1.0, ceiling=3.0),
        "profitFactorScore": _bounded_score(profit_factor, floor=0.7, ceiling=2.5),
        "winRateScore": _bounded_score(win_rate, floor=35.0, ceiling=70.0),
        "drawdownScore": _inverse_score(max_drawdown, floor=5.0, ceiling=45.0),
        "stabilityScore": _stability_score(result.get("equityCurve", [])),
        "sampleSizeScore": _bounded_score(total_trades, floor=5.0, ceiling=80.0),
    }

    # 体制感知权重
    weights = _resolve_weights(regime)

    # 加权总分
    weighted = (
        components["returnScore"] * weights["return"]
        + components["annualReturnScore"] * weights["annual_return"]
        + components["sharpeScore"] * weights["sharpe"]
        + components["profitFactorScore"] * weights["profit_factor"]
        + components["winRateScore"] * weights["win_rate"]
        + components["drawdownScore"] * weights["drawdown"]
        + components["stabilityScore"] * weights["stability"]
    )

    # 样本量惩罚
    if total_trades < 5:
        weighted -= 12.0
    elif total_trades < 12:
        weighted -= 5.0

    overall = max(0.0, min(100.0, weighted))

    return {
        "overallScore": round(overall, 2),
        "grade": _score_grade(overall),
        "components": {k: round(v, 2) for k, v in components.items()},
        "summary": {
            "totalTrades": total_trades,
            "riskAdjustedReturn": round((components["sharpeScore"] + components["drawdownScore"]) / 2.0, 2),
            "consistency": round((components["stabilityScore"] + components["winRateScore"]) / 2.0, 2),
        },
        "weights": weights,
    }


def _resolve_weights(regime: str = None) -> Dict[str, float]:
    """根据体制解析权重"""
    regime_key = REGIME_MAP.get(regime, "range_compression") if regime else "range_compression"
    weights = REGIME_SCORE_WEIGHTS.get(regime_key, DEFAULT_SCORE_WEIGHTS)
    # 归一化
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 4) for k, v in weights.items()}


# ════════════════════════════════════════════════════════════
# 2. AI 多轮优化引擎（移植自 QuantDinger experiment/runner.py）
# ════════════════════════════════════════════════════════════

OPTIMIZER_SYSTEM_PROMPT = """你是量化交易策略优化专家。你的任务是为给定的交易策略提出参数组合进行回测优化。

你必须返回 JSON 数组，每个元素是一个候选参数组合。不要输出解释，不要输出 markdown。"""

OPTIMIZER_ROUND_TEMPLATE = """## 策略配置
{strategy_config}

## 可调参数
{param_description}

## 当前市场体制
{regime_info}

## 上一轮结果
{previous_results}

## 任务
提出 {n_candidates} 组不同的参数组合。每组必须包含完整参数。
{learning_instruction}

返回 JSON 数组：
[
  {{
    "name": "简短描述名",
    "reasoning": "为什么这组参数应该表现更好（1句话）",
    "params": {{ ... 所有可调参数 ... }}
  }}
]"""


def _call_llm(prompt: str, temperature: float = 0.7) -> str:
    """调用 DeepSeek API"""
    from atos.ai.engine_v5 import _call_deepseek
    return _call_deepseek(OPTIMIZER_SYSTEM_PROMPT, prompt, temperature=temperature, timeout=90)


def _extract_json(text: str) -> List[Dict]:
    """从 LLM 输出提取 JSON 数组"""
    from atos.ai.engine_v5 import _extract_json as engine_extract_json
    text = text.strip()
    # 尝试直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "candidates" in parsed:
            return parsed["candidates"]
    except json.JSONDecodeError:
        pass
    # 尝试提取 JSON 数组
    import re
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    logger.warning(f"无法解析 LLM 输出: {text[:200]}")
    return []


@dataclass
class OptimizationRound:
    """一轮优化的结果"""
    round_num: int
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    best_score: float = 0.0
    best_candidate: Optional[Dict[str, Any]] = None
    elapsed_seconds: float = 0.0
    error: Optional[str] = None


def optimize_strategy(
    *,
    strategy_config: Dict[str, Any],
    param_space: Dict[str, Any],
    backtest_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    regime: str = None,
    max_rounds: int = 3,
    candidates_per_round: int = 5,
    early_stop_score: float = 82.0,
) -> Dict[str, Any]:
    """AI 驱动的多轮策略参数优化。

    这是 QuantDinger 最核心的功能 —— AI 自主寻找最优策略参数。

    参数:
        strategy_config: 策略基础配置（指标代码、参数等）
        param_space: 可调参数空间 {"param_name": {"min": 0, "max": 100, "step": 5}, ...}
        backtest_fn: 回测函数，接受参数dict，返回结果dict
        regime: 市场体制
        max_rounds: 最大轮数
        candidates_per_round: 每轮候选数
        early_stop_score: 早停分数（达到此分数停止优化）

    返回:
        {
            "rounds": [...],
            "best_strategy": {...},
            "best_score": float,
            "total_rounds": int,
            "total_candidates": int,
            "convergence": [...],
        }
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("无 DeepSeek API — 使用随机搜索降级")
        return _random_search_fallback(strategy_config, param_space, backtest_fn, regime)

    rounds: List[OptimizationRound] = []
    global_best: Optional[Dict[str, Any]] = None
    global_best_score = -1.0
    previous_ranked: Optional[List[Dict[str, Any]]] = None

    for round_num in range(1, max_rounds + 1):
        round_start = time.time()
        logger.info(f"🧪 优化第 {round_num}/{max_rounds} 轮...")

        # 1. 构建提示词
        if previous_ranked:
            learning = (
                "仔细分析上一轮结果。找出高分参数的规律，低分参数的问题。"
                "在保持探索多样性的同时，集中优化有前景的方向。"
            )
            prev_text = "\n".join(
                f"- {r.get('name','?')}: 得分={r.get('score',{}).get('overallScore',0):.1f} "
                f"收益={r.get('result',{}).get('totalReturn',0):.1f}% "
                f"回撤={r.get('result',{}).get('maxDrawdown',0):.1f}% "
                f"夏普={r.get('result',{}).get('sharpeRatio',0):.2f}"
                for r in previous_ranked
            )
        else:
            learning = (
                "这是第一轮，请提出多样化的参数组合："
                "一些保守（紧止损、小仓位），一些适中，一些激进。"
            )
            prev_text = "这是第1轮 — 无历史结果。"

        param_desc = _describe_param_space(param_space)
        regime_info = f"市场体制: {regime or '未知'}" + (
            f" ({'牛市偏收益和夏普' if regime in ('BULL','BULL_STRONG') else '熊市重回撤和盈亏比' if regime in ('BEAR',) else '震荡市重胜率和稳定性'})"
        )

        prompt = OPTIMIZER_ROUND_TEMPLATE.format(
            strategy_config=json.dumps(strategy_config, ensure_ascii=False)[:3000],
            param_description=param_desc,
            regime_info=regime_info,
            previous_results=prev_text,
            n_candidates=candidates_per_round,
            learning_instruction=learning,
        )

        # 2. 调用 LLM 获取候选参数
        try:
            raw = _call_llm(prompt, temperature=0.7 + round_num * 0.05)
            candidates = _extract_json(raw)
        except Exception as e:
            logger.error(f"LLM 调用失败 (第{round_num}轮): {e}")
            rounds.append(OptimizationRound(round_num=round_num, error=str(e)))
            continue

        if not candidates:
            logger.warning(f"第{round_num}轮 LLM 未返回有效候选")
            continue

        # 3. 回测每个候选
        round_ranked = []
        for idx, cand in enumerate(candidates[:candidates_per_round], start=1):
            params = cand.get("params", {})
            name = cand.get("name", f"R{round_num}_C{idx}")
            reasoning = cand.get("reasoning", "")

            # 合并参数到策略配置
            test_config = copy.deepcopy(strategy_config)
            _merge_params(test_config, params)

            try:
                bt_result = backtest_fn(test_config)
            except Exception as e:
                logger.warning(f"回测失败 {name}: {e}")
                continue

            # 评分
            score = score_backtest_result(bt_result, regime=regime)
            round_ranked.append({
                "name": name,
                "reasoning": reasoning,
                "params": params,
                "score": score,
                "result": {
                    "totalReturn": bt_result.get("totalReturn", 0),
                    "maxDrawdown": bt_result.get("maxDrawdown", 0),
                    "sharpeRatio": bt_result.get("sharpeRatio", 0),
                    "profitFactor": bt_result.get("profitFactor", 0),
                    "winRate": bt_result.get("winRate", 0),
                    "totalTrades": bt_result.get("totalTrades", 0),
                },
                "config": test_config,
            })

        # 按分数排名
        round_ranked.sort(key=lambda x: x["score"]["overallScore"], reverse=True)
        round_best = round_ranked[0] if round_ranked else None
        round_best_score = round_best["score"]["overallScore"] if round_best else 0.0

        if round_best and round_best_score > global_best_score:
            global_best = round_best
            global_best_score = round_best_score

        elapsed = round(time.time() - round_start, 1)
        rounds.append(OptimizationRound(
            round_num=round_num,
            candidates=round_ranked,
            best_score=round_best_score,
            best_candidate=round_best,
            elapsed_seconds=elapsed,
        ))

        logger.info(f"  第{round_num}轮完成: 最佳={round_best_score:.1f}分 "
                     f"(候选={round_best['name'] if round_best else 'N/A'}), "
                     f"全局最佳={global_best_score:.1f}分, 耗时={elapsed}s")

        previous_ranked = round_ranked

        # 早停
        if global_best_score >= early_stop_score:
            logger.info(f"🎯 早停! 分数 {global_best_score:.1f} >= {early_stop_score}")
            break

    # 汇总所有候选
    all_candidates = []
    for rd in rounds:
        all_candidates.extend(rd.candidates)
    all_candidates.sort(key=lambda x: x["score"]["overallScore"], reverse=True)

    # 收敛曲线
    convergence = []
    best_so_far = -1
    for rd in rounds:
        if rd.best_score > best_so_far:
            best_so_far = rd.best_score
        convergence.append({"round": rd.round_num, "bestScore": rd.best_score, "globalBest": best_so_far})

    return {
        "rounds": [
            {
                "round": rd.round_num,
                "bestScore": rd.best_score,
                "candidateCount": len(rd.candidates),
                "elapsed": rd.elapsed_seconds,
                "error": rd.error,
            }
            for rd in rounds
        ],
        "rankedStrategies": all_candidates[:20],
        "bestStrategy": global_best,
        "bestScore": global_best_score,
        "totalRounds": len(rounds),
        "totalCandidates": len(all_candidates),
        "convergence": convergence,
        "mode": "ai_optimization",
    }


def _describe_param_space(param_space: Dict[str, Any]) -> str:
    """描述参数空间"""
    lines = []
    for name, spec in param_space.items():
        if isinstance(spec, dict):
            lines.append(f"- {name}: 范围 [{spec.get('min')}, {spec.get('max')}], 步长={spec.get('step', 1)}")
        elif isinstance(spec, list):
            lines.append(f"- {name}: 可选值 {spec}")
        else:
            lines.append(f"- {name}: {spec}")
    return "\n".join(lines) if lines else "(无可调参数)"


def _merge_params(config: Dict[str, Any], params: Dict[str, Any]) -> None:
    """合并参数到配置（支持点分隔路径如 'risk.stopLossPct'）"""
    for key, value in params.items():
        parts = key.split(".")
        cursor = config
        for part in parts[:-1]:
            if part not in cursor:
                cursor[part] = {}
            cursor = cursor[part]
        cursor[parts[-1]] = value


def _random_search_fallback(
    strategy_config: Dict[str, Any],
    param_space: Dict[str, Any],
    backtest_fn: Callable,
    regime: str = None,
    n_samples: int = 20,
) -> Dict[str, Any]:
    """无 LLM 时的随机搜索降级方案"""
    import random as rng
    logger.info(f"🎲 使用随机搜索 ({n_samples}次)")

    ranked = []
    for i in range(n_samples):
        test_config = copy.deepcopy(strategy_config)
        params = {}
        for name, spec in param_space.items():
            if isinstance(spec, dict):
                mn, mx, step = spec.get("min", 0), spec.get("max", 100), spec.get("step", 1)
                vals = []
                cursor = mn
                while cursor <= mx:
                    vals.append(round(cursor, 6) if isinstance(cursor, float) else cursor)
                    cursor += step
                params[name] = rng.choice(vals) if vals else mn
            elif isinstance(spec, list):
                params[name] = rng.choice(spec)
            else:
                params[name] = spec
        _merge_params(test_config, params)

        try:
            bt_result = backtest_fn(test_config)
        except Exception:
            continue

        score = score_backtest_result(bt_result, regime=regime)
        ranked.append({
            "name": f"random_{i+1}",
            "reasoning": "随机搜索",
            "params": params,
            "score": score,
            "result": {
                "totalReturn": bt_result.get("totalReturn", 0),
                "maxDrawdown": bt_result.get("maxDrawdown", 0),
                "sharpeRatio": bt_result.get("sharpeRatio", 0),
                "profitFactor": bt_result.get("profitFactor", 0),
                "winRate": bt_result.get("winRate", 0),
                "totalTrades": bt_result.get("totalTrades", 0),
            },
            "config": test_config,
        })

    ranked.sort(key=lambda x: x["score"]["overallScore"], reverse=True)
    return {
        "rounds": [{"round": 1, "bestScore": ranked[0]["score"]["overallScore"] if ranked else 0, "candidateCount": len(ranked), "elapsed": 0}],
        "rankedStrategies": ranked,
        "bestStrategy": ranked[0] if ranked else None,
        "bestScore": ranked[0]["score"]["overallScore"] if ranked else 0,
        "totalRounds": 1,
        "totalCandidates": len(ranked),
        "convergence": [],
        "mode": "random_search",
    }


# ════════════════════════════════════════════════════════════
# 3. AI 自校准（移植自 QuantDinger ai_calibration.py）
# ════════════════════════════════════════════════════════════

@dataclass
class CalibrationResult:
    """校准结果"""
    buy_threshold: float = 20.0    # 分数 >= 此值 → BUY
    sell_threshold: float = -20.0  # 分数 <= 此值 → SELL
    best_accuracy: float = 0.0
    sample_count: int = 0


def calibrate_thresholds(decisions: List[Dict[str, Any]]) -> CalibrationResult:
    """基于历史决策结果，自动校准买入/卖出阈值。

    移植自 QuantDinger AICalibrationService.calibrate_market()

    参数:
        decisions: [{"score": float, "actual_return_pct": float}, ...]

    返回:
        CalibrationResult 包含最优阈值
    """
    if len(decisions) < 10:
        logger.info(f"自校准: 样本不足 ({len(decisions)}<10)，使用默认阈值")
        return CalibrationResult()

    best_accuracy = 0.0
    best_buy = 20.0
    best_sell = -20.0

    # 网格搜索最优阈值
    for buy_thr in range(5, 51, 5):  # 5, 10, 15, ..., 50
        for sell_thr in range(-50, -4, 5):  # -50, -45, ..., -5
            correct = 0
            total = 0
            for d in decisions:
                score = d.get("score", 0)
                ret = d.get("actual_return_pct", 0)

                # 判断规则
                if score >= buy_thr:
                    predicted = "BUY"
                    correct_pred = ret > 2  # BUY正确: 实际涨 > 2%
                elif score <= sell_thr:
                    predicted = "SELL"
                    correct_pred = ret < -2  # SELL正确: 实际跌 > 2%
                else:
                    predicted = "HOLD"
                    correct_pred = abs(ret) <= 5  # HOLD正确: 波动 < 5%

                total += 1
                if correct_pred:
                    correct += 1

            if total > 0:
                accuracy = correct / total
                if accuracy > best_accuracy:
                    best_accuracy = accuracy
                    best_buy = buy_thr
                    best_sell = sell_thr

    result = CalibrationResult(
        buy_threshold=best_buy,
        sell_threshold=best_sell,
        best_accuracy=round(best_accuracy, 3),
        sample_count=len(decisions),
    )
    logger.info(f"🎯 自校准完成: BUY≥{best_buy} SELL≤{best_sell} 准确率={best_accuracy:.1%} (n={len(decisions)})")
    return result


# ════════════════════════════════════════════════════════════
# 4. OOS 验证（移植自 QuantDinger _evaluate_oos）
# ════════════════════════════════════════════════════════════

def oos_validate(
    ranked_strategies: List[Dict[str, Any]],
    backtest_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    oos_start_date: str,
    oos_end_date: str,
    regime: str = None,
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """对排名靠前的策略进行样本外（OOS）验证。

    核心思想（移植自 QuantDinger）:
    - 训练集70%: 用于选参和排名
    - 测试集30%: 验证策略没有过拟合
    - 如果 OOS 分数暴跌 > 40% → 过拟合警告

    参数:
        ranked_strategies: 已排名的策略列表
        backtest_fn: 回测函数(接受含start_date/end_date的config)
        oos_start_date: OOS开始日期
        oos_end_date: OOS结束日期
        regime: 市场体制
        top_k: 验证前K个策略

    返回:
        带 oosScore, oosDegradation, oosOverfit 标注的策略列表
    """
    for i, strategy in enumerate(ranked_strategies[:top_k]):
        name = strategy.get("name", f"strategy_{i}")
        config = strategy.get("config", strategy.get("snapshot", {}))

        # 设置OOS日期
        oos_config = copy.deepcopy(config)
        oos_config["start_date"] = oos_start_date
        oos_config["end_date"] = oos_end_date

        try:
            oos_result = backtest_fn(oos_config)
        except Exception as e:
            logger.warning(f"OOS回测失败 {name}: {e}")
            strategy["oosError"] = str(e)[:100]
            continue

        oos_score = score_backtest_result(oos_result, regime=regime)
        is_score = strategy.get("score", {}).get("overallScore", 0)
        oos_overall = oos_score.get("overallScore", 0)

        # 计算衰减
        degradation = None
        if is_score > 0:
            degradation = round((is_score - oos_overall) / is_score, 4)

        strategy["oosScore"] = oos_score
        strategy["oosResult"] = {
            "totalReturn": oos_result.get("totalReturn", 0),
            "maxDrawdown": oos_result.get("maxDrawdown", 0),
            "sharpeRatio": oos_result.get("sharpeRatio", 0),
            "totalTrades": oos_result.get("totalTrades", 0),
        }
        strategy["oosDegradation"] = degradation
        strategy["oosOverfit"] = bool(degradation is not None and degradation > 0.4)

        if degradation is not None and degradation > 0.4:
            logger.warning(f"⚠️ 过拟合警告: {name} IS={is_score:.1f} OOS={oos_overall:.1f} 衰减={degradation:.0%}")
        else:
            logger.info(f"✅ OOS验证 {name}: IS={is_score:.1f} OOS={oos_overall:.1f}")

    return ranked_strategies


# ════════════════════════════════════════════════════════════
# 5. 便捷入口 — 完整优化流水线
# ════════════════════════════════════════════════════════════

def run_full_optimization(
    *,
    strategy_config: Dict[str, Any],
    param_space: Dict[str, Any],
    backtest_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    regime: str = None,
    train_start: str = None,
    train_end: str = None,
    oos_start: str = None,
    oos_end: str = None,
    use_ai: bool = True,
) -> Dict[str, Any]:
    """完整优化流水线: AI多轮优化 → 评分排名 → OOS验证

    这是 QuantDinger 整个 experiment pipeline 的移植版本。
    """
    # Step 1: AI 优化 / 随机搜索
    opt_result = optimize_strategy(
        strategy_config=strategy_config,
        param_space=param_space,
        backtest_fn=backtest_fn,
        regime=regime,
        max_rounds=3 if use_ai else 1,
    )

    # Step 2: OOS 验证（如果有日期）
    ranked = opt_result.get("rankedStrategies", [])
    if oos_start and oos_end and ranked:
        ranked = oos_validate(
            ranked_strategies=ranked,
            backtest_fn=backtest_fn,
            oos_start_date=oos_start,
            oos_end_date=oos_end,
            regime=regime,
        )
        opt_result["rankedStrategies"] = ranked
        opt_result["oosValidation"] = {
            "enabled": True,
            "oosStart": oos_start,
            "oosEnd": oos_end,
        }

    # Step 3: 生成优化建议
    best = opt_result.get("bestStrategy", {})
    opt_result["recommendation"] = {
        "action": "deploy" if opt_result.get("bestScore", 0) >= 60 else "review",
        "reason": (
            f"最佳策略得分{opt_result.get('bestScore',0):.1f}，"
            f"建议{'部署纸交易' if opt_result.get('bestScore',0) >= 60 else '继续优化参数'}"
        ),
        "best_params": best.get("params", {}),
        "expected_return": (best.get("result", {}) or {}).get("totalReturn", 0),
        "expected_drawdown": (best.get("result", {}) or {}).get("maxDrawdown", 0),
    }

    return opt_result
