"""
ATOS PRO v5 — AI 决策引擎（GuruAgents + 牛熊辩论 + 反思代理 + 提示词集成）
==========================================================================

v5 核心创新（基于 2025-2026 前沿研究）:
  1. GuruAgents 多角色框架 — 4位投资大师独立分析（Buffett/Lynch/Greenblatt/Soros）
  2. 牛熊辩论 — 每个候选标的由牛方和熊方辩论，主席裁判
  3. 反思代理 — 每轮复盘上轮决策，口头反馈注入下轮提示词（+31% 绩效）
  4. 提示词集成 — 跑2个变体，只保留两者共识的标的（解决 <30% 重叠问题）
  5. 动态因子权重 — 基于 IC 表现 + 市场体制实时调整

参考文献:
  - GuruAgents (CIKM 2025): Role-based persona +42.2% CAGR
  - TradingAgents: Multi-agent debate, DeepSeek +49% in benchmark
  - Adaptive Multi-Agent: Verbal feedback +31% total performance
  - Multi-faceted variability: Ensemble across prompt variations

使用方法:
  from atos.ai.engine_v5 import get_advice_v5
  result = get_advice_v5(snapshot)
"""

import json
import os
import datetime
import requests
import re
import math
from atos.core.logging import get_logger

logger = get_logger("ai.engine_v5")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


