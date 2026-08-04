"""
ATOS AI — 多模型路由器
======================
统一管理多个 AI 后端，支持热切换。

支持的模型:
  deepseek-v4    — DeepSeek 直连 API (最快, 免费额度)
  deepseek-v3    — OpenRouter → DeepSeek V3
  deepseek-r1    — OpenRouter → DeepSeek R1 (推理增强)
  llama-3        — OpenRouter → Llama 3.1 (免费)
  kimi-k3        — OpenRouter → Kimi K3 (月之暗面)

用法:
  from atos.ai.model_router import switch_model, get_current_model, ask, analyze_trade
  switch_model("deepseek-v4")  # 切换模型
  answer = ask("今天市场怎么样?")
"""

import json, os, time, urllib.request, threading
from atos.core.logging import get_logger

logger = get_logger("ai.router")

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    ""
)

# 模型注册表
MODELS = {
    "deepseek-v4": {
        "name": "DeepSeek V4 (直连)",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "url": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "cost": "~$0.001/请求",
        "speed": "最快",
    },
    "deepseek-v3": {
        "name": "DeepSeek V3 (OpenRouter)",
        "provider": "openrouter",
        "model": "deepseek/deepseek-chat",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "cost": "~$0.002/请求",
        "speed": "快",
    },
    "deepseek-r1": {
        "name": "DeepSeek R1 (推理增强)",
        "provider": "openrouter",
        "model": "deepseek/deepseek-r1",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "cost": "~$0.005/请求",
        "speed": "较慢",
    },
    "llama-3": {
        "name": "Llama 3.1 8B (免费)",
        "provider": "openrouter",
        "model": "meta-llama/llama-3.1-8b-instruct",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "cost": "免费",
        "speed": "快",
    },
    "kimi-k3": {
        "name": "Kimi K3 (月之暗面)",
        "provider": "openrouter",
        "model": "moonshotai/kimi-k3",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "cost": "~$0.003/请求",
        "speed": "快(5s)",
        "verified": True,
    },
}

# 当前激活的模型
_current_model = "deepseek-v4"
_switch_lock = threading.Lock()
_usage_stats = {}  # {model: {"calls": N, "tokens": N, "errors": N}}


def switch_model(model_id: str) -> dict:
    """切换当前使用的 AI 模型"""
    global _current_model

    if model_id not in MODELS:
        available = ", ".join(MODELS.keys())
        return {"success": False, "message": f"未知模型 '{model_id}'。可用: {available}"}

    with _switch_lock:
        old = _current_model
        _current_model = model_id

    new_model = MODELS[model_id]
    logger.info(f"🔄 AI模型切换: {MODELS[old]['name']} → {new_model['name']}")

    # Quick connectivity test
    test_result = _test_connectivity(model_id)

    return {
        "success": True,
        "previous": old,
        "current": model_id,
        "model_name": new_model["name"],
        "provider": new_model["provider"],
        "cost": new_model["cost"],
        "speed": new_model["speed"],
        "connectivity": test_result,
    }


def get_current_model() -> dict:
    """获取当前模型信息"""
    model = MODELS[_current_model]
    stats = _usage_stats.get(_current_model, {"calls": 0, "tokens": 0, "errors": 0})
    return {
        "id": _current_model,
        "name": model["name"],
        "provider": model["provider"],
        "cost": model["cost"],
        "speed": model["speed"],
        "usage": stats,
        "all_models": list(MODELS.keys()),
    }


def list_models() -> list:
    """列出所有可用模型"""
    result = []
    for mid, info in MODELS.items():
        stats = _usage_stats.get(mid, {"calls": 0, "tokens": 0, "errors": 0})
        active = "⭐" if mid == _current_model else "  "
        result.append({
            "id": mid,
            "name": info["name"],
            "cost": info["cost"],
            "speed": info["speed"],
            "active": mid == _current_model,
            "calls": stats.get("calls", 0),
        })
    return result


def _test_connectivity(model_id: str) -> str:
    """测试模型连通性"""
    try:
        resp = _call_api(model_id, "hi", max_tokens=15, timeout=15)
        if resp and isinstance(resp, str) and len(resp) > 0:
            return "✅ 连通正常"
        return "⚠️ 响应异常"
    except Exception as e:
        return f"❌ {str(e)[:60]}"


