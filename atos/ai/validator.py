"""
ATOS PRO v2 — AI 反幻觉验证层
==============================
大模型容易编造数据。交易系统每一分钱都是真的——不允许任何幻觉。

防护机制：
  1. 价格锚定 — AI 说的价格必须在真实数据 ±2% 内
  2. 标的校验 — AI 只能推荐标的池内的股票
  3. 数字合理性 — 仓位不能 >100%, 价格不能 <=0
  4. JSON 结构校验 — 输出格式不对直接拒绝
  5. 置信度下限 — conf<0.5 的决策不执行
  6. 事实锁定 — 提示词禁止使用训练数据
  7. 熔断机制 — 连续 3 次验证失败 → 暂停 AI 决策
"""

import json
import re
from atos.core.logging import get_logger, log_risk

logger = get_logger("ai.validator")

# 熔断状态
_circuit_breaker = {"failures": 0, "max_failures": 3, "open": False}


def reset_circuit_breaker():
    """重置熔断器"""
    _circuit_breaker["failures"] = 0
    _circuit_breaker["open"] = False


def trip_circuit_breaker():
    """触发熔断"""
    _circuit_breaker["failures"] += 1
    if _circuit_breaker["failures"] >= _circuit_breaker["max_failures"]:
        _circuit_breaker["open"] = True
        logger.critical("⚠️ AI 熔断：连续验证失败超限，暂停 AI 决策，回退到纯风控模式")
        log_risk("CIRCUIT_BREAKER", "AI决策已暂停，仅执行止损/再平衡")


def is_circuit_open() -> bool:
    return _circuit_breaker["open"]


# ─── 1. 价格锚定 ───
def validate_price(ai_price: float, real_price: float, tolerance: float = 0.02) -> dict:
    """
    AI 说的价格必须在真实价格的 ±2% 以内。
    大模型经常编造"看起来合理"的价格——这个检查直接杜绝。
    """
    if real_price <= 0:
        return {"valid": False, "reason": f"真实价格无效: {real_price}"}

    deviation = abs(ai_price - real_price) / real_price
    if deviation > tolerance:
        return {
            "valid": False,
            "reason": f"价格偏差 {deviation:.1%} > {tolerance:.0%}：AI={ai_price:.2f} vs 实际={real_price:.2f}",
        }
    return {"valid": True}


# ─── 2. 标的校验 ───
def validate_symbol(symbol: str, allowed_universe: set[str]) -> dict:
    """AI 只能交易标的池内的股票"""
    if not symbol or not isinstance(symbol, str):
        return {"valid": False, "reason": "标的为空或格式错误"}

    if symbol.upper() not in allowed_universe:
        return {
            "valid": False,
            "reason": f"{symbol} 不在标的池 (共{len(allowed_universe)}只)。AI 可能幻觉了一个不存在的标的。",
        }
    return {"valid": True}


# ─── 3. 数字合理性 ───
def validate_numbers(action: dict) -> dict:
    """检查交易指令的数字是否合理"""
    target_pct = action.get("target_pct", 0)
    confidence = action.get("confidence", 0)

    checks = []

    if target_pct < 0:
        checks.append(f"仓位比例负数: {target_pct}")
    if target_pct > 1.0:
        checks.append(f"仓位比例 > 100%: {target_pct}")
    if confidence < 0 or confidence > 1.0:
        checks.append(f"置信度超出 [0,1]: {confidence}")
    if action.get("action") not in ("BUY", "SELL", "HOLD"):
        checks.append(f"未知操作: {action.get('action')}")

    if checks:
        return {"valid": False, "reason": "; ".join(checks)}
    return {"valid": True}


# ─── 4. JSON 结构校验 ───
REQUIRED_KEYS = {"action", "symbol", "target_pct", "confidence", "reason"}

def validate_json_structure(actions: list) -> dict:
    """检查 AI 返回的 JSON 结构是否完整"""
    if not isinstance(actions, list):
        return {"valid": False, "reason": "actions 不是列表"}

    errors = []
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"第{i}项不是字典")
            continue
        missing = REQUIRED_KEYS - set(action.keys())
        if missing:
            errors.append(f"第{i}项 ({action.get('symbol','?')}) 缺少字段: {missing}")

    if errors:
        return {"valid": False, "reason": "; ".join(errors)}
    return {"valid": True}


# ─── 5. 综合验证 ───
def validate_ai_output(advice: dict, real_prices: dict[str, float],
                        allowed_symbols: set[str]) -> dict:
    """
    对 AI 输出做完整验证。
    返回:
      {"passed": True/False,
       "safe_actions": [...],      # 通过验证的
       "rejected": [...],          # 被拒绝的
       "circuit_open": bool}
    """
    if is_circuit_open():
        return {
            "passed": False,
            "safe_actions": [],
            "rejected": [],
            "circuit_open": True,
            "reason": "熔断已触发—AI决策暂停",
        }

    all_actions = (
        advice.get("short_term_actions", []) +
        advice.get("long_term_actions", [])
    )

    # 结构验证
    struct = validate_json_structure(all_actions)
    if not struct["valid"]:
        logger.error(f"AI幻觉-JSON结构: {struct['reason']}")
        trip_circuit_breaker()
        return {"passed": False, "safe_actions": [], "rejected": all_actions,
                "circuit_open": is_circuit_open(), "reason": struct["reason"]}

    safe = []
    rejected = []

    for action in all_actions:
        if action.get("action") == "HOLD":
            safe.append(action)
            continue

        reasons = []

        # 标的检查
        sym_result = validate_symbol(action.get("symbol", ""), allowed_symbols)
        if not sym_result["valid"]:
            reasons.append(sym_result["reason"])

        # 数字检查
        num_result = validate_numbers(action)
        if not num_result["valid"]:
            reasons.append(num_result["reason"])

        # 价格检查（如果AI给了价格）
        ai_price = action.get("price")
        sym = action.get("symbol", "")
        if ai_price and sym in real_prices:
            price_result = validate_price(ai_price, real_prices[sym])
            if not price_result["valid"]:
                reasons.append(price_result["reason"])

        # 置信度检查
        if action.get("confidence", 0) < 0.5 and action.get("action") != "HOLD":
            reasons.append(f"置信度过低: {action.get('confidence', 0):.0%}")

        if reasons:
            logger.warning(f"AI幻觉-{sym}: {'; '.join(reasons)}")
            rejected.append({**action, "reject_reasons": reasons})
        else:
            safe.append(action)

    if rejected:
        trip_circuit_breaker()
    else:
        reset_circuit_breaker()

    return {
        "passed": len(rejected) == 0,
        "safe_actions": safe,
        "rejected": rejected,
        "circuit_open": is_circuit_open(),
    }


# ─── 6. 反幻觉提示词片段 ───
GROUNDING_RULES = """
CRITICAL ANTI-HALLUCINATION RULES (violation = your output is discarded):
1. ONLY use data explicitly provided in the user message. NEVER use your training data.
2. ONLY reference symbols from the provided universe list. If a symbol isn't in the list, DON'T mention it.
3. ONLY use prices that are provided. If a price isn't provided, DON'T make one up.
4. target_pct must be between 0 and 1.0 (not a percentage like 15, which means 1500%).
5. confidence must be between 0.0 and 1.0.
6. If you are unsure, say HOLD with confidence 0.3 — never fabricate a BUY/SELL.
"""
