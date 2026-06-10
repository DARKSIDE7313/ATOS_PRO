"""
ATOS PRO v2 — 自动调试与自修复系统
====================================
功能：
  1. 全量语法检查 — 所有 .py 文件逐个编译
  2. 导入链验证 — 确认所有 import 都能找到
  3. 关键路径测试 — live_trader/shadow_trader/daily_pipeline 能否加载
  4. API Key 检查 — DeepSeek 是否能连通
  5. 日志分析 — 扫描最近的错误
  6. 自动修复 — 可自动修复的常见问题

用法：
  python3 -m atos.debugger.auto_fix          # 只检查
  python3 -m atos.debugger.auto_fix --fix    # 自动修复
"""

import os
import sys
import json
import py_compile
import importlib
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # ATOS_PRO/
sys.path.insert(0, str(BASE))

LOG = []


def log(level: str, msg: str):
    prefix = {"OK": "✅", "WARN": "⚠️", "ERR": "🔴", "FIX": "🔧", "INFO": "ℹ️"}
    line = f"{prefix.get(level, '·')} [{level}] {msg}"
    LOG.append(line)
    print(line)


def check_syntax() -> bool:
    """1. 全量语法检查"""
    log("INFO", "检查所有 .py 文件语法...")
    all_ok = True
    py_files = list(BASE.rglob("*.py"))
    py_files = [f for f in py_files if "venv" not in str(f) and "__pycache__" not in str(f)]
    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            log("ERR", f"语法错误: {f.relative_to(BASE)} → {e}")
            all_ok = False
    if all_ok:
        log("OK", f"语法检查通过 ({len(py_files)} 个文件)")
    return all_ok


def check_imports() -> dict:
    """2. 核心模块导入验证"""
    log("INFO", "验证核心模块导入...")
    modules = {
        "core": "atos.core",
        "factors": "atos.factors",
        "ai": "atos.ai",
        "portfolio": "atos.portfolio",
        "risk": "atos.risk.advanced",
        "shadow": "atos.shadow",
        "iterate": "atos.iterate",
        "live_trader": "atos.live.live_trader",
    }
    results = {}
    for name, mod in modules.items():
        try:
            importlib.import_module(mod)
            results[name] = True
        except Exception as e:
            log("ERR", f"导入失败 {name}: {str(e)[:80]}")
            results[name] = False
    ok = all(results.values())
    if ok:
        log("OK", f"核心模块导入全部通过 ({len(results)}个)")
    return results


def check_api_key() -> dict:
    """3. API 连接检查"""
    log("INFO", "检查 DeepSeek API 连接...")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        log("ERR", "DEEPSEEK_API_KEY 未设置")
        return {"ok": False, "key_set": False}

    import requests
    try:
        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            log("OK", f"DeepSeek API 连接正常 (余额可通过 platform.deepseek.com 查看)")
            return {"ok": True, "key_set": True}
        else:
            log("WARN", f"API 返回 {resp.status_code}: {resp.text[:80]}")
            return {"ok": False, "key_set": True, "status": resp.status_code}
    except Exception as e:
        log("ERR", f"API 连接失败: {e}")
        return {"ok": False, "key_set": True, "error": str(e)}


def check_futu_readiness() -> dict:
    """4. FutuOpenD 接入就绪检查"""
    log("INFO", "检查 FutuOpenD 接入就绪状态...")
    issues = []

    # 检查 futu-api 是否安装
    try:
        import futu
        log("OK", "futu-api 已安装")
    except ImportError:
        issues.append("futu-api 未安装 → pip install futu-api")
        log("ERR", issues[-1])

    # 检查端口可达性
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 11111), timeout=2)
        s.close()
        log("OK", "FutuOpenD 端口 11111 可达")
    except Exception:
        issues.append("FutuOpenD 未运行或端口 11111 不可达 → open -a FutuOpenD")
        log("WARN", issues[-1])

    # 检查夜间不要连（收盘后 FutuOpenD 可能关）
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() >= 5:
        issues.append("今天是周末，FutuOpenD 可能未登录")
    elif now.hour < 13 or now.hour > 20:
        issues.append("非美股交易时间(UTC 13:30-20:00)，FutuOpenD 可能处于离线模式")

    return {"ready": len(issues) == 0, "issues": issues}


