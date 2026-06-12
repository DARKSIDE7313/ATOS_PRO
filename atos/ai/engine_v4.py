"""
# ATOS PRO v4 — AI 决策引擎（重写版）
# 所有 JSON 解析使用 _extract_json() 统一处理 DeepSeek 非结构化输出
# BUGFIX 2026-06-12: 
#   - 所有 json.loads() 改为 _extract_json() (处理 ```json 包裹 + markdown)
#   - 修复双层 json.loads(resp.json()) 导致全部崩走fallback
#   - API_KEY 读取增加换行/strip防御
#   - 每个API失败时有完整回溯日志"""

import json
import os
import datetime
import requests
import re
from atos.core.logging import get_logger
# Noise guard: skip recording tiny pnl changes
NOISE_THRESHOLD = 0.001
def _is_noise(pnl_change):
    return abs(pnl_change) < NOISE_THRESHOLD


logger = get_logger("ai.engine_v4")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"  # DeepSeek API 模型名

def _extract_json(text: str) -> dict:
    """从 LLM 响应中提取并解析 JSON。
    
    DeepSeek 在无 response_format 约束时可能返回：
    - 纯 JSON: {...}
    - markdown 包裹: ```json\n...\n```
    - markdown 包裹: ```\n...\n```
    - 文本+JSON混合: "... {...} ..."
    
    依次尝试：直接解析 → markdown提取 → 正则提取第一个{...}
    全部失败则抛出 ValueError。
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
    
    # 3. 提取第一个 {...} 或 [{...}]
    m = re.search(r'(\{.*\})', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    
    m = re.search(r'(\[.*\])', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    
    raise ValueError(f"无法从响应中提取JSON: {text[:200]}")


# 从多个可能的位置读取 API Key
def _get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        key = key.strip().strip("'\"")
        if key:
            return key
    # 从 Hermes .env 读取
    for env_path in [
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

API_KEY = _get_api_key()


# ════════════════════════════════════════════════════════════
# 1. CIO — 市场研判 + 仓位建议
# ════════════════════════════════════════════════════════════

CIO_SYSTEM_PROMPT = """
你是 ATOS 交易系统的首席投资官（CIO）。你的工作是做自上而下的市场分析并给出具体的策略指导。

分析框架：
1. **大盘趋势** — SPY 的趋势、MA20/MA50/MA200 关系、VIX 水平
2. **市场情绪** — 恐惧贪婪指数、综合情绪评分
3. **宏观环境** — 利率、通胀预期、地缘政治
4. **AI记忆反思** — 回顾过去决策的胜率，避免重复错误模式
5. **仓位建议** — 根据以上因素，给出最优仓位比例 0-100%
6. **因子权重调整** — 当前市场环境下哪些因子更重要
7. **风控参数** — 建议的止损/止盈比例
8. **风险警示** — 需要特别关注的系统性风险

必须输出严格 JSON 格式，含以下字段：
{
    "position_size": 0-100整数,
    "risk_level": "LOW|MEDIUM|HIGH",
    "market_observations": ["观察1", "观察2", "观察3"],
    "reasoning": "中文推理（3-5句话）",
    "factor_weight_suggestions": {
        "momentum": 0.0-1.0,
        "value": 0.0-1.0,
        "quality": 0.0-1.0,
        "technical": 0.0-1.0,
        "mean_rev": 0.0-1.0,
        "reason": "为什么这样调权重"
    },
    "risk_params": {
        "stop_loss_pct": 建议止损比例(0.02-0.10),
        "take_profit_pct": 建议止盈比例(0.05-0.25),
        "max_single_pct": 单只最大仓位(0.05-0.25)
    },
    "override_bear_gate": true/false,
    "override_reason": "如果建议在BEAR下开仓，必须给出理由"
}

