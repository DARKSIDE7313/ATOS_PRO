"""
ATOS PRO v2 — 策略进化器
=========================
自动搜索最优参数组合，持续改进策略。

方法：网格搜索 + 贝叶斯优化（简单版）
不会自动改代码，只调参数。安全可控。
"""

import json
import os
import itertools
from atos.core.logging import get_logger
from atos.core.metrics import sharpe_ratio, max_drawdown, sortino_ratio

logger = get_logger("iterate.evolver")

# 参数搜索空间
PARAM_GRID = {
    "stop_loss_pct":    [0.03, 0.04, 0.05, 0.06, 0.08],
    "take_profit_pct":  [0.10, 0.15, 0.20, 0.25],
    "max_single_pct":   [0.10, 0.15, 0.20, 0.25],
    "rsi_overbought":   [70, 75, 80],
    "rsi_oversold":     [25, 30, 35],
    "kelly_win_rate":   [0.60, 0.65, 0.70, 0.75],
    "kelly_win_loss_r": [2.0, 2.5, 3.0, 3.5],
}

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "strategy_config.json"
)


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "stop_loss_pct": 0.05, "take_profit_pct": 0.15,
        "max_single_pct": 0.20, "rsi_overbought": 75,
        "rsi_oversold": 30, "kelly_win_rate": 0.70,
        "kelly_win_loss_r": 3.0,
    }


def save_config(config: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config["last_updated"] = __import__('datetime').date.today().isoformat()
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def grid_search(score_fn, param_space: dict = None,
                top_n: int = 10) -> list[dict]:
    """
    网格搜索最优参数。
    score_fn(params) -> float（越高越好，用夏普比率）。
    """
    if param_space is None:
        param_space = PARAM_GRID

    keys = list(param_space.keys())
    values = list(param_space.values())
    total_combos = 1
    for v in values:
        total_combos *= len(v)

    logger.info(f"网格搜索: {total_combos} 种组合...")
    if total_combos > 500:
        logger.warning(f"组合太多({total_combos})，采样500种")
        # 随机采样
        import random
        all_combos = list(itertools.product(*values))
        random.shuffle(all_combos)
        combos = all_combos[:500]
    else:
        combos = list(itertools.product(*values))

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            score = score_fn(params)
            results.append({"params": params, "score": round(score, 4)})
        except Exception as e:
            logger.debug(f"组合 {i} 失败: {e}")

        if (i + 1) % 50 == 0:
            logger.info(f"网格搜索进度: {i+1}/{len(combos)}")

    results.sort(key=lambda r: r["score"], reverse=True)
    logger.info(f"最优: {results[0]}")

    return results[:top_n]


def evaluate_params(params: dict, backtest_fn) -> float:
    """
    用回测评估一组参数的得分。
    得分 = 夏普比率 * 0.5 + 索提诺比率 * 0.3 - 最大回撤 * 0.2
    （侧重风险调整收益，惩罚回撤）
    """
    try:
        result = backtest_fn(params)
        sharpe = result.get("sharpe_ratio", 0)
        sortino = result.get("sortino_ratio", 0)
        mdd = result.get("max_drawdown", 0)

        # 综合评分
        score = sharpe * 0.5 + sortino * 0.3 - mdd * 0.2
        return max(-10.0, score)  # 不低于 -10
    except Exception as e:
        logger.error(f"评估失败: {e}")
        return -10.0


def compare_to_current(best_params: dict, current_score: float,
                        best_score: float) -> dict:
    """比较最优参数和当前参数"""
    improvement = (best_score - current_score) / abs(current_score) if current_score != 0 else 0

    return {
        "current_score": round(current_score, 4),
        "best_score": round(best_score, 4),
        "improvement_pct": round(improvement * 100, 1),
        "recommend_update": improvement > 0.05,  # 改善超过5%建议更新
        "best_params": best_params,
        "param_changes": {
            k: {"current": load_config().get(k), "suggested": v}
            for k, v in best_params.items()
            if load_config().get(k) != v
        },
    }


def auto_tune(backtest_fn, dry_run: bool = True) -> dict:
    """
    自动调参主入口。

    参数:
        backtest_fn: 回测函数，接收params返回metrics dict
        dry_run: True=只建议不修改, False=自动应用

    返回: 比较报告
    """
    current_config = load_config()
    current_score = evaluate_params(current_config, backtest_fn)

    logger.info(f"当前参数得分: {current_score:.4f}")

    best = grid_search(lambda p: evaluate_params(p, backtest_fn), top_n=1)
    if not best:
        return {"error": "搜索无结果"}

    best_params = best[0]["params"]
    best_score = best[0]["score"]
    comparison = compare_to_current(best_params, current_score, best_score)

    if comparison["recommend_update"] and not dry_run:
        # 应用最优参数
        new_config = {**current_config, **best_params}
        new_config["adjustment_history"] = current_config.get("adjustment_history", []) + [{
            "date": __import__('datetime').date.today().isoformat(),
            "type": "AUTO_TUNE",
            "old_score": current_score,
            "new_score": best_score,
            "changes": comparison["param_changes"],
        }]
        save_config(new_config)
        logger.info("已自动应用最优参数")

    return comparison
