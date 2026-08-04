"""
ATOS AutoPilot — AI 调试引擎
============================
使用 DeepSeek API 分析错误并生成修复方案。

流程:
  1. 接收错误上下文（日志+代码+系统状态）
  2. 先查知识库，命中则直接返回
  3. 未命中则调用 DeepSeek AI 分析
  4. AI 返回: 诊断 + 修复方案 + 风险等级
  5. 结果存入知识库供下次使用
"""

import os, json, re, time, urllib.request
from typing import Optional
from atos.autopilot.knowledge_base import (
    lookup_error, record_error, record_fix, match_pattern
)
from atos.core.logging import get_logger

logger = get_logger("autopilot.ai_debugger")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 可自动应用的安全修复类型
SAFE_FIX_TYPES = {
    "clear_cache": "清除缓存文件",
    "restart_module": "重启故障模块",
    "sleep_retry": "等待后重试",
    "reduce_freq": "降低调用频率",
    "skip_cycle": "跳过当前周期",
    "reset_kelly": "重置 Kelly 统计",
    "adjust_param": "调整配置参数",
}

# 需要人工确认的修复类型
RISKY_FIX_TYPES = {
    "code_patch": "代码修改",
    "config_change": "配置变更",
    "restart_futu": "重启 FutuOpenD",
    "install_deps": "安装依赖包",
}