规则：
- 仓位建议必须是一个 0-100 的整数百分比
- 给出 3 个最重要的市场观察（每个不超过 20 字）
- 如果你认为有系统性风险，risk_level 设为 HIGH
- 如果 AI记忆显示某些模式反复失败，在reasoning中提及
- override_bear_gate 默认为 false，只有在强烈认为应该逆势时才设为 true
"""


def cio_analysis(market_snapshot: dict) -> dict:
    """CIO 市场研判 — 决定仓位和风险偏好"""
    if not API_KEY:
        return _cio_fallback(market_snapshot)

    try:
        prompt_input = {
            "spy_price": market_snapshot.get("spy_price", 0),
            "spy_ma20": market_snapshot.get("spy_ma20", 0),
            "spy_ma50": market_snapshot.get("spy_ma50", 0),
            "vix": market_snapshot.get("vix", 18),
            "regime": market_snapshot.get("regime", "UNKNOWN"),
            "sentiment": market_snapshot.get("sentiment", "NEUTRAL"),
            "fear_greed": market_snapshot.get("fear_greed", 50),
            "macro_notes": market_snapshot.get("macro_notes", ""),
            "total_equity_usd": int(market_snapshot.get("total_equity", 0)),
            "current_cash_pct": round(market_snapshot.get("current_cash_pct", 0) * 100, 1),
        }

        payload = {
            "model": MODEL,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": CIO_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt_input, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = _extract_json(content)
        logger.info(f"CIO: 仓位={result.get('position_size',50)}% 风险={result.get('risk_level','MEDIUM')}")
        return result
    except Exception as e:
        logger.error(f"CIO分析失败: {e}")
        return _cio_fallback(market_snapshot)


def _cio_fallback(snapshot: dict) -> dict:
    """规则兜底"""
    vix = snapshot.get("vix", 18)
    regime = snapshot.get("regime", "UNKNOWN")

    if vix > 25 or regime in ("BEAR", "HIGH_VOL"):
        pos = 30
        risk = "HIGH"
    elif vix > 20:
        pos = 50
        risk = "MEDIUM"
    else:
        pos = 70
        risk = "LOW"

    return {
        "position_size": pos,
        "risk_level": risk,
        "market_observations": [f"VIX={vix:.0f}", f"Regime={regime}", f"仓位{pos}%"],
        "reasoning": f"Fallback: VIX={vix:.1f} Regime={regime}",
    }


# ════════════════════════════════════════════════════════════
# 2. 持仓问诊 — AI 独立诊断每只持仓
# ════════════════════════════════════════════════════════════

POSITION_REVIEW_PROMPT = """你是 ATOS 的持仓分析师。你的任务是对单只持仓做独立诊断。

你收到的数据：
- symbol, qty, avg_price, current_price, pnl_pct
- sector, market_regime, spy_trend
- rsi, ma50, ma200, volume_ratio, atr
- days_held（持有天数）
- position_weight（占组合比例）

你的输出（JSON格式必须严格）：
{
    "symbol": "...",
    "verdict": "HOLD | SELL | ADD",
    "confidence": 0.0-1.0,
    "reasoning": "中文推理过程（3-5句话）",
    "target_price": 目标价或0,
    "stop_loss": 建议止损价或0,
    "what_could_change_my_mind": "什么情况下我会改变判断"
}

诊断框架：
1. **趋势配合** — 大盘趋势向上且个股强势 → 持有/加仓
2. **估值合理性** — 不要仅因涨了一点就卖，也不要仅因跌了一点就买
3. **止损纪律** — 如果逻辑变了就卖，如果只是波动就持有
4. **仓位管理** — 如果单只超过15%仓位建议减仓，低于3%建议加仓
5. **时间维度** — 持有少于5天给更多耐心，超过20天重新审视逻辑
"""


def review_position(position_data: dict) -> dict:
    """诊断单只持仓"""
    if not API_KEY:
        return _pos_fallback(position_data)

    try:
        payload = {
            "model": MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": POSITION_REVIEW_PROMPT},
                {"role": "user", "content": json.dumps(position_data, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = _extract_json(resp.json()["choices"][0]["message"]["content"])
        logger.info(f"持仓诊断 {position_data.get('symbol','?')}: {result.get('verdict','?')} conf={result.get('confidence',0):.2f}")
        return result
    except Exception as e:
        logger.debug(f"持仓诊断失败: {e}")
        return _pos_fallback(position_data)


def _pos_fallback(data: dict) -> dict:
    pnl = data.get("pnl_pct", 0)
    sym = data.get("symbol", "?")
    if pnl < -0.10:
        return {"symbol": sym, "verdict": "SELL", "confidence": 0.6, "reasoning": f"亏损{pnl:.0%}到止损线", "target_price": 0, "stop_loss": 0, "what_could_change_my_mind": ""}
    elif pnl > 0.12:
        return {"symbol": sym, "verdict": "SELL", "confidence": 0.5, "reasoning": f"盈利{pnl:.0%}止盈", "target_price": 0, "stop_loss": 0, "what_could_change_my_mind": ""}
    return {"symbol": sym, "verdict": "HOLD", "confidence": 0.5, "reasoning": "正常持有", "target_price": 0, "stop_loss": 0, "what_could_change_my_mind": ""}


# ════════════════════════════════════════════════════════════
# 3. 开仓分析 — 对候选标的做深度分析
# ════════════════════════════════════════════════════════════

OPEN_TRADE_PROMPT = """你是 ATOS 的交易员。你的任务是分析一个候选标的，判断是否值得买入。

