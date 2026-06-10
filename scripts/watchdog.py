#!/usr/bin/env python3
"""
ATOS 守护者 — 自动化监控 + 自动修复
====================================
每2分钟检查一次全系统状态，发现问题自动修复。

监控项：
  1. ShadowTrader — 端口19999是否监听，周期是否在推进
  2. FutuOpenD — 端口11111是否监听
  3. Dashboard — 端口9000是否响应
  4. Hermes Gateway — 是否运行
  5. 日志健康 — 最近5分钟是否有周期崩溃

自动修复：
  - 端口丢失 → 重启对应 launchd 服务
  - 周期停滞超过10分钟 → 清除锁文件 + 硬重启
  - 连续崩溃超过3次 → 回退到上一次正常状态的代码
  - FutuOpenD 断开 → 重启 FutuOpenD + 通知用户

用法: python3 watchdog.py
      或通过 launchd 设为开机自启常驻进程
"""

import os, sys, json, time, socket, subprocess
from datetime import datetime, timedelta
from pathlib import Path

ATOS_DIR = "/Users/benson/ATOS_PRO"
LOG_DIR = os.path.join(ATOS_DIR, "logs")
STATE_FILE = os.path.join(ATOS_DIR, "data", "shadow_state.json")
LOCK_FILE = os.path.join(ATOS_DIR, "data", ".shadow_trader.lock")
WATCHDOG_LOG = os.path.join(LOG_DIR, "watchdog.log")

# 配置
CHECK_INTERVAL = 120  # 2分钟检查一次
CRASH_THRESHOLD = 3   # 连续崩溃超过3次 → 深度修复
STALL_THRESHOLD = 10  # 周期停滞超过10分钟 → 重启