def check_log_errors() -> list:
    """5. 日志错误扫描"""
    log("INFO", "扫描近期日志错误...")
    log_dir = BASE / "logs"
    if not log_dir.exists():
        log("WARN", "logs 目录不存在")
        return []

    errors = []
    for log_file in sorted(log_dir.glob("atos_*.log"), reverse=True)[:3]:
        try:
            with open(log_file) as f:
                for line in f:
                    if "ERROR" in line or "Traceback" in line or "CRITICAL" in line:
                        errors.append(line.strip()[-120:])
        except Exception:
            pass

    if errors:
        log("WARN", f"发现 {len(errors)} 条错误")
        for e in errors[-5:]:
            print(f"     {e[:100]}")
    else:
        log("OK", "近期无错误")
    return errors


def auto_fix_common_issues():
    """6. 自动修复常见问题"""
    fixes = 0

    # 检查 .env 文件
    env_file = BASE / ".env"
    if env_file.exists():
        content = env_file.read_text()
        if "DEEPSEEK_API_KEY" in content:
            log("OK", ".env 已配置 API Key")
        else:
            env_file.write_text(f"DEEPSEEK_API_KEY={os.environ.get('DEEPSEEK_API_KEY', 'YOUR_KEY')}\n")
            log("FIX", "已补充 .env 文件")
            fixes += 1
    else:
        env_file.write_text(f"DEEPSEEK_API_KEY={os.environ.get('DEEPSEEK_API_KEY', 'YOUR_KEY')}\n")
        log("FIX", "已创建 .env 文件")
        fixes += 1

    # 确保目录存在
    for d in ["logs", "data", "reports", "reports/transparency"]:
        p = BASE / d
        if not p.exists():
            p.mkdir(parents=True)
            log("FIX", f"已创建目录: {d}")
            fixes += 1

    # 清理损坏的状态文件
    state_file = BASE / "data" / "shadow_state.json"
    if state_file.exists():
        try:
            with open(state_file) as f:
                json.load(f)
        except json.JSONDecodeError:
            state_file.unlink()
            log("FIX", "已清理损坏的状态文件")
            fixes += 1

    if fixes:
        log("OK", f"自动修复 {fixes} 个问题")
    else:
        log("OK", "无需修复")
    return fixes


def run_full_check(fix: bool = False) -> dict:
    """完整诊断报告"""
    print("=" * 55)
    print("ATOS PRO v2 自动诊断系统")
    print("=" * 55)
    print()

    results = {
        "syntax": check_syntax(),
        "imports": check_imports(),
        "api": check_api_key(),
        "futu": check_futu_readiness(),
        "log_errors": check_log_errors(),
    }

    if fix:
        print()
        log("INFO", "执行自动修复...")
        results["fixes"] = auto_fix_common_issues()

    print()
    print("=" * 55)
    all_ok = (
        results["syntax"]
        and all(results["imports"].values())
        and results["api"]["ok"]
    )
    grade = "A" if all_ok else ("B" if results["syntax"] else "C")
    print(f"总评: {grade} | 语法: {'✅' if results['syntax'] else '❌'} | "
          f"导入: {'✅' if all(results['imports'].values()) else '❌'} | "
          f"API: {'✅' if results['api']['ok'] else '❌'} | "
          f"Futu: {'✅' if results['futu']['ready'] else '⚠️'}")
    print("=" * 55)

    return results


if __name__ == "__main__":
    run_full_check(fix="--fix" in sys.argv)