你收到的数据：
- symbol, sector, industry
- price, rsi, ma50, ma200, volume_ratio, atr
- trend (UP/DOWN/NEUTRAL), bollinger %B
- market_regime, spy_trend
- factor_scores: value/momentum/quality/technical

你的输出（JSON格式必须严格）：
{
    "symbol": "...",
    "decision": "BUY | WAIT | PASS",
    "confidence": 0.0-1.0,
    "reasoning": "中文推理过程（3-5句话）",
    "position_size_pct": "建议仓位占总资金百分比（0-15）",
    "target_price": 目标价,
    "stop_loss_price": 止损价,
    "key_risks": ["风险1", "风险2"],
    "catalyst": "这个标的的催化剂是什么"
}

分析框架：
1. **趋势是第一位的** — 大盘跌的时候不要逆势买入
2. **好公司 + 好价格** — 趋势向上 + RSI不太高 + 基本面支持
3. **催化剂** — 未来1-4周有什么推动股价的事件
4. **风险识别** — 最大的风险是什么？概率多大？
5. **仓位建议** — 最多不超过总资金的15%

核心纪律：
- 不要追高（RSI>65不买）
- 不要在大盘下跌趋势中买入
- 不知道催化剂就不买
- 没有止损计划就不买
"""


def analyze_candidate(candidate_data: dict) -> dict:
    """分析候选标的"""
    if not API_KEY:
        return _candidate_fallback(candidate_data)

    try:
        payload = {
            "model": MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": OPEN_TRADE_PROMPT},
                {"role": "user", "content": json.dumps(candidate_data, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = _extract_json(resp.json()["choices"][0]["message"]["content"])
        logger.info(f"开仓分析 {candidate_data.get('symbol','?')}: {result.get('decision','?')} conf={result.get('confidence',0):.2f}")
        return result
    except Exception as e:
        logger.debug(f"开仓分析失败: {e}")
        return _candidate_fallback(candidate_data)


def _candidate_fallback(data: dict) -> dict:
    sym = data.get("symbol", "?")
    rsi = data.get("rsi", 50)
    trend = data.get("trend", "NEUTRAL")
    if rsi > 65 or trend == "DOWN":
        return {"symbol": sym, "decision": "PASS", "confidence": 0.5, "reasoning": f"RSI={rsi:.0f}或趋势{trend}不满足条件", "position_size_pct": 0, "target_price": 0, "stop_loss_price": 0, "key_risks": [], "catalyst": ""}
    return {"symbol": sym, "decision": "WAIT", "confidence": 0.3, "reasoning": "数据不足以判断，等待更多信号", "position_size_pct": 0, "target_price": 0, "stop_loss_price": 0, "key_risks": [], "catalyst": ""}


# ════════════════════════════════════════════════════════════
# ULTRA: 一次 API 调用完成 CIO + 持仓 + 候选 全部分析
# ════════════════════════════════════════════════════════════

ULTRA_PROMPT = """你是 ATOS 量化交易系统的全权投资经理。你有完整的投资决策权。

你收到的数据包含（json format）：
1. **市场环境** — SPY价格/MA20/MA50/VIX/市场体制/情绪/宏观
2. **组合快照** — 总权益、现金、持仓列表（每只的盈亏/仓位/RSI等）
3. **候选标的** — 因子评分最高的候选（含技术面+基本面）
4. **AI记忆** — 历史决策统计和错误模式
5. **业绩历史** — 组合权益曲线和最大回撤

你需要一次性输出完整的投资决策：

