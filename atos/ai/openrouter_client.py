"""
ATOS AI — OpenRouter 客户端 (Fusion Budget)
===========================================
通过 OpenRouter 调用 Fusion Budget 模型（性价比组合，~$0.04/请求）。
作为 DeepSeek 的补充 AI 后端。

OpenRouter API: https://openrouter.ai/api/v1/chat/completions
Fusion Budget: openrouter/fusion-budget (自动路由到最优性价比模型)

用法:
  from atos.ai.openrouter_client import call_fusion, quick_ask
"""

import json, os, time, urllib.request
from atos.core.logging import get_logger

logger = get_logger("ai.openrouter")

OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    ""
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FUSION_MODEL = "meta-llama/llama-3.1-8b-instruct"  # 免费，OpenRouter可用


def call_fusion(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.3,
    max_tokens: int = 500,
    timeout: int = 30,
) -> str:
    """
    调用 OpenRouter Fusion Budget 模型。

    Args:
        prompt: 用户消息
        system_prompt: 系统提示
        temperature: 温度 (0.0-1.0)
        max_tokens: 最大输出token
        timeout: 超时秒数

    Returns:
        模型回复文本
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    data = json.dumps({
        "model": FUSION_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    try:
        req = urllib.request.Request(OPENROUTER_URL, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "HTTP-Referer": "https://atos-pro.local",
            "X-Title": "ATOS PRO Trading System",
        })

        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())

        elapsed = time.time() - t0
        model_used = result.get("model", "unknown")
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})

        logger.info(f"Fusion: {model_used} {len(content)}chars "
                    f"{elapsed:.1f}s "
                    f"in={usage.get('prompt_tokens',0)} "
                    f"out={usage.get('completion_tokens',0)}")

        return content

    except Exception as e:
        logger.warning(f"Fusion调用失败: {e}")
        return ""


def quick_ask(question: str) -> str:
    """快速问 Fusion 一个问题（简短回复）"""
    return call_fusion(
        prompt=question,
        system_prompt="你是ATOS交易系统的AI助手。用中文简洁回答，不超过3句话。",
        temperature=0.3,
        max_tokens=200,
        timeout=20,
    )


def analyze_trade_opportunity(
    symbol: str,
    factor_score: float,
    rsi: float,
    trend: str,
    market_regime: str,
    vix: float,
    news_headlines: list = None,
) -> dict:
    """
    用 Fusion 分析单个交易机会。

    Returns:
        {"verdict": "BUY|WAIT|SKIP", "confidence": 0.0-1.0, "reason": str}
    """
    headlines_str = "\n".join(f"  - {h}" for h in (news_headlines or [])[:3])

    prompt = f"""分析这个交易机会（只输出JSON）:

标的: {symbol}
因子评分: {factor_score:.2f}
RSI: {rsi:.0f}
趋势: {trend}
市场体制: {market_regime}
VIX: {vix:.0f}
相关新闻:
{headlines_str or '  无'}

输出JSON:
{{"verdict":"BUY|WAIT|SKIP","confidence":0.0-1.0,"reason":"用中文，一句话解释"}}"""

    resp = call_fusion(
        prompt=prompt,
        system_prompt="你是量化交易分析师。只输出JSON，不要其他文字。",
        temperature=0.2,
        max_tokens=150,
        timeout=20,
    )

    # Parse JSON from response
    try:
        import re
        match = re.search(r'\{[^}]+\}', resp)
        if match:
            return json.loads(match.group())
    except Exception:
        pass

    return {"verdict": "WAIT", "confidence": 0.4, "reason": "Fusion分析失败"}


def get_market_sentiment_fusion() -> dict:
    """用 Fusion 分析当前市场情绪"""
    resp = call_fusion(
        prompt="基于当前市场情况(2026年7月)，用一句话总结美股市场情绪。输出JSON: {\"sentiment\":\"bullish|neutral|bearish\",\"score\":0-100,\"summary\":\"一句话\"}",
        system_prompt="你是华尔街市场分析师。只输出JSON。",
        temperature=0.3,
        max_tokens=100,
        timeout=20,
    )
    try:
        import re
        match = re.search(r'\{[^}]+\}', resp)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return {"sentiment": "neutral", "score": 50, "summary": "无法获取"}