def _read_relevant_code(error_module: str = "") -> str:
    """读取与错误相关的源代码片段"""
    base = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    if not error_module:
        return ""

    # 尝试找到对应文件
    parts = error_module.split(".")
    possible_paths = [
        os.path.join(base, *parts) + ".py",
        os.path.join(base, "atos", *parts[1:]) + ".py" if len(parts) > 1 else "",
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    lines = f.readlines()
                # 返回前 200 行（通常包含 import 和主要逻辑）
                return "".join(lines[:200])
            except Exception:
                pass

    return ""


def _call_deepseek(prompt: str) -> str:
    """调用 DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        return "DeepSeek API Key 未配置，无法进行 AI 分析"

    try:
        data = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是 ATOS 量化交易系统的 AI 运维工程师。你精通 Python、量化交易系统、金融数据API（yfinance/FutuOpenD）、风控系统。请用中文回复，简洁专业。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 800,
        }).encode()

        req = urllib.request.Request(
            f"{DEEPSEEK_BASE}/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]

    except Exception as e:
        logger.error(f"DeepSeek API 调用失败: {e}")
        return f"AI 分析暂时不可用: {str(e)[:100]}"


def _parse_ai_response(response: str) -> dict:
    """从 AI 响应中解析结构化信息"""
    result = {
        "diagnosis": response[:500],
        "root_cause": "",
        "fix_suggestion": "",
        "fix_code": "",
        "fix_type": "manual",
        "risk_level": "safe",
        "can_auto_fix": False,
    }

    # 提取诊断
    diag_match = re.search(r'(?:诊断|根因|问题)[：:]\s*(.+?)(?:\n|$)', response)
    if diag_match:
        result["root_cause"] = diag_match.group(1).strip()

    # 提取修复方案
    fix_match = re.search(r'(?:修复|方案|解决)[：:]\s*(.+?)(?:\n|$)', response)
    if fix_match:
        result["fix_suggestion"] = fix_match.group(1).strip()

    # 提取代码
    code_match = re.search(r'```(?:python|bash)?\n(.+?)\n```', response, re.DOTALL)
    if code_match:
        result["fix_code"] = code_match.group(1).strip()

    # 判断风险等级
    if any(w in response.lower() for w in ["重启", "重启 futu", "restart", "reboot"]):
        result["risk_level"] = "risky"
    if any(w in response.lower() for w in ["删除", "drop table", "rm -rf", "删除持仓"]):
        result["risk_level"] = "dangerous"
    if any(w in response.lower() for w in ["修改代码", "代码修改", "patch", "修改.*py"]):
        result["risk_level"] = "risky"

    # 判断修复类型
    for fix_type, desc in {**SAFE_FIX_TYPES, **RISKY_FIX_TYPES}.items():
        if fix_type in response.lower() or desc in response:
            result["fix_type"] = fix_type
            break

    # 判断是否可自动修复
    result["can_auto_fix"] = (
        result["fix_type"] in SAFE_FIX_TYPES and
        result["risk_level"] == "safe"
    )

    return result


def analyze_error(
    error_type: str,
    error_message: str,
    module: str = "",
    stack_trace: str = "",
    log_context: str = "",
    system_state: dict = None,
) -> dict:
    """
    分析错误并生成修复方案。

    Args:
        error_type: 错误类型 (Exception/ERROR/WARNING)
        error_message: 错误消息
        module: 发生错误的模块
        stack_trace: 堆栈跟踪
        log_context: 上下文日志
        system_state: 当前系统状态

    Returns:
        {
            "known": bool,         # 是否已知错误
            "error_hash": str,     # 错误哈希
            "diagnosis": str,      # AI 诊断
            "root_cause": str,     # 根因
            "fix_suggestion": str, # 修复建议
            "fix_type": str,       # 修复类型
            "risk_level": str,     # 风险等级
            "can_auto_fix": bool,  # 是否可自动修复
            "fix_code": str,       # 修复代码
        }
    """
    # 1. 知识库查找
    known = lookup_error(error_type, error_message, module)
    error_hash = record_error(error_type, error_message, module, stack_trace,
                             "high" if error_type == "CRITICAL" else "medium")

    if known["known"] and known["fixes"]:
        best_fix = known["fixes"][0]
        logger.info(f"📚 知识库命中: {error_hash} → {best_fix['fix_type']}")
        return {
            "known": True,
            "error_hash": error_hash,
            "diagnosis": f"已知错误（第{known['error']['occurrence_count']}次）",
            "root_cause": known["error"]["error_message"],
            "fix_suggestion": best_fix["fix_description"],
            "fix_type": best_fix["fix_type"],
            "risk_level": best_fix["risk_level"],
            "can_auto_fix": best_fix["fix_type"] in SAFE_FIX_TYPES,
            "fix_code": best_fix.get("fix_code", ""),
            "fix_id": best_fix["id"],
        }

    # 2. 模式匹配
    combined = f"{error_type}: {error_message} {stack_trace[:200]}"
    pattern = match_pattern(combined)
    if pattern:
        logger.info(f"🔍 模式匹配: {pattern['pattern_name']} → {pattern['action']}")
        return {
            "known": True,
            "error_hash": error_hash,
            "diagnosis": f"模式匹配: {pattern['pattern_name']}",
            "root_cause": pattern.get("notes", ""),
            "fix_suggestion": pattern.get("notes", ""),
            "fix_type": pattern["action"],
            "risk_level": pattern["severity"],
            "can_auto_fix": pattern["action"] in SAFE_FIX_TYPES,
            "fix_code": "",
        }

    # 3. AI 分析
    logger.info(f"🤖 调用 AI 分析: {error_type} in {module}")

    code_snippet = _read_relevant_code(module)
    state_str = json.dumps(system_state or {}, indent=2, ensure_ascii=False)

    prompt = f"""ATOS 交易系统发生错误，请诊断并建议修复方案。

【错误类型】{error_type}
【错误模块】{module}
【错误消息】{error_message[:500]}
【堆栈追踪】
{stack_trace[:1000]}

【上下文日志】
{log_context[:800]}

【系统状态】
{state_str[:500]}

【相关代码】
{code_snippet[:1500]}

请按以下格式回复：
诊断：[根因分析]
修复：[具体修复步骤]
代码：[如果需要代码修改]

注意：
- 安全修复标记为 SAFE（如清缓存、调参数、跳周期）
- 风险修复标记为 RISKY（如重启服务、改代码）
- 危险操作标记为 DANGEROUS（如删数据）"""

    ai_response = _call_deepseek(prompt)
    result = _parse_ai_response(ai_response)
    result["known"] = False
    result["error_hash"] = error_hash
    result["diagnosis"] = ai_response[:500]

    # 4. 存入知识库
    record_fix(error_hash, result["fix_type"], result["fix_suggestion"],
               result.get("fix_code", ""), result["risk_level"])

    return result


def quick_check(log_line: str) -> Optional[dict]:
    """快速检查单行日志，看是否需要 AI 分析"""
    # 只对 ERROR 和 CRITICAL 级别进行 AI 分析
    if not any(level in log_line for level in ["ERROR", "CRITICAL", "Exception",
                                                "Traceback", "traceback"]):
        return None

    # 先模式匹配
    pattern = match_pattern(log_line)
    if pattern:
        return {
            "need_ai": pattern["action"] == "ai_analyze",
            "pattern": pattern,
            "log_line": log_line,
        }

    # 需要 AI 分析
    return {
        "need_ai": True,
        "pattern": None,
        "log_line": log_line,
    }