{
    "cio": {
        "position_size": 0-100,
        "risk_level": "LOW|MEDIUM|HIGH",
        "market_view": "对市场的3句话判断",
        "factor_weight_suggestions": {
            "momentum": 0.0-1.0, "value": 0.0-1.0,
            "quality": 0.0-1.0, "technical": 0.0-1.0, "mean_rev": 0.0-1.0,
            "reason": "为什么这样调"
        },
        "risk_params": {"stop_loss_pct": 0.02-0.10, "take_profit_pct": 0.05-0.25, "max_single_pct": 0.05-0.25},
        "override_bear_gate": true/false,
        "override_reason": "如果建议BEAR下开仓的理由"
    },
    "position_actions": [
        {
            "symbol": "TICKER",
            "action": "HOLD|SELL_HALF|SELL_ALL|ADD|CUT",
            "confidence": 0.0-1.0,
            "reasoning": "具体原因（中文）",
            "target_price": 0,
            "stop_loss": 0
        }
    ],
    "buy_candidates": [
        {
            "symbol": "TICKER",
            "action": "BUY|WAIT|PASS",
            "confidence": 0.0-1.0,
            "position_pct": 建议仓位百分比(0-15),
            "reasoning": "具体原因（中文）",
            "key_risks": ["风险1"],
            "catalyst": "催化剂"
        }
    ],
    "portfolio_commentary": "对整体组合的一句话评价"
}

核心投资理念：
1. 趋势是你的朋友，但极端情绪是反向信号
2. 好公司+好价格+好时机 = 开仓
3. 亏损超8%或者逻辑变了 = 止损
4. 盈利超15%或者估值过高 = 止盈
5. 持仓不要超过8只，单只不超过20%
6. 现金在手永远有机会，不要被迫买入
7. 如果在BEAR市场但有强烈抄底信号，可以建议override并给出理由
8. 考虑持仓之间的相关性，避免同一行业集中
9. 参考AI记忆中的错误模式，避免重蹈覆辙
10. 对你每个建议标注置信度：>80%很确定，50-80%有把握，<50%不确定
"""


# ════════════════════════════════════════════════════════════
# VETO: 简单的 AI 否决端点（新架构的核心）
# ════════════════════════════════════════════════════════════

VETO_PROMPT = """你是 ATOS 交易系统的进攻型审查员。你的工作是帮交易员找理由批准优秀的候选标的。

因子引擎选出了最高评分的候选标的，现在由你来确认这些机会。

规则：
- 只在你确定这笔交易必定会亏损时才回答 YES（否决）
- 任何不确定性、模糊信号、价格偏高都不应阻止交易
- 寻找"这笔交易为什么可能成功"的理由，而不是找否决的借口
- 以下情况 YES = 否决（极端情况）: 流动性为0、存在已知欺诈风险、系统性崩盘
- 其他所有情况默认 NO = 批准（让交易通过）
- 你能看到的只是部分数据，因子引擎有更完整的分析
- 大胆一点——在Paper Trading中，错过机会比亏钱更糟糕

你必须输出严格 JSON 格式：
{"veto": true/false, "reason": "简短理由（中文，20字以内）"}