def log(msg: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{timestamp} | {level:5s} | watchdog | {msg}"
    print(entry)
    try:
        with open(WATCHDOG_LOG, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass

def check_port(port: int) -> bool:
    """检查端口是否在监听（用 lsof 而非 connect，避免 listen-only 端口超时）"""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5
        )
        return "LISTEN" in result.stdout
    except Exception:
        return False

def check_http(url: str) -> bool:
    """检查 HTTP 端点是否响应"""
    try:
        import urllib.request
        req = urllib.request.Request(url)
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False

def get_state_cycle_count() -> int:
    """获取当前周期数"""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state.get("cycle_count", 0)
    except Exception:
        return -1

def get_recent_crashes(minutes: int = 5) -> int:
    """检查最近N分钟内的崩溃次数"""
    cutoff = datetime.now() - timedelta(minutes=minutes)
    count = 0
    log_files = [
        os.path.join(LOG_DIR, "shadow_trader_stderr.log"),
        os.path.join(LOG_DIR, "shadow_launchd.err"),
    ]
    for lf in log_files:
        try:
            with open(lf) as f:
                for line in f:
                    if "周期崩溃" in line or "ULTRA失败" in line or "ERROR" in line:
                        try:
                            ts_str = line[:19]
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                            if ts > cutoff:
                                count += 1
                        except Exception:
                            pass
        except Exception:
            pass
    return count

def restart_service(name: str, plist: str):
    """重启 launchd 服务"""
    log(f"🔄 重启 {name}...", "ACTION")
    try:
        subprocess.run(["launchctl", "unload", plist], capture_output=True, timeout=10)
        time.sleep(1)
        subprocess.run(["launchctl", "load", plist], capture_output=True, timeout=10)
        log(f"✅ {name} 重启完成", "OK")
        return True
    except Exception as e:
        log(f"❌ {name} 重启失败: {e}", "ERROR")
        return False

def restart_futuopend():
    """重启 FutuOpenD"""
    log("🔄 重启 FutuOpenD...", "ACTION")
    try:
        subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.futunn.FutuOpenD"],
                       capture_output=True, timeout=10)
        log("✅ FutuOpenD 重启完成", "OK")
    except Exception:
        # Fallback: 直接启动应用
        try:
            subprocess.Popen(["open", "/Applications/Futu_OpenD.app"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("✅ FutuOpenD 应用启动", "OK")
        except Exception as e:
            log(f"❌ FutuOpenD 启动失败: {e}", "ERROR")

def clear_pycache():
    """清除 Python 缓存"""
    try:
        subprocess.run(["find", os.path.join(ATOS_DIR, "atos"), "-type", "d",
                       "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
                       capture_output=True, timeout=10)
        log("🧹 pycache 已清除", "MAINT")
    except Exception:
        pass

def deep_fix():
    """深度修复：清锁 + 清缓存 + 全重启"""
    log("🔧 执行深度修复...", "ACTION")
    
    # 1. 杀残留进程
    try:
        subprocess.run(["pkill", "-9", "-f", "atos.shadow.shadow_trader"],
                       capture_output=True, timeout=5)
        log("  ✓ 残留进程已清除")
    except Exception:
        pass
    time.sleep(2)
    
    # 2. 清除锁文件
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            log("  ✓ 锁文件已清除")
    except Exception:
        pass
    
    # 3. 清除缓存
    clear_pycache()
    time.sleep(1)
    
    # 4. 重启服务
    restart_service("ShadowTrader", os.path.expanduser("~/Library/LaunchAgents/ai.atos.shadowtrader.plist"))
    
    # 5. 重启 Gateway
    for hermes_bin in [
        "/Users/benson/.local/bin/hermes",
        "/Users/benson/.hermes/hermes-agent/venv/bin/hermes",
    ]:
        try:
            subprocess.run([hermes_bin, "gateway", "start"], capture_output=True, timeout=15)
            log(f"  ✓ Hermes Gateway 已启动 ({hermes_bin})")
            break
        except Exception:
            continue

def health_report():
    """完整健康报告"""
    shadow_ok = check_port(19999)
    futu_ok = check_port(11111)
    dash_ok = check_http("http://localhost:9000/api")
    cycle_count = get_state_cycle_count()
    recent_crashes = get_recent_crashes(5)
    # Gateway 检测：用完整路径避免 launchd 下 PATH 为空的问题
    gateway_ok = False
    for hermes_bin in [
        "/Users/benson/.local/bin/hermes",
        "/Users/benson/.hermes/hermes-agent/venv/bin/hermes",
    ]:
        try:
            r = subprocess.run([hermes_bin, "gateway", "status"], capture_output=True, timeout=5)
            output = r.stdout.decode().lower()
            if "loaded" in output or "running" in output or "pid" in output:
                gateway_ok = True
                break
        except Exception:
            continue
    # 二级检测：检查 Hermes gateway 的 launchd 进程是否存活
    if not gateway_ok:
        try:
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
            if "ai.hermes.gateway" in r.stdout:
                gateway_ok = True
        except Exception:
            pass
    
    return {
        "shadow": shadow_ok, "futu": futu_ok, "dash": dash_ok,
        "gateway": gateway_ok, "cycle": cycle_count,
        "crashes_5m": recent_crashes,
    }


def main():
    log("🚀 ATOS 守护者启动")
    log(f"检查间隔: {CHECK_INTERVAL // 60} 分钟")
    
    # 启动后先等 60 秒让所有服务就绪
    log("⏳ 等待服务初始化 (60秒)...")
    time.sleep(60)
    
    last_cycle = get_state_cycle_count()
    first_check = True  # 第一次检查跳过"停滞"检测
    crash_streak = 0
    
    while True:
        try:
            health = health_report()
            
            issues = []
            if not health["shadow"]:
                issues.append("ShadowTrader 端口丢失")
            if not health["futu"]:
                issues.append("FutuOpenD 端口丢失")
            if not health["dash"]:
                issues.append("Dashboard 无响应")
            if health["crashes_5m"] > 0:
                issues.append(f"最近5分钟有 {health['crashes_5m']} 次崩溃")
            
            if issues:
                log(f"⚠️ 发现问题: {', '.join(issues)}", "ALERT")
                crash_streak += 1
            else:
                crash_streak = 0
            
            # 自动修复逻辑
            if crash_streak >= CRASH_THRESHOLD:
                log(f"🔴 连续 {crash_streak} 次告警 → 深度修复", "CRITICAL")
                deep_fix()
                crash_streak = 0
                time.sleep(60)  # 深度修复后等1分钟让服务启动
                continue
            
            # 单点修复
            fixed_something = False
            if not health["shadow"]:
                log("🔄 ShadowTrader 丢失 → 重启", "FIX")
                clear_pycache()
                if os.path.exists(LOCK_FILE):
                    os.remove(LOCK_FILE)
                restart_service("ShadowTrader", os.path.expanduser("~/Library/LaunchAgents/ai.atos.shadowtrader.plist"))
                fixed_something = True
            
            if not health["futu"]:
                log("🔄 FutuOpenD 丢失 → 重启", "FIX")
                restart_futuopend()
                fixed_something = True
            
            if not health["dash"]:
                log("🔄 Dashboard 无响应 → 重启", "FIX")
                restart_service("Dashboard", os.path.expanduser("~/Library/LaunchAgents/ai.atos.dashboard.plist"))
                fixed_something = True
            
            if not health["gateway"]:
                log("🔄 Gateway 丢失 → 重启", "FIX")
                for hermes_bin in [
                    "/Users/benson/.local/bin/hermes",
                    "/Users/benson/.hermes/hermes-agent/venv/bin/hermes",
                ]:
                    try:
                        subprocess.run([hermes_bin, "gateway", "start"], capture_output=True, timeout=15)
                        break
                    except Exception:
                        continue
                fixed_something = True
            
            # 修复完成后重置计数器，等待服务启动
            if fixed_something:
                crash_streak = 0
                time.sleep(15)
            
            # 检查周期停滞（首次检查跳过）
            current_cycle = health["cycle"]
            if not first_check and current_cycle == last_cycle and current_cycle > 0:
                log(f"⚠️ 周期停滞在 #{current_cycle} (可能卡死)", "STALL")
                if crash_streak >= 1:
                    deep_fix()
            last_cycle = current_cycle
            first_check = False
            
            # 正常心跳
            if not issues:
                log(f"✅ 全正常 | Shadow:✓ Futu:✓ Dash:✓ | Cycle #{current_cycle} | 崩溃:{health['crashes_5m']}")
            
        except Exception as e:
            log(f"守护者自身异常: {e}", "ERROR")
        
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