# ════════════════════════════════════════════════════════════
# 0. 工具函数
# ════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON（兼容 markdown 包裹、文本前缀、不完整JSON）

    DeepSeek 可能返回：
    - 纯 JSON: {...}
    - markdown 包裹: ```json\n...\n```
    - 文本前缀+JSON: "好的，这是分析结果：\n{...}"
    - 不完整JSON: 缺少开头的 { 或结尾的 }
    """
    if not text or not isinstance(text, str):
        raise ValueError("空响应")

    text = text.strip()

    # 1. 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 提取 ```json ... ``` 或 ``` ... ```
    m = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. 找到第一个 { 和最后一个 }，提取中间的 JSON
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            # 尝试修复常见问题：尾部多余逗号
            try:
                import re as _re
                fixed = _re.sub(r',\s*}', '}', candidate)
                fixed = _re.sub(r',\s*]', ']', fixed)
                return json.loads(fixed)
            except (json.JSONDecodeError, TypeError):
                pass

    # 4. 尝试提取 [...] 数组
    m = re.search(r'(\[.*\])', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # 5. 最后尝试：用正则提取整个 {...}
    m = re.search(r'(\{.*\})', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    raise ValueError(f"无法提取JSON: {text[:200]}")


def _safe_format(template: str, **kwargs) -> str:
    """安全的模板替换——只替换 {key} 占位符，不碰 JSON 的 {} 括号。

    Python 的 str.format() 会把 JSON 模板里的 { 当成占位符导致 KeyError。
    这个函数只精确替换 {key} 形式的占位符，JSON 花括号原样保留。"""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        key = key.strip().strip("'\"")
        if key:
            return key
    for env_path in [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.expanduser("~/.hermes/.env"),
        os.path.expanduser("~/.env"),
    ]:
        if os.path.exists(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DEEPSEEK_API_KEY="):
                            val = line.split("=", 1)[1].strip().strip("\"'")
                            if val:
                                return val
            except Exception:
                pass
    return ""


def _call_deepseek(system_prompt: str, user_content: str, temperature: float = 0.3, timeout: int = 60) -> str:
    """统一的 DeepSeek API 调用"""
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")
    payload = {
        "model": MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(API_URL, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ════════════════════════════════════════════════════════════
# 1. GuruAgents — 四位投资大师独立分析
# ════════════════════════════════════════════════════════════

GURU_PROFILES = {
    "buffett": {
        "name": "沃伦·巴菲特",
        "style": "价值投资 —— 寻找有持久护城河、优秀管理层、合理估值的伟大企业",
        "maxims": [
            "别人贪婪时我恐惧，别人恐惧时我贪婪",
            "以合理价格买入伟大企业，远胜于以便宜价格买入平庸企业",
            "如果你不打算持有一只股票十年，那就十分钟都不要持有",
        ],
        "buy_triggers": ["ROE>15%连续5年", "低负债率", "可理解的商业模式", "定价权/护城河", "PE<行业平均"],
        "sell_triggers": ["护城河消失", "管理层恶化", "估值极端高估", "商业模式被颠覆"],
        "focus": "企业质量、护城河深度、管理层诚信、估值安全边际",
    },
    "lynch": {
        "name": "彼得·林奇",
        "style": "成长投资 —— 寻找盈利增长加速、估值合理的成长股",
        "maxims": [
            "投资你了解的东西",
            "寻找 PEG<1 的成长股",
            "完美的股票往往从无人关注处诞生",
        ],
        "buy_triggers": ["盈利增速>20%", "PEG<1.5", "机构持股低", "内幕买入", "新产品/新市场"],
        "sell_triggers": ["增长放缓连续2季", "PEG>3", "故事变了", "更好机会出现"],
        "focus": "盈利增长率、PEG比率、催化剂事件、行业地位变化",
    },
    "greenblatt": {
        "name": "乔尔·格林布拉特",
        "style": "神奇公式 —— 好公司（高ROC）+ 好价格（高EY）",
        "maxims": [
            "神奇公式不在乎市场情绪，只在乎数字",
            "高资本回报率 + 高盈利收益率 = 超额收益",
            "坚持公式，不要被短期波动吓跑",
        ],
        "buy_triggers": ["ROC>20%", "EY>10%", "排名在前20%", "持续高ROC（非一次性）"],
        "sell_triggers": ["排名跌出前50%", "ROC显著恶化", "盈利收益率变为负"],
        "focus": "资本回报率(ROC)、盈利收益率(EY)、排名持续性、财务质量",
    },
    "soros": {
        "name": "乔治·索罗斯",
        "style": "宏观反射 —— 趋势是你的朋友，直到拐点出现",
        "maxims": [
            "重要的不是对错，而是对的时候赚多少，错的时候亏多少",
            "市场总是错的，但趋势可以持续很久",
            "先投资，再调查",
        ],
        "buy_triggers": ["趋势加速", "资金流入", "宏观顺风", "市场情绪从恐惧转贪婪"],
        "sell_triggers": ["反身性拐点信号", "趋势衰竭", "拥挤交易", "宏观转向"],
        "focus": "趋势强度、资金流向、宏观催化剂、拥挤度风险",
    },
}

GURU_ANALYSIS_PROMPT = """你是 {guru_name}，你的投资风格是：{guru_style}

你的核心原则：
{maxims}

你的买入触发条件：{buy_triggers}
你的卖出触发条件：{sell_triggers}
你关注的重点：{focus}

现在，请以 {guru_name} 的身份分析以下投资候选标的。

分析数据：
{stock_data}

你必须输出严格的 JSON 格式（不要有其他文字）：
{{
    "guru": "{guru_key}",
    "symbol": "股票代码",
    "verdict": "BUY|WAIT|SELL",
    "conviction": 0.0-1.0（你对自己判断的确信度）,
    "reasoning": "用你的投资哲学解释为什么（中文，2-4句话）",
    "key_metric": "你最关注的一个数字是什么",
    "risk_flag": "你看到的最大风险（如果有）",
    "target_price": 目标价（0表示不确定）,
    "time_horizon": "你预期的持有时间"
}}

记住：你只能以 {guru_name} 的身份和分析框架来思考。不要输出其他风格的建议。"""


def _guru_analyze(guru_key: str, stock_data: dict) -> dict:
    """让一位投资大师分析单个候选标的"""
    guru = GURU_PROFILES[guru_key]
    prompt = _safe_format(GURU_ANALYSIS_PROMPT,
        guru_name=guru["name"],
        guru_style=guru["style"],
        maxims="\n".join(f'  "{m}"' for m in guru["maxims"]),
        buy_triggers=", ".join(guru["buy_triggers"]),
        sell_triggers=", ".join(guru["sell_triggers"]),
        focus=guru["focus"],
        guru_key=guru_key,
        stock_data=json.dumps(stock_data, ensure_ascii=False),
    )
    try:
        content = _call_deepseek(prompt, "", temperature=0.4, timeout=45)
        result = _extract_json(content)
        logger.info(f"  {guru['name']} 分析 {stock_data.get('symbol','?')}: {result.get('verdict','?')} (确信度={result.get('conviction',0):.0%})")
        return result
    except Exception as e:
        logger.debug(f"  {guru['name']} 分析失败: {e}")
        return {"guru": guru_key, "symbol": stock_data.get("symbol", "?"), "verdict": "WAIT", "conviction": 0.3,
                "reasoning": f"分析出错: {str(e)[:50]}", "key_metric": "N/A", "risk_flag": "API错误", "target_price": 0}


# ════════════════════════════════════════════════════════════
# 2. 牛熊辩论 — Bull vs Bear + 裁判
# ════════════════════════════════════════════════════════════

DEBATE_BULL_PROMPT = """你是 ATOS 交易系统的**多头分析师**。你的工作是为你分配的股票找到买入的理由。

你的性格：乐观但诚实。不编造利好，但充分挖掘被市场忽视的正面因素。

分析框架（从以下角度找理由）:
1. **催化剂** — 未来1-4周有什么推动股价的事件？（财报、产品发布、行业政策）
2. **估值优势** — 相比同行业或历史，估值是否合理/低估？
3. **动量信号** — 技术面是否显示趋势加速或突破？
4. **资金流向** — 是否有机构增持、内幕买入？
5. **市场定位** — 公司在行业中的竞争地位是否在改善？

你必须输出严格 JSON：
{
    "symbol": "...",
    "stance": "BULL",
    "bull_case_strength": 0.0-1.0（你认为多头理由有多强）,
    "top_3_reasons": ["理由1", "理由2", "理由3"],
    "catalyst": "最关键的催化剂",
    "upside_target": 目标价（0表示不确定）,
    "conviction": 0.0-1.0
}

诚实原则：如果实在找不到好的买入理由，bull_case_strength 可以 < 0.3，并诚实说明。"""

DEBATE_BEAR_PROMPT = """你是 ATOS 交易系统的**空头分析师**。你的工作是找出你分配的股票的风险和问题。

你的性格：谨慎但客观。不制造恐慌，但充分揭示被市场忽视的风险。

分析框架（从以下角度找风险）:
1. **估值风险** — 相比同行业或历史，估值是否过高？
2. **增长风险** — 盈利增长是否在减速？是否有下修风险？
3. **技术风险** — 是否有顶部信号、量价背离、RSI过高？
4. **行业风险** — 行业是否面临监管、竞争加剧、周期见顶？
5. **宏观风险** — 利率、汇率、政策是否构成逆风？

你必须输出严格 JSON：
{
    "symbol": "...",
    "stance": "BEAR",
    "bear_case_strength": 0.0-1.0（你认为空头理由有多强）,
    "top_3_risks": ["风险1", "风险2", "风险3"],
    "worst_case_scenario": "最坏情况下会怎样",
    "downside_target": 下行目标价（0表示不确定）,
    "conviction": 0.0-1.0
}

诚实原则：如果实在找不到重大风险，bear_case_strength 可以 < 0.3，并诚实说明。"""

DEBATE_JUDGE_PROMPT = """你是 ATOS 的首席辩论裁判。你收到了牛方和熊方对一个股票的分析，现在需要做出最终裁决。

裁决规则：
1. 哪一方的论据更**具体**？（有数字、有事件 > 泛泛而谈）
2. 哪一方的逻辑更**可靠**？（基于事实 > 基于猜测）
3. 市场当前处于什么**体制**？（牛市偏多、熊市偏空、震荡偏中性）
4. 综合判断该股票是否值得**现在**买入

市场环境：{market_context}

牛方观点：{bull_arguments}
熊方观点：{bear_arguments}

你必须输出严格 JSON：
{
    "symbol": "...",
    "final_verdict": "BUY|WAIT|SELL",
    "confidence": 0.0-1.0,
    "winner": "BULL|BEAR|DRAW（哪一方的论据更有说服力）",
    "reasoning": "综合判断理由（中文，2-3句话）",
    "key_condition": "什么条件下这个判断会改变",
    "suggested_position_pct": 建议仓位百分比（0-15）
}

核心原则：如果不确定 → WAIT。错过的机会比错误的买入便宜。"""


def _debate_candidate(candidate: dict, market_context: dict) -> dict:
    """对单个候选标的进行牛熊辩论"""
    symbol = candidate.get("symbol", "?")
    api_key = _get_api_key()
    if not api_key:
        return {"symbol": symbol, "final_verdict": "WAIT", "confidence": 0.3,
                "winner": "DRAW", "reasoning": "API不可用，默认等待"}

    try:
        # Phase 1: 牛方分析
        bull_data = json.dumps(candidate, ensure_ascii=False)
        bull_content = _call_deepseek(DEBATE_BULL_PROMPT, bull_data, temperature=0.5, timeout=45)
        bull_result = _extract_json(bull_content)
        logger.info(f"  🐂 牛方 {symbol}: strength={bull_result.get('bull_case_strength',0):.0%}")

        # Phase 2: 熊方分析
        bear_content = _call_deepseek(DEBATE_BEAR_PROMPT, bull_data, temperature=0.5, timeout=45)
        bear_result = _extract_json(bear_content)
        logger.info(f"  🐻 熊方 {symbol}: strength={bear_result.get('bear_case_strength',0):.0%}")

        # Phase 3: 裁判裁决
        judge_input = {
            "market_context": market_context,
            "bull_arguments": json.dumps(bull_result, ensure_ascii=False),
            "bear_arguments": json.dumps(bear_result, ensure_ascii=False),
        }
        judge_prompt = _safe_format(DEBATE_JUDGE_PROMPT,
            market_context=json.dumps(market_context, ensure_ascii=False),
            bull_arguments=json.dumps(bull_result, ensure_ascii=False),
            bear_arguments=json.dumps(bear_result, ensure_ascii=False),
        )
        judge_content = _call_deepseek(judge_prompt, "", temperature=0.2, timeout=45)
        judge_result = _extract_json(judge_content)
        judge_result["_bull_strength"] = bull_result.get("bull_case_strength", 0)
        judge_result["_bear_strength"] = bear_result.get("bear_case_strength", 0)
        judge_result["_bull_reasons"] = bull_result.get("top_3_reasons", [])
        judge_result["_bear_risks"] = bear_result.get("top_3_risks", [])
        logger.info(f"  ⚖️ 裁判 {symbol}: {judge_result.get('final_verdict','?')} "
                     f"(牛{bull_result.get('bull_case_strength',0):.0%} vs 熊{bear_result.get('bear_case_strength',0):.0%})")
        return judge_result

    except Exception as e:
        logger.error(f"辩论失败 {symbol}: {e}")
        return {"symbol": symbol, "final_verdict": "WAIT", "confidence": 0.2,
                "winner": "DRAW", "reasoning": f"辩论过程出错: {str(e)[:50]}"}


# ════════════════════════════════════════════════════════════
# 3. 反思代理 (Reflection Agent)
# ════════════════════════════════════════════════════════════

REFLECTION_PROMPT = """你是 ATOS 的首席反思官（Chief Reflection Officer）。

你的工作是复盘上一轮交易周期的决策，找出：
1. 哪些决策做对了？为什么？
2. 哪些决策做错了？根本原因是什么？
3. 当前持仓中有没有应该卖但没卖的？
4. 有没有错过的好机会？
5. 风控参数是否需要调整？

复盘数据：
{reflection_data}

你必须输出严格 JSON：
{
    "cycle_grade": "A|B|C|D|F（对本轮决策的总体评分）",
    "wins": ["做得好的决策1", "做得好的决策2"],
    "mistakes": ["错误1", "错误2"],
    "root_cause": "最根本的问题是什么（1句话）",
    "lessons": ["教训1", "教训2"],
    "suggested_changes": {
        "stop_loss_pct": 建议的止损比例调整（0表示不变）,
        "max_positions": 建议的最大持仓数调整（0表示不变）,
        "factor_weight_changes": "因子权重应该如何调整（用中文描述）"
    },
    "urgent_actions": ["需要立即执行的行动1"],
    "mood": "CONFIDENT|CAUTIOUS|CONCERNED|DEFENSIVE"
}

复盘原则：
- 诚实但不苛刻——objective但不self-flagellating
- 关注过程而非结果——好的决策也可能亏钱，坏的决策也可能赚钱
- 找出模式而非个别事件——系统性问题才值得改
- 如果没有什么大问题，就说没有——不要硬找"""


def _reflect_on_cycle(cycle_history: dict) -> dict:
    """反思代理：复盘本轮决策，生成口头反馈"""
    api_key = _get_api_key()
    if not api_key:
        return {"cycle_grade": "C", "mistakes": [], "lessons": ["无API跳过反思"],
                "suggested_changes": {}, "urgent_actions": [], "mood": "CAUTIOUS"}

    try:
        reflection_data = json.dumps(cycle_history, ensure_ascii=False)
        prompt = _safe_format(REFLECTION_PROMPT, reflection_data=reflection_data)
        content = _call_deepseek(prompt, "", temperature=0.3, timeout=60)
        result = _extract_json(content)
        logger.info(f"🪞 反思: 评级={result.get('cycle_grade','?')} "
                     f"情绪={result.get('mood','?')} "
                     f"教训={len(result.get('lessons',[]))}条")
        return result
    except Exception as e:
        logger.error(f"反思失败: {e}")
        return {"cycle_grade": "C", "mistakes": [], "lessons": [f"反思出错: {str(e)[:50]}"],
                "suggested_changes": {}, "urgent_actions": [], "mood": "CAUTIOUS"}


# ════════════════════════════════════════════════════════════
# 4. 增强版 CIO — 带行业轮动和宏观因子
# ════════════════════════════════════════════════════════════

ENHANCED_CIO_PROMPT = """你是 ATOS v5 的增强版首席投资官（CIO）。你的分析框架比 v4 多了三个维度。

你收到的数据：
{cio_data}

分析框架（8个维度）：
1. **大盘趋势** — SPY vs MA20/MA50/MA200，趋势强度（ADX等效）
2. **市场情绪** — VIX水平、恐惧贪婪指数、put/call ratio
3. **宏观环境** — 利率方向、通胀预期、美元走势
4. **行业轮动** — 当前哪个行业最强？哪个最弱？资金在流向哪里？
5. **因子表现** — 各因子的近期IC表现，哪个因子在赚钱？
6. **AI记忆** — 历史上的错误模式，避免重蹈覆辙
7. **反思反馈** — 上一轮的反思教训
8. **风险预算** — 当前回撤水平、可用风险预算

你必须输出严格 JSON：
{
    "position_size": 0-100（整数）,
    "risk_level": "LOW|MEDIUM|HIGH|EXTREME",
    "market_observations": ["观察1", "观察2", "观察3", "观察4"],
    "sector_rotation_view": {
        "overweight": ["行业1", "行业2"],
        "underweight": ["行业3"],
        "reasoning": "为什么这样轮动"
    },
    "factor_weight_adjustments": {
        "momentum": 0.0-1.0,
        "value": 0.0-1.0,
        "quality": 0.0-1.0,
        "technical": 0.0-1.0,
        "mean_rev": 0.0-1.0,
        "earnings_revision": 0.0-1.0,
        "reason": "为什么这样调权重"
    },
    "risk_params": {
        "stop_loss_pct": 0.02-0.10,
        "take_profit_pct": 0.05-0.25,
        "max_single_pct": 0.05-0.25,
        "trailing_stop_pct": 0.03-0.12
    },
    "reflection_acknowledgment": "对上轮反思的回应（1-2句）",
    "override_bear_gate": true/false,
    "override_reason": "如建议在BEAR下开仓的理由"
}

关键原则：
- 行业轮动是最被低估的alpha来源 — 选对行业比选对个股更重要
- 因子IC为负时果断降权，不要抱有幻想
- 尊重反思的教训 — 同样的错误不要犯两次
- 如果回撤>5%，自动切换到防守模式"""


def _enhanced_cio_analysis(market_snapshot: dict, reflection: dict = None, factor_ic: dict = None) -> dict:
    """增强版 CIO 分析"""
    api_key = _get_api_key()
    if not api_key:
        return _cio_v5_fallback(market_snapshot)

    try:
        # 构建输入
        market = market_snapshot.get("market", {})
        positions = market_snapshot.get("positions", [])

        # AI记忆
        mem_context = ""
        try:
            from atos.ai.memory import get_memory_stats, get_mistake_patterns
            stats = get_memory_stats()
            mistakes = get_mistake_patterns(min_count=2)
            mem_context = (
                f"历史{stats['total_decisions']}决策 胜率{stats['win_rate']:.1%} "
                f"错误模式: {[m['description'][:40] for m in mistakes[:3]]}"
            )
        except Exception:
            pass

        # 反思反馈
        reflection_context = ""
        if reflection:
            reflection_context = (
                f"上轮评级: {reflection.get('cycle_grade','?')}, "
                f"情绪: {reflection.get('mood','?')}, "
                f"教训: {reflection.get('lessons',[])[:2]}, "
                f"需执行: {reflection.get('urgent_actions',[])[:2]}"
            )

        # 因子IC
        ic_context = ""
        if factor_ic:
            ic_items = []
            for factor_name, ic_val in factor_ic.items():
                ic_items.append(f"{factor_name}: IC={ic_val:.3f}")
            ic_context = "因子近期IC: " + ", ".join(ic_items)

        # 持仓行业分布
        sectors = {}
        for p in positions:
            sec = p.get("sector", "Unknown")
            sectors[sec] = sectors.get(sec, 0) + p.get("weight", 0)

        ci_data = {
            "spy_price": market.get("spy_price", 0),
            "spy_ma20": market.get("spy_ma20", 0),
            "spy_ma50": market.get("spy_ma50", 0),
            "vix": market.get("vix", 18),
            "regime": market.get("regime", "UNKNOWN"),
            "spy_trend": market.get("spy_trend", "NEUTRAL"),
            "sentiment": market.get("sentiment", "NEUTRAL"),
            "fear_greed": market.get("fear_greed", 50),
            "total_equity": market_snapshot.get("total_equity", 0),
            "cash": market_snapshot.get("cash", 0),
            "cash_pct": round(market_snapshot.get("cash", 0) / max(market_snapshot.get("total_equity", 1), 1) * 100, 1),
            "num_positions": len(positions),
            "max_drawdown": market_snapshot.get("max_drawdown", 0),
            "sector_allocation": sectors,
            "ai_memory": mem_context,
            "reflection_feedback": reflection_context,
            "factor_ic": ic_context,
            "equity_curve": market_snapshot.get("equity_history", [])[-10:],
        }

        prompt = _safe_format(ENHANCED_CIO_PROMPT, cio_data=json.dumps(ci_data, ensure_ascii=False))
        content = _call_deepseek(prompt, "", temperature=0.3, timeout=90)
        result = _extract_json(content)
        logger.info(f"CIO v5: 仓位={result.get('position_size',50)}% 风险={result.get('risk_level','MEDIUM')} "
                     f"超配={result.get('sector_rotation_view',{}).get('overweight',[])}")
        return result

    except Exception as e:
        logger.error(f"CIO v5 失败: {e}")
        return _cio_v5_fallback(market_snapshot)


def _cio_v5_fallback(snapshot: dict) -> dict:
    """CIO v5 规则兜底"""
    market = snapshot.get("market", {})
    vix = market.get("vix", 18)
    regime = market.get("regime", "UNKNOWN")
    if vix > 25 or regime in ("BEAR", "HIGH_VOL"):
        pos, risk = 30, "HIGH"
    elif vix > 20:
        pos, risk = 50, "MEDIUM"
    else:
        pos, risk = 70, "LOW"
    return {
        "position_size": pos, "risk_level": risk,
        "market_observations": [f"VIX={vix:.0f}", f"Regime={regime}", "Fallback v5"],
        "sector_rotation_view": {"overweight": ["Technology"], "underweight": [], "reasoning": "默认兜底"},
        "factor_weight_adjustments": {
            "momentum": 0.25, "value": 0.25, "quality": 0.20, "technical": 0.15, "mean_rev": 0.10, "earnings_revision": 0.05,
            "reason": "默认等权兜底"
        },
        "risk_params": {"stop_loss_pct": 0.05, "take_profit_pct": 0.12, "max_single_pct": 0.15, "trailing_stop_pct": 0.06},
        "reflection_acknowledgment": "无反思数据", "override_bear_gate": False, "override_reason": "",
    }


# ════════════════════════════════════════════════════════════
# 5. 提示词集成 — 跑2个变体，取共识
# ════════════════════════════════════════════════════════════

def _ensemble_analyze(candidate: dict, market_context: dict) -> dict:
    """对单个候选跑2个略有不同的提示词变体，只保留共识结果。

    解决 DeepSeek 等 LLM 的 '重复变异性' 问题——同一提示词30%以下重叠。
    """
    symbol = candidate.get("symbol", "?")

    # 变体1: 以价值/质量为导向
    variant1_data = dict(candidate)
    variant1_data["_focus"] = "侧重估值安全边际和商业质量"

    # 变体2: 以动量/催化剂为导向
    variant2_data = dict(candidate)
    variant2_data["_focus"] = "侧重趋势强度和近期催化剂"

    api_key = _get_api_key()
    if not api_key:
        return {"symbol": symbol, "final_verdict": "WAIT", "confidence": 0.3,
                "ensemble_agreement": False, "reasoning": "API不可用"}

    try:
        # 并行尝试（串行调用以控制 API 费用，后续可改为 asyncio）
        r1 = _debate_candidate(variant1_data, market_context)
        r2 = _debate_candidate(variant2_data, market_context)

        v1 = r1.get("final_verdict", "WAIT")
        v2 = r2.get("final_verdict", "WAIT")

        # 共识：两个变体都同意 BUY 或都同意 SELL
        both_buy = (v1 == "BUY" and v2 == "BUY")
        both_sell = (v1 == "SELL" and v2 == "SELL")
        agree = both_buy or both_sell

        if agree:
            final_verdict = v1
            confidence = (r1.get("confidence", 0) + r2.get("confidence", 0)) / 2
            confidence += 0.05  # 共识加分
            reasoning = (f"✅ 双视角共识: {r1.get('reasoning','')[:80]} | {r2.get('reasoning','')[:80]}")
        elif v1 == "BUY" or v2 == "BUY":
            # 一个BUY一个不是 → WAIT
            final_verdict = "WAIT"
            confidence = 0.3
            reasoning = (f"⚠️ 无共识: 视角1={v1}, 视角2={v2} → 等待更多信号")
        else:
            final_verdict = v1
            confidence = (r1.get("confidence", 0) + r2.get("confidence", 0)) / 2
            reasoning = f"视角1={v1} 视角2={v2}: {r1.get('reasoning','')[:80]}"

        logger.info(f"  🔬 集成 {symbol}: v1={v1} v2={v2} → {final_verdict} (共识={'是' if agree else '否'})")
        return {
            "symbol": symbol,
            "final_verdict": final_verdict,
            "confidence": min(confidence, 1.0),
            "ensemble_agreement": agree,
            "reasoning": reasoning,
            "_v1": r1, "_v2": r2,
        }

    except Exception as e:
        logger.error(f"集成分析失败 {symbol}: {e}")
        return {"symbol": symbol, "final_verdict": "WAIT", "confidence": 0.2,
                "ensemble_agreement": False, "reasoning": f"集成出错: {str(e)[:50]}"}


# ════════════════════════════════════════════════════════════
# 6. 主入口 — get_advice_v5
# ════════════════════════════════════════════════════════════

def get_advice_v5(snapshot: dict, use_ensemble: bool = True,
                  use_reflection: bool = True, use_gurus: bool = True) -> dict:
    """ATOS v5 AI 决策主入口。

    参数:
        snapshot: 市场 + 持仓 + 候选的完整快照
        use_ensemble: 是否使用提示词集成（2个变体取共识）
        use_reflection: 是否使用反思代理（复盘上轮）
        use_gurus: 是否使用 GuruAgents 多角色分析

    返回:
        {
            "cio": {...},
            "position_reviews": [...],
            "trade_decisions": [...],
            "debate_results": [...],
            "reflection": {...},
            "guru_opinions": {...},
            "summary": "...",
        }
    """
    result = {
        "cio": {},
        "position_reviews": [],
        "trade_decisions": [],
        "debate_results": [],
        "reflection": {},
        "guru_opinions": {},
        "summary": "",
        "errors": [],
    }

    api_key = _get_api_key()
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY 未设置，使用 fallback")
        from atos.ai.engine_v4 import get_advice_v4
        fallback = get_advice_v4(snapshot)
        fallback["_engine"] = "v4_fallback"
        return fallback

    market_data = snapshot.get("market", {})
    candidates = snapshot.get("candidates", [])
    positions = snapshot.get("positions", [])

    # ── Step 1: 反思代理（复盘上轮） ──
    reflection = {}
    if use_reflection:
        try:
            cycle_history = {
                "positions": [
                    {"symbol": p.get("symbol"), "pnl_pct": p.get("pnl_pct", 0),
                     "weight": p.get("weight", 0), "days_held": p.get("days_held", 0)}
                    for p in positions
                ],
                "last_cycle_actions": snapshot.get("last_actions", []),
                "market_regime": market_data.get("regime", "UNKNOWN"),
                "equity_change": snapshot.get("equity_change_pct", 0),
                "max_drawdown": snapshot.get("max_drawdown", 0),
            }
            # 获取 AI 记忆的统计
            try:
                from atos.ai.memory import get_memory_stats
                cycle_history["ai_stats"] = get_memory_stats()
            except Exception:
                pass
            reflection = _reflect_on_cycle(cycle_history)
            result["reflection"] = reflection
        except Exception as e:
            result["errors"].append(f"反思失败: {e}")

    # ── Step 2: CIO 分析 ──
    try:
        # 获取因子IC
        factor_ic = {}
        try:
            from atos.factors.engine import ic_analysis
            ic_result = ic_analysis(positions if positions else [])
            if ic_result:
                factor_ic = {k: v for k, v in ic_result.items() if isinstance(v, (int, float))}
        except Exception:
            pass

        result["cio"] = _enhanced_cio_analysis(snapshot, reflection, factor_ic)
    except Exception as e:
        result["errors"].append(f"CIO失败: {e}")

    # ── Step 3: 持仓复核 (AI Berkshire风格) ──
    try:
        from atos.portfolio.correlation import SECTOR_MAP
        from atos.tools.financial_rigor import portfolio_health_check, PORTFOLIO_REVIEW_QUESTIONS

        # 组合体检
        health_data = []
        for pos in positions:
            sym = pos.get("symbol", "")
            health_data.append({
                "symbol": sym,
                "market_value": pos.get("weight", 0) * snapshot.get("total_equity", 1),
                "pnl_pct": pos.get("pnl_pct", 0),
                "sector": SECTOR_MAP.get(sym, "Unknown"),
                "days_held": pos.get("days_held", 0),
            })
        health = portfolio_health_check(health_data)

        for pos in positions:
            sym = pos.get("symbol", "")
            pnl = pos.get("pnl_pct", 0)
            weight = pos.get("weight", 0)
            days = pos.get("days_held", 0)

            # AI Berkshire 核心问题: 如果今天不持有，还会买吗？
            would_buy_today = True
            if pnl < -0.08 and weight > 0.05:
                verdict, conf = "SELL", 0.75
                reason = f"亏损{pnl:.0%}超止损线8%+仓位{weight:.0%}"
                would_buy_today = False
            elif pnl > 0.20:
                verdict, conf = "SELL_HALF", 0.65
                reason = f"盈利{pnl:.0%}止盈减半，锁定利润"
            elif pnl < -0.04 and days > 20:
                verdict, conf = "SELL", 0.55
                reason = f"长期亏损{pnl:.0%}超20天，买入逻辑可能已变"
            elif weight > 0.20:
                verdict, conf = "SELL_HALF", 0.60
                reason = f"仓位{weight:.0%}超标>20%，降低集中度风险"
            else:
                verdict, conf = "HOLD", 0.70
                reason = f"盈亏{pnl:+.1%}合理，买入逻辑不变"

            # 加 Berkshire 审视问题
            berkshire_check = f"如果今天不持有，{'仍会' if would_buy_today else '不会'}买入"
            result["position_reviews"].append({
                "symbol": sym, "verdict": verdict, "confidence": conf,
                "reasoning": f"{reason} | {berkshire_check}",
                "would_buy_today": would_buy_today,
            })

        # 组合健康度
        if health["warnings"]:
            logger.info(f"组合体检: {health['health']} | {'; '.join(health['warnings'])}")
        result["_portfolio_health"] = health

    except Exception as e:
        result["errors"].append(f"持仓复核失败: {e}")

    # ── Step 4: 候选分析（核心创新） ──
    try:
        debate_results = []
        guru_opinions = {}

        # 只分析前5个候选（控制API费用）
        top_candidates = candidates[:5]

        for c in top_candidates:
            sym = c.get("symbol", "")
            # 兼容两种数据格式:
            #   Format A (from get_top_picks): {breakdown: {value, momentum, quality, ...}}
            #   Format B (flat keys): {value_score, momentum_score, ...}
            bd = c.get("breakdown", {})
            if bd:
                v_score = bd.get("value", 0.5)
                m_score = bd.get("momentum", 0.5)
                q_score = bd.get("quality", 0.5)
                t_score = bd.get("technical", 0.5)
                r_score = bd.get("mean_rev", 0.5)
            else:
                v_score = c.get("value_score", 0.5)
                m_score = c.get("momentum_score", 0.5)
                q_score = c.get("quality_score", 0.5)
                t_score = c.get("technical_score", 0.5)
                r_score = c.get("mean_rev_score", 0.5)

            cand_data = {
                "symbol": sym,
                "price": c.get("price", 0),
                "rsi": c.get("rsi", 50),
                "trend": c.get("trend", "NEUTRAL"),
                "sector": c.get("sector", "Unknown"),
                "market_cap": c.get("market_cap", "Unknown"),
                "value_score": v_score,
                "momentum_score": m_score,
                "quality_score": q_score,
                "technical_score": t_score,
                "mean_rev_score": r_score,
                "volume_ratio": c.get("volume_ratio", 1.0),
                "bollinger_pct_b": c.get("bollinger", {}).get("pct_b", 0.5),
                "news_score": c.get("news_score", 0),
                "ma50": c.get("ma50", 0),
                "ma200": c.get("ma200", 0),
                "atr": c.get("atr", 0),
            }

            # 市场上下文
            market_context = {
                "regime": market_data.get("regime", "UNKNOWN"),
                "spy_trend": market_data.get("spy_trend", "NEUTRAL"),
                "vix": market_data.get("vix", 18),
                "fear_greed": market_data.get("fear_greed", 50),
            }

            # A. GuruAgents 分析（如果启用）
            if use_gurus:
                guru_results = {}
                for guru_key in ["buffett", "lynch", "greenblatt", "soros"]:
                    try:
                        guru_results[guru_key] = _guru_analyze(guru_key, cand_data)
                    except Exception as e:
                        guru_results[guru_key] = {"verdict": "WAIT", "conviction": 0.3,
                                                   "reasoning": f"错误: {str(e)[:40]}"}
                guru_opinions[sym] = guru_results

                # 统计大师共识
                guru_verdicts = [g["verdict"] for g in guru_results.values()]
                buy_count = sum(1 for v in guru_verdicts if v == "BUY")
                sell_count = sum(1 for v in guru_verdicts if v == "SELL")
                logger.info(f"  🎓 大师会诊 {sym}: BUY={buy_count}/4 SELL={sell_count}/4 "
                             f"(巴菲特={guru_results['buffett']['verdict']} 林奇={guru_results['lynch']['verdict']} "
                             f"格林布拉特={guru_results['greenblatt']['verdict']} 索罗斯={guru_results['soros']['verdict']})")

            # B. 牛熊辩论 + 集成
            if use_ensemble:
                debate_result = _ensemble_analyze(cand_data, market_context)
            else:
                debate_result = _debate_candidate(cand_data, market_context)

            # C. 合并大师意见到辩论结果
            if use_gurus:
                buy_count = sum(1 for g in guru_opinions.get(sym, {}).values() if g.get("verdict") == "BUY")
                if buy_count >= 3 and debate_result.get("final_verdict") == "WAIT":
                    # 3/4大师同意买入 → 比辩论更乐观
                    debate_result["final_verdict"] = "BUY"
                    debate_result["confidence"] = min(debate_result.get("confidence", 0.5) + 0.1, 1.0)
                    debate_result["reasoning"] += " [大师共识加成: 3/4位大师同意]"
                elif buy_count <= 1 and debate_result.get("final_verdict") == "BUY":
                    # 仅1/4大师同意买入 → 需要降置信度
                    debate_result["confidence"] = max(debate_result.get("confidence", 0.5) - 0.1, 0.2)
                    debate_result["reasoning"] += f" [大师分歧: 仅{buy_count}/4同意]"

            debate_results.append(debate_result)

            # 记录到 AI 记忆
            try:
                from atos.ai.memory import record_decision
                record_decision(
                    symbol=sym,
                    action=debate_result.get("final_verdict", "WAIT"),
                    confidence=debate_result.get("confidence", 0.5),
                    factor_score=c.get("factor_score", c.get("technical_score", 0.5)),
                    reasons={"v5_analysis": debate_result.get("reasoning", "")},
                    debate_summary=f"v5 辩论: {debate_result.get('winner','?')}赢, "
                                   f"牛{debate_result.get('_bull_strength',0):.0%} vs 熊{debate_result.get('_bear_strength',0):.0%}",
                    market_regime=market_data.get("regime", "UNKNOWN"),
                )
            except Exception:
                pass

        result["debate_results"] = debate_results
        result["guru_opinions"] = guru_opinions

        # 构建 trade_decisions（兼容旧接口）
        result["trade_decisions"] = [
            {
                "symbol": d.get("symbol", ""),
                "decision": d.get("final_verdict", "WAIT"),
                "confidence": d.get("confidence", 0.3),
                "reasoning": d.get("reasoning", ""),
                "position_size_pct": d.get("suggested_position_pct", 0),
                "ensemble_agreement": d.get("ensemble_agreement", False),
            }
            for d in debate_results
        ]

    except Exception as e:
        result["errors"].append(f"候选分析失败: {e}")

    # ── Step 5: 生成摘要 ──
    cio = result["cio"]
    buy_count = sum(1 for d in result["trade_decisions"] if d.get("decision") == "BUY")
    sell_count = sum(1 for r in result["position_reviews"] if r.get("verdict") in ("SELL", "SELL_HALF"))
    reflection_grade = reflection.get("cycle_grade", "?") if reflection else "?"

    parts = [
        f"CIO仓位{cio.get('position_size',50)}%",
        f"风险{cio.get('risk_level','MEDIUM')}",
    ]
    if buy_count:
        parts.append(f"建议买入{buy_count}只")
    if sell_count:
        parts.append(f"建议卖出{sell_count}只")
    if reflection:
        parts.append(f"反思评级{reflection_grade}")

    result["summary"] = " | ".join(parts)
    result["_engine"] = "v5"

    logger.info(f"v5 分析完成: {result['summary']}")
    return result


# ════════════════════════════════════════════════════════════
# 7. 兼容接口
# ════════════════════════════════════════════════════════════

def veto_candidates(candidates: list) -> dict:
    """兼容 v4 的 veto_candidates 接口 — 使用 v5 辩论逻辑"""
    if not candidates:
        return {}
    api_key = _get_api_key()
    if not api_key:
        from atos.ai.engine_v4 import veto_candidates as v4_veto
        return v4_veto(candidates)

    veto_map = {}
    for c in candidates[:3]:
        sym = c.get("symbol", "")
        try:
            # 使用简化的单次辩论（不集成）
            mini_context = {
                "regime": c.get("regime", "UNKNOWN"),
                "spy_trend": c.get("spy_trend", "NEUTRAL"),
                "vix": c.get("vix", 18),
            }
            debate = _debate_candidate(c, mini_context)
            vetoed = debate.get("final_verdict") == "SELL"
            veto_map[sym] = vetoed
            logger.info(f"v5 否决 {sym}: {'❌否决' if vetoed else '✅批准'} ({debate.get('reasoning','')[:50]})")
        except Exception as e:
            logger.debug(f"v5 否决 {sym} 失败: {e}")
            veto_map[sym] = False
    return veto_map


def get_advice_v4(snapshot: dict) -> dict:
    """兼容 v4 接口 — 内部使用 v5"""
    return get_advice_v5(snapshot, use_ensemble=False, use_reflection=False, use_gurus=False)


def get_advice_v2(snapshot: dict) -> dict:
    """兼容旧 v2 接口"""
    v5 = get_advice_v5(snapshot, use_ensemble=False, use_reflection=False, use_gurus=False)
    return {
        "short_term_actions": [],
        "long_term_actions": [],
        "position_reviews": [
            {"position": r.get("symbol", ""), "action": r.get("verdict", "HOLD"),
             "confidence": r.get("confidence", 0.5), "reason": r.get("reasoning", "")[:100]}
            for r in v5.get("position_reviews", [])
        ],
        "veto_map": {},
        "portfolio_health": v5.get("cio", {}).get("risk_level", "UNKNOWN"),
        "cio_market_read": v5.get("cio", {}).get("reasoning", ""),
        "risk_notes": v5.get("summary", ""),
        "market_read": v5.get("cio", {}).get("reasoning", ""),
        "cycle_summary": v5.get("summary", ""),
    }