默认：如果没有极端理由否决，输出{"veto": false, "reason": "批准"}"""


def veto_candidate(candidate: dict) -> dict:
    """AI 否决审查 — 对单个候选标的回答 YES/NO。

    参数 candidate:
    {
        "symbol": "AAPL",
        "price": 180.50,
        "factor_score": 0.82,
        "reason": "低估值+强动量",
        "rsi": 55,
        "spy_price": 450.0,
        "spy_trend": "BULL",
        "vix": 15.2,
        "regime": "BULL",
    }

    返回:
    {"veto": true/false, "reason": "..."}
    """
    if not API_KEY:
        return {"veto": False, "reason": "无API"}

    try:
        payload = {
            "model": MODEL,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": VETO_PROMPT},
                {"role": "user", "content": json.dumps(candidate, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = _extract_json(resp.json()["choices"][0]["message"]["content"])
        logger.info(f"否决审查 {candidate.get('symbol','?')}: {'❌否决' if result.get('veto') else '✅批准'} | {result.get('reason','')[:40]}")
        return result
    except Exception as e:
        logger.debug(f"否决审查失败: {e}")
        return {"veto": False, "reason": "审查失败，默认通过"}


def veto_candidates(candidates: list) -> dict:
    """对一组候选标的运行否决审查。返回 {symbol: True/False} 映射。"""
    veto_map = {}
    for c in candidates[:3]:  # 最多3个
        result = veto_candidate(c)
        veto_map[c.get("symbol", "")] = result.get("veto", False)
    return veto_map


# ════════════════════════════════════════════════════════════
# 4. 主入口 — 完整决策流程（保留旧接口兼容）
# ════════════════════════════════════════════════════════════

def get_advice_v4(snapshot: dict) -> dict:
    """
    ATOS v4 AI 决策入口。
    
    参数 snapshot 完整格式:
    {
        "total_equity": float,
        "cash": float,
        "positions": [{symbol, qty, avg_price, last_price, pnl_pct, sector, ...}],
        "market": {spy_price, spy_ma20, spy_ma50, vix, regime, sentiment, fear_greed, spy_trend},
        "candidates": [{symbol, price, rsi, ma50, ma200, trend, ...}],
        "macro_notes": "",
    }
    
    返回:
    {
        "cio": {position_size, risk_level, ...},
        "position_reviews": [{symbol, verdict, confidence, ...}],
        "trade_decisions": [{symbol, decision, confidence, ...}],
        "summary": "...",
    }
    """
    result = {
        "cio": {},
        "position_reviews": [],
        "trade_decisions": [],
        "summary": "",
        "errors": [],
    }

    # Step 1: CIO 市场研判（含 AI 记忆上下文）
    try:
        market_data = snapshot.get("market", {})
        # 获取 AI 记忆 — 让 CIO 知道过去的表现
        mem_context = ""
        try:
            from atos.ai.memory import get_memory_stats, get_mistake_patterns
            stats = get_memory_stats()
            mistakes = get_mistake_patterns(min_count=2)
            mem_context = (
                f"历史: {stats['total_decisions']}条决策, "
                f"胜率{stats['win_rate']:.1%}, "
                f"错误模式: {[m['description'][:40] for m in mistakes[:3]]}"
            )
        except Exception:
            pass
        
        cio_input = {
            "spy_price": market_data.get("spy_price", 0),
            "spy_ma20": market_data.get("spy_ma20", 0),
            "spy_ma50": market_data.get("spy_ma50", 0),
            "vix": market_data.get("vix", 18),
            "regime": market_data.get("regime", "UNKNOWN"),
            "spy_trend": market_data.get("spy_trend", "NEUTRAL"),
            "sentiment": market_data.get("sentiment", "NEUTRAL"),
            "fear_greed": market_data.get("fear_greed", 50),
            "macro_notes": snapshot.get("macro_notes", ""),
            "total_equity": snapshot.get("total_equity", 0),
            "current_cash_pct": snapshot.get("cash", 0) / max(snapshot.get("total_equity", 1), 1),
            "ai_memory": mem_context,
        }
        result["cio"] = cio_analysis(cio_input)
    except Exception as e:
        result["errors"].append(f"CIO失败: {e}")

    cio_pos = result["cio"].get("position_size", 50)

    # Step 2: 持仓问诊（每只都诊，但熔断时不诊）
    try:
        positions = snapshot.get("positions", [])
        if positions:
            from atos.portfolio.correlation import SECTOR_MAP
            reviews = []
            for pos in positions:
                sym = pos.get("symbol", "")
                pos_data = {
                    "symbol": sym,
                    "qty": pos.get("qty", 0),
                    "avg_price": pos.get("avg_price", 0),
                    "current_price": pos.get("last_price", pos.get("avg_price", 0)),
                    "pnl_pct": pos.get("pnl_pct", 0),
                    "sector": SECTOR_MAP.get(sym, "Unknown"),
                    "market_regime": market_data.get("regime", "UNKNOWN"),
                    "spy_trend": market_data.get("spy_trend", "NEUTRAL"),
                    "rsi": pos.get("rsi", 50),
                    "volume_ratio": pos.get("volume_ratio", 1.0),
                    "position_weight": pos.get("weight", 0),
                    "days_held": pos.get("days_held", 1),
                }
                review = review_position(pos_data)
                reviews.append(review)
            result["position_reviews"] = reviews
    except Exception as e:
        result["errors"].append(f"持仓诊断失败: {e}")

    # Step 3: 开仓分析
    try:
        candidates = snapshot.get("candidates", [])
        decisions = []
        for c in candidates[:3]:  # 最多分析 3 个候选
            sym = c.get("symbol", "")
            cand_data = {
                "symbol": sym,
                "sector": c.get("sector", "Unknown"),
                "industry": c.get("industry", ""),
                "price": c.get("price", 0),
                "rsi": c.get("rsi", 50),
                "ma50": c.get("ma50", 0),
                "ma200": c.get("ma200", 0),
                "volume_ratio": c.get("volume_ratio", 1.0),
                "atr": c.get("atr", 0),
                "trend": c.get("trend", "NEUTRAL"),
                "bollinger_pct_b": c.get("bollinger", {}).get("pct_b", 0.5),
                "market_regime": market_data.get("regime", "UNKNOWN"),
                "spy_trend": market_data.get("spy_trend", "NEUTRAL"),
                "factor_scores": {
                    "value": c.get("value_score", 0.5),
                    "momentum": c.get("momentum_score", 0.5),
                    "quality": c.get("quality_score", 0.5),
                    "technical": c.get("technical_score", 0.5),
                },
                "cio_position_size": cio_pos,
            }
            decision = analyze_candidate(cand_data)
            decisions.append(decision)
        result["trade_decisions"] = decisions
    except Exception as e:
        result["errors"].append(f"开仓分析失败: {e}")

    # Step 4: 生成摘要
    cio = result["cio"]
    sell_count = sum(1 for r in result["position_reviews"] if r.get("verdict") == "SELL")
    buy_count = sum(1 for d in result["trade_decisions"] if d.get("decision") == "BUY")
    summary_parts = [
        f"CIO建议仓位{cio.get('position_size',50)}%",
        f"风险等级{cio.get('risk_level','MEDIUM')}",
    ]
    if sell_count:
        summary_parts.append(f"建议卖出{sell_count}只")
    if buy_count:
        summary_parts.append(f"建议买入{buy_count}只")
    result["summary"] = " | ".join(summary_parts)

    return result


# ════════════════════════════════════════════════════════════
# ULTRA ENTRY: 一次 API 调用 = CIO + 全持仓 + 全候选
# ════════════════════════════════════════════════════════════
def get_advice_v4_ultra(snapshot: dict) -> dict:
    """ULTRA 模式：一次 DeepSeek API 调用完成全部分析。
    
    相比逐只调用的 v4，ULTRA 的优势：
    - AI 看到全局组合 → 避免行业集中、考虑相关性
    - 持仓+候选一起分析 → 能判断"卖X买Y"的调仓逻辑
    - 1次API调用 vs 1+N+M次 → 更快更便宜
    - AI 有完整的权益曲线 → 能判断回撤是否需要防守
    """
    if not API_KEY:
        return _ultra_fallback(snapshot)
    
    try:
        # 收集所有上下文
        market = snapshot.get("market", {})
        positions = snapshot.get("positions", [])
        candidates = snapshot.get("candidates", [])
        
        # AI 记忆
        mem_context = ""
        try:
            from atos.ai.memory import get_memory_stats, get_mistake_patterns
            stats = get_memory_stats()
            mistakes = get_mistake_patterns(min_count=2)
            mem_context = (
                f"历史{stats['total_decisions']}决策 胜率{stats['win_rate']:.1%} "
                f"错误模式: {[m['description'][:30] for m in mistakes[:3]]}"
            )
        except Exception:
            pass
        
        # 权益历史（最近10个点）
        equity_curve = snapshot.get("equity_history", [])[-10:]
        
        # 宏观数据
        macro_notes = snapshot.get("macro_notes", "")
        
        # 构建 ULTRA 输入
        ultra_input = {
            "market": {
                "spy_price": market.get("spy_price", 0),
                "spy_ma20": market.get("spy_ma20", 0),
                "spy_ma50": market.get("spy_ma50", 0),
                "vix": market.get("vix", 18),
                "regime": market.get("regime", "UNKNOWN"),
                "spy_trend": market.get("spy_trend", "NEUTRAL"),
                "sentiment": market.get("sentiment", "NEUTRAL"),
                "fear_greed": market.get("fear_greed", 50),
            },
            "portfolio": {
                "total_equity": snapshot.get("total_equity", 0),
                "cash": snapshot.get("cash", 0),
                "cash_pct": round(snapshot.get("cash", 0) / max(snapshot.get("total_equity", 1), 1) * 100, 1),
                "num_positions": len(positions),
                "max_drawdown_pct": snapshot.get("max_drawdown", 0),
                "positions": [
                    {
                        "symbol": p.get("symbol", ""),
                        "pnl_pct": round(p.get("pnl_pct", 0) * 100, 2),
                        "weight": round(p.get("weight", 0) * 100, 1),
                        "rsi": p.get("rsi", 50),
                        "sector": p.get("sector", "?"),
                        "days_held": p.get("days_held", 0),
                        "trend_vs_spy": "outperform" if p.get("pnl_pct", 0) > 0 else "underperform",
                    }
                    for p in positions
                ],
                "equity_curve_recent": equity_curve,
            },
            "candidates": [
                {
                    "symbol": c.get("symbol", ""),
                    "price": c.get("price", 0),
                    "rsi": c.get("rsi", 50),
                    "trend": c.get("trend", "NEUTRAL"),
                    "sector": c.get("sector", "?"),
                    "value_score": c.get("value_score", 0.5),
                    "momentum_score": c.get("momentum_score", 0.5),
                    "quality_score": c.get("quality_score", 0.5),
                    "technical_score": c.get("technical_score", 0.5),
                    "mean_rev_score": c.get("mean_rev_score", 0.5),
                }
                for c in candidates[:5]
            ],
            "ai_memory": mem_context,
            "macro_notes": macro_notes,
        }
        
        payload = {
            "model": MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": ULTRA_PROMPT},
                {"role": "user", "content": json.dumps(ultra_input, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        raw = _extract_json(content)
        
        # 映射到兼容格式
        result = {
            "cio": raw.get("cio", {}),
            "position_reviews": [
                {
                    "symbol": pa.get("symbol", ""),
                    "verdict": pa.get("action", "HOLD"),
                    "confidence": pa.get("confidence", 0.5),
                    "reasoning": pa.get("reasoning", ""),
                    "target_price": pa.get("target_price", 0),
                    "stop_loss": pa.get("stop_loss", 0),
                }
                for pa in raw.get("position_actions", [])
            ],
            "trade_decisions": [
                {
                    "symbol": bc.get("symbol", ""),
                    "decision": bc.get("action", "WAIT"),
                    "confidence": bc.get("confidence", 0.3),
                    "position_size_pct": bc.get("position_pct", 0),
                    "reasoning": bc.get("reasoning", ""),
                    "key_risks": bc.get("key_risks", []),
                    "catalyst": bc.get("catalyst", ""),
                }
                for bc in raw.get("buy_candidates", [])
            ],
            "summary": raw.get("portfolio_commentary", ""),
            "errors": [],
        }
        
        logger.info(f"ULTRA完成: {result['cio'].get('risk_level','?')}风险 "
                    f"{len(result['position_reviews'])}持仓 {len(result['trade_decisions'])}候选"
                    f" | {result['summary'][:60]}")
        return result
        
    except Exception as e:
        logger.error(f"ULTRA分析失败: {e}")
        return _ultra_fallback(snapshot)


def _ultra_fallback(snapshot: dict) -> dict:
    """ULTRA 规则兜底"""
    return get_advice_v4(snapshot)  # 降级到 v4
def get_advice_v2(snapshot: dict) -> dict:
    """兼容旧接口 — 转为新格式"""
    v4 = get_advice_v4(snapshot)
    # 映射旧接口期望的字段
    return {
        "short_term_actions": [],
        "long_term_actions": [],
        "position_reviews": [
            {"position": r.get("symbol", ""), "action": r.get("verdict", "HOLD"),
             "confidence": r.get("confidence", 0.5), "reason": r.get("reasoning", "")[:100]}
            for r in v4.get("position_reviews", [])
        ],
        "veto_map": {},
        "portfolio_health": v4.get("cio", {}).get("risk_level", "UNKNOWN"),
        "cio_market_read": v4.get("cio", {}).get("reasoning", ""),
        "risk_notes": v4.get("summary", ""),
        "market_read": v4.get("cio", {}).get("reasoning", ""),
        "cycle_summary": v4.get("summary", ""),
    }