def _call_api(model_id: str, prompt: str, system_prompt: str = "",
              temperature: float = 0.3, max_tokens: int = 300,
              timeout: int = 30) -> str:
    """调用 AI API（自动路由到正确后端）"""
    model = MODELS.get(model_id, MODELS["deepseek-v4"])

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    data = json.dumps({
        "model": model["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    headers = {"Content-Type": "application/json"}

    if model["provider"] == "deepseek":
        key = DEEPSEEK_KEY
        headers["Authorization"] = f"Bearer {key}"
    else:  # openrouter
        key = OPENROUTER_KEY
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "https://atos-pro.local"
        headers["X-Title"] = "ATOS PRO"

    t0 = time.time()
    req = urllib.request.Request(model["url"], data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        _record_usage(model_id, 0, error=True)
        raise

    elapsed = time.time() - t0
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    total_tokens = usage.get("total_tokens", usage.get("completion_tokens", 0))

    _record_usage(model_id, total_tokens)
    logger.debug(f"[{model_id}] {len(content)}chars {elapsed:.1f}s {total_tokens}tok")

    return content


def _record_usage(model_id: str, tokens: int, error: bool = False):
    """记录使用统计"""
    if model_id not in _usage_stats:
        _usage_stats[model_id] = {"calls": 0, "tokens": 0, "errors": 0}
    _usage_stats[model_id]["calls"] += 1
    _usage_stats[model_id]["tokens"] += tokens
    if error:
        _usage_stats[model_id]["errors"] += 1


# ═══════════════════════════════════════════
# 高层 API
# ═══════════════════════════════════════════

def ask(question: str, model: str = None) -> str:
    """问 AI 一个问题（使用当前模型）"""
    mid = model or _current_model
    return _call_api(mid, question, max_tokens=300, timeout=30)


def analyze_trade(symbol: str, factor_score: float, rsi: float,
                  trend: str, regime: str, vix: float,
                  model: str = None) -> dict:
    """让 AI 分析单只股票的交易机会"""
    mid = model or _current_model

    prompt = f"""分析交易机会(只输出JSON):

标的: {symbol} | 因子分: {factor_score:.2f} | RSI: {rsi:.0f}
趋势: {trend} | 市场: {regime} | VIX: {vix:.0f}

输出: {{"verdict":"BUY|WAIT|SKIP","confidence":0.0-1.0,"reason":"一句话"}}"""

    resp = _call_api(mid, prompt, system_prompt="你是量化分析师。只输出JSON。",
                     temperature=0.2, max_tokens=120, timeout=20)

    try:
        import re
        match = re.search(r'\{[^}]+\}', resp)
        if match:
            return json.loads(match.group())
    except Exception:
        pass

    return {"verdict": "WAIT", "confidence": 0.4, "reason": "AI解析失败"}


def get_market_read(model: str = None) -> str:
    """让 AI 快速解读当前市场"""
    mid = model or _current_model
    return _call_api(mid,
        "基于当前美股市场(2026年7月)，一句话总结市场情绪和交易建议。用中文。",
        system_prompt="你是华尔街分析师。回答要简洁。",
        max_tokens=100, timeout=20)


# ═══════════════════════════════════════════
# 初始化
# ═══════════════════════════════════════════

def _init():
    """启动时自动检测最佳模型"""
    global _current_model

    # 如果 DeepSeek 直连可用，优先使用
    if DEEPSEEK_KEY:
        if _test_connectivity("deepseek-v4").startswith("✅"):
            _current_model = "deepseek-v4"
            logger.info(f"🤖 默认AI: DeepSeek V4 (直连)")
            return

    # 否则用 OpenRouter 的 DeepSeek V3
    if OPENROUTER_KEY:
        if _test_connectivity("deepseek-v3").startswith("✅"):
            _current_model = "deepseek-v3"
            logger.info(f"🤖 默认AI: DeepSeek V3 (OpenRouter)")
            return

    # 最后用免费 Llama
    _current_model = "llama-3"
    logger.info(f"🤖 默认AI: Llama 3.1 (免费)")


_init()
