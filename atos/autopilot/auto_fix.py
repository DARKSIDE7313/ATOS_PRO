"""
ATOS AutoPilot — 自动修复执行器
===============================
根据 AI 诊断结果执行安全修复。

安全修复（自动执行）:
  - clear_cache: 清除 yfinance / __pycache__ 缓存
  - restart_module: 重新加载故障模块
  - sleep_retry: 等待后重试
  - reduce_freq: 降低 API 调用频率
  - skip_cycle: 跳过当前交易周期
  - reset_kelly: 重置 Kelly 统计数据

风险修复（标记待审核）:
  - code_patch / config_change / restart_futu / install_deps
"""

import os, sys, json, time, glob, shutil, subprocess
from typing import Optional
from atos.core.logging import get_logger

logger = get_logger("autopilot.auto_fix")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# 修复历史文件
FIX_LOG_PATH = os.path.join(BASE_DIR, "data", "autopilot_fixes.json")


def _log_fix(error_hash: str, fix_type: str, result: str, details: str = ""):
    """记录修复历史"""
    os.makedirs(os.path.dirname(FIX_LOG_PATH), exist_ok=True)
    try:
        if os.path.exists(FIX_LOG_PATH):
            with open(FIX_LOG_PATH) as f:
                history = json.load(f)
        else:
            history = []
    except Exception:
        history = []

    history.append({
        "time": __import__('datetime').datetime.now().isoformat(),
        "error_hash": error_hash,
        "fix_type": fix_type,
        "result": result,
        "details": details,
    })

    # 只保留最近 200 条
    history = history[-200:]

    with open(FIX_LOG_PATH, "w") as f:
        json.dump(history, f, indent=2)


def safe_fix(error_hash: str, fix_type: str, fix_code: str = "",
             error_context: dict = None) -> dict:
    """
    执行安全修复。

    Returns:
        {"success": bool, "message": str, "action": str}
    """
    result = {"success": False, "message": "", "action": fix_type}

    try:
        if fix_type == "clear_cache":
            result = _fix_clear_cache()

        elif fix_type == "restart_module":
            result = _fix_restart_module(error_context)

        elif fix_type == "sleep_retry":
            result = _fix_sleep_retry()

        elif fix_type == "reduce_freq":
            result = _fix_reduce_freq()

        elif fix_type == "skip_cycle":
            result = _fix_skip_cycle()

        elif fix_type == "reset_kelly":
            result = _fix_reset_kelly()

        elif fix_type == "adjust_param":
            result = _fix_adjust_param(fix_code, error_context)

        else:
            result["message"] = f"修复类型 '{fix_type}' 需要人工审核"
            result["action"] = "manual_review"

    except Exception as e:
        result["success"] = False
        result["message"] = f"自动修复失败: {e}"

    _log_fix(error_hash, fix_type, "success" if result["success"] else "failed",
             result.get("message", ""))
    return result


def _fix_clear_cache() -> dict:
    """清除各种缓存"""
    cleaned = []

    # yfinance 缓存
    yf_cache = os.path.expanduser("~/Library/Caches/py-yfinance")
    if os.path.exists(yf_cache):
        for f in glob.glob(os.path.join(yf_cache, "*.db-wal")):
            try:
                os.remove(f)
                cleaned.append(f"yf_wal:{os.path.basename(f)}")
            except Exception:
                pass
        for f in glob.glob(os.path.join(yf_cache, "*.db-shm")):
            try:
                os.remove(f)
                cleaned.append(f"yf_shm:{os.path.basename(f)}")
            except Exception:
                pass

    # Python cache
    for root, dirs, files in os.walk(BASE_DIR):
        if "__pycache__" in dirs:
            try:
                shutil.rmtree(os.path.join(root, "__pycache__"))
                cleaned.append(f"pycache:{os.path.relpath(root, BASE_DIR)}")
            except Exception:
                pass

    logger.info(f"🧹 清除缓存: {len(cleaned)} 项")
    return {"success": True, "message": f"清除了 {len(cleaned)} 个缓存文件", "action": "clear_cache"}


def _fix_restart_module(error_context: dict = None) -> dict:
    """重启故障模块（通过重新导入）"""
    module_name = (error_context or {}).get("module", "")
    if module_name:
        # 从 sys.modules 中移除，强制下次重新加载
        for key in list(sys.modules.keys()):
            if module_name in key:
                del sys.modules[key]
                logger.info(f"🔄 卸载模块: {key}")
        return {"success": True, "message": f"模块 {module_name} 已卸载，下次自动重新加载",
                "action": "restart_module"}
    return {"success": False, "message": "未指定模块名", "action": "restart_module"}


def _fix_sleep_retry() -> dict:
    """创建重试标记"""
    retry_file = "/tmp/atos_retry_pending"
    with open(retry_file, "w") as f:
        f.write(str(time.time()))
    logger.info("⏳ 设置重试标记")
    return {"success": True, "message": "重试标记已设置，系统将在下个周期重试",
            "action": "sleep_retry"}


def _fix_reduce_freq() -> dict:
    """降低 API 调用频率"""
    rate_file = os.path.join(BASE_DIR, "data", ".rate_limit")
    current_rate = 1.0
    try:
        if os.path.exists(rate_file):
            with open(rate_file) as f:
                current_rate = float(f.read().strip())
    except Exception:
        pass

    new_rate = min(current_rate * 2.0, 10.0)
    with open(rate_file, "w") as f:
        f.write(str(new_rate))

    logger.info(f"🐢 降频: {current_rate}x → {new_rate}x")
    return {"success": True, "message": f"API 调用频率已降低到 {new_rate}x",
            "action": "reduce_freq"}


def _fix_skip_cycle() -> dict:
    """创建跳过标记"""
    skip_file = "/tmp/atos_skip_next_cycle"
    with open(skip_file, "w") as f:
        f.write(str(time.time()))
    logger.info("⏭ 下个周期跳过标记已设置")
    return {"success": True, "message": "下个交易周期将被跳过", "action": "skip_cycle"}


def _fix_reset_kelly() -> dict:
    """重置 Kelly 统计数据"""
    stats_path = os.path.join(BASE_DIR, "data", "trade_stats.json")
    backup_path = stats_path + ".bak"

    try:
        if os.path.exists(stats_path):
            os.rename(stats_path, backup_path)
            logger.info("🔄 Kelly 统计已重置（旧数据已备份）")
            return {"success": True, "message": "Kelly 统计已重置", "action": "reset_kelly"}
    except Exception as e:
        return {"success": False, "message": f"重置失败: {e}", "action": "reset_kelly"}


def _fix_adjust_param(fix_code: str, error_context: dict = None) -> dict:
    """调整配置参数（安全参数修改）"""
    if not fix_code:
        return {"success": False, "message": "未提供参数修改代码", "action": "adjust_param"}

    # 只允许简单的参数修改（不做复杂的代码注入）
    if "=" in fix_code and len(fix_code) < 200:
        logger.info(f"⚙️ 参数调整建议: {fix_code}")
        return {"success": True, "message": f"参数调整已记录: {fix_code}（需人工确认）",
                "action": "adjust_param"}

    return {"success": False, "message": "参数调整代码格式不支持",
            "action": "adjust_param"}


def get_fix_history(limit: int = 20) -> list:
    """获取修复历史"""
    try:
        if os.path.exists(FIX_LOG_PATH):
            with open(FIX_LOG_PATH) as f:
                history = json.load(f)
            return history[-limit:]
    except Exception:
        pass
    return []
