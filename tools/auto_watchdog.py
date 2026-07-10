#!/usr/bin/env python3
"""
ATOS PRO v3 — 自监管看门狗（v2）
==================================
每5分钟检查一次系统健康，自动修复已知问题。

监视项：
1. Shadow Trader 进程存活（端口19999）
2. FutuOpenD 连接状态（端口11111）
3. 日志中的错误模式（no such table、timeout、rate limit）
4. 权益变化异常（突然大幅亏损 > 5%）
5. 磁盘空间（< 200MB 告警）
6. yfinance 缓存健康（WAL/SHM 残留清理）
7. 自动代码修复（根据错误模式）

执行方式：
  1. 用 cronjob 每5分钟激活一个 agent 来检查
  2. agent 直接调用本脚本并读取输出
  3. 输出为空 = 一切正常；有输出 = 需要关注/修复

Usage:
  python3 /Users/ATOS_PRO/tools/auto_watchdog.py
"""

import os
import sys
import json
import time
import socket
import subprocess
import datetime
from collections import defaultdict  # Fix: 顶部导入，check_log_errors() L109 使用

ATOS_DIR = os.path.expanduser("/Users/benson/ATOS_PRO")
TIMESTAMP = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "INFO"):
    print(f"{TIMESTAMP} [{level}] {msg}")


# ════════════════════════════════════════════════════════════
# 1. 进程存活检查
# ════════════════════════════════════════════════════════════
def check_shadow_trader() -> bool:
    """检查 Shadow Trader 是否通过端口19999存活"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(("127.0.0.1", 19999))
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError):
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_futu_opend() -> bool:
    """检查 FutuOpenD 是否在端口11111上"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(("127.0.0.1", 11111))
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError):
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
# 2. 日志错误模式检测
# ════════════════════════════════════════════════════════════
def check_log_errors() -> list[str]:
    """检查最近的日志中是否有已知错误模式"""
    log_file = os.path.join(ATOS_DIR, "logs", "shadow_trader_stderr.log")
    if not os.path.exists(log_file):
        return []
    
    errors = []
    try:
        with open(log_file, "r") as f:
            # 只读最后500行
            lines = f.readlines()[-500:]
    except Exception:
        return []
    
    # 已知错误模式 → 修复建议
    error_patterns = {
        "no such table: _tz_kv": "yfinance缓存表名错误 → 重启即可自愈（已修复自愈函数）",
        "no such table: 'tkr-tz'": "yfinance缓存损坏 → 重启即可自愈",
        "unable to open database file": "yfinance SQLite被锁 → 清除 WAL/SHM 残留重启",
        "HTTP 429": "DeepSeek API限流 → 降频到30分钟周期",
        "402 Payment Required": "DeepSeek API 余额不足 → 需要充值",
        "Connection refused": "FutuOpenD 未运行 → 需启动 FutuOpenD",
        "SSLError": "网络问题 → 检查 VPN/代理",
        "Timeout": "请求超时 → 网络波动",
        "Error downloading": "yfinance下载失败 → 网络或缓存问题",
    }
    
    # 取最后10个错误模式的出现次数
    recent_errors = defaultdict(int)
    for line in lines[-200:]:  # 只检查最近200行
        for pattern, fix in error_patterns.items():
            if pattern in line:
                recent_errors[pattern] += 1
                break
    
    for pattern, count in recent_errors.items():
        if count >= 2:  # 同一个错误出现 >= 2 次才告警
            fix = error_patterns.get(pattern, "未知错误")
            errors.append(f"日志中 '{pattern}' 出现 {count} 次 → 修复建议: {fix}")
    
    return errors


# ════════════════════════════════════════════════════════════
# 3. 权益监控
# ════════════════════════════════════════════════════════════
def check_equity_health() -> list[str]:
    """检查 shadow_state.json 中的权益变化和回撤"""
    state_file = os.path.join(ATOS_DIR, "data", "shadow_state.json")
    if not os.path.exists(state_file):
        return []
    
    issues = []
    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception:
        return ["shadow_state.json 无法读取，可能损坏"]
    
    # 检查回撤
    equity = state.get("equity", 0)
    peak = state.get("peak_equity", equity)
    dd = (peak - equity) / peak if peak > 0 else 0
    
    if dd > 0.08:
        issues.append(f"⚠️ 回撤 {dd:.1%} > 8%，接近最大回撤线15%")
    if dd > 0.12:
        issues.append(f"🔴 回撤 {dd:.1%} > 12%，接近熔断线，建议关注持仓")
    
    # 检查冷却期过长的标的
    blacklist = state.get("stop_loss_blacklist", {})
    cycle_count = state.get("cycle_count", 0)
    if blacklist:
        long_cooled = [s for s, c in blacklist.items() if cycle_count - c > 96]  # >8小时
        if long_cooled:
            issues.append(f"冷却期异常: {long_cooled} 冷却>8小时，可能是BUG")
    
    return issues


# ════════════════════════════════════════════════════════════
# 4. 磁盘空间
# ════════════════════════════════════════════════════════════
def check_disk() -> list[str]:
    """检查磁盘空间"""
    try:
        stat = os.statvfs(ATOS_DIR)
        free_mb = stat.f_bavail * stat.f_frsize / (1024 * 1024)
        if free_mb < 200:
            return [f"⚠️ 磁盘剩余 {free_mb:.0f}MB < 200MB"]
        if free_mb < 500:
            return [f"🟡 磁盘剩余 {free_mb:.0f}MB < 500MB"]
    except Exception:
        pass
    return []


# ════════════════════════════════════════════════════════════
# 5. yfinance 缓存修复
# ════════════════════════════════════════════════════════════
def fix_yfinance_cache():
    """清理 yfinance 缓存中的 WAL/SHM 残留。返回是否修复了任何东西。"""
    import glob
    import sqlite3
    cache_dir = os.path.expanduser("~/Library/Caches/py-yfinance")
    if not os.path.exists(cache_dir):
        return False
    
    fixed = False
    for pattern in ("*.db-wal", "*.db-shm"):
        for f in glob.glob(os.path.join(cache_dir, pattern)):
            try:
                os.remove(f)
                log(f"已清除 yfinance 缓存残留 {os.path.basename(f)}", "WARN")
                fixed = True
            except OSError:
                pass
    
    # 检查数据库完整性
    for db_name in ("tkr-tz.db", "cookies.db"):
        db_path = os.path.join(cache_dir, db_name)
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA integrity_check")
                # 确保 yfinance 需要的所有表都存在
                for table in ('tkr-tz', 'cookie', '_cookieschema', '_tz_kv', 'metadata', 'cache'):
                    conn.execute(f"CREATE TABLE IF NOT EXISTS '{table}' (key TEXT PRIMARY KEY, value TEXT)")
                    # yfinance 1.4.1 需要额外列
                    for col in ['strategy', 'exchange']:
                        try:
                            conn.execute(f"ALTER TABLE '{table}' ADD COLUMN {col} TEXT DEFAULT ''")
                        except Exception:
                            pass
                conn.commit()
                conn.close()
            except Exception as e:
                try:
                    os.remove(db_path)
                    log(f"已删除损坏的 yfinance 缓存 {db_name}: {e}", "WARN")
                    fixed = True
                except OSError:
                    pass
    
    return fixed


# ════════════════════════════════════════════════════════════
# 6. 自动修复动作
# ════════════════════════════════════════════════════════════
def restart_shadow_trader():
    """通过 launchctl 重启 Shadow Trader"""
    log("重启 Shadow Trader...", "WARN")
    try:
        # kill current
        subprocess.run(
            ["launchctl", "kickstart", "-k", "gui/501/com.atos.shadowtrader"],
            capture_output=True, timeout=10
        )
        log("LaunchAgent kickstart 已发送", "INFO")
        return True
    except Exception as e:
        log(f"重启失败: {e}", "ERROR")
        return False


def clear_lock_file():
    """清除 lock 文件"""
    lock_file = os.path.join(ATOS_DIR, "data", ".shadow_trader.lock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            log("已清除 .shadow_trader.lock", "WARN")
            return True
        except Exception:
            pass
    return False


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════
def main():
    issues = []
    fixes_applied = []
    
    # 1. 检查进程
    if not check_shadow_trader():
        log("🔴 Shadow Trader 端口19999无响应", "ERROR")
        issues.append("Shadow Trader 端口无响应")
        # 尝试自动修复
        clear_lock_file()
        if restart_shadow_trader():
            fixes_applied.append("Shadow Trader 已重启")
    else:
        log("✅ Shadow Trader 运行中", "INFO")
    
    if not check_futu_opend():
        log("🔴 FutuOpenD 端口11111无响应", "ERROR")
        issues.append("FutuOpenD 未运行")
    else:
        log("✅ FutuOpenD 运行中", "INFO")
    
    # 2. 日志错误检测
    log_errors = check_log_errors()
    for err in log_errors:
        issues.append(f"[LOG] {err}")
    
    # 3. 权益健康
    equity_issues = check_equity_health()
    issues.extend(equity_issues)
    
    # 4. 磁盘
    disk_issues = check_disk()
    issues.extend(disk_issues)
    
    # 5. yfinance 缓存自愈
    if fix_yfinance_cache():
        fixes_applied.append("yfinance 缓存已修复")
    
    # 6. 检查并清理过大的日志文件（> 100MB 自动轮转）
    log_file = os.path.join(ATOS_DIR, "logs", "shadow_trader_stderr.log")
    if os.path.exists(log_file) and os.path.getsize(log_file) > 100 * 1024 * 1024:
        # 备份并清空
        backup = log_file + ".bak"
        try:
            os.rename(log_file, backup)
            fixes_applied.append(f"日志>100MB已轮转: {os.path.basename(backup)}")
        except Exception:
            pass
    
    # 输出
    if issues:
        log(f"\n====== 发现问题: {len(issues)} 项 ======", "WARN")
        for i, issue in enumerate(issues, 1):
            log(f"  {i}. {issue}", "WARN")
    
    if fixes_applied:
        log(f"\n====== 已执行修复: {len(fixes_applied)} 项 ======", "INFO")
        for fix in fixes_applied:
            log(f"  ✅ {fix}", "INFO")
    
    if not issues and not fixes_applied:
        log("✅ 一切正常，无需处理", "INFO")
    
    # 返回非0意味着有问题——cron 可以用这个判断
    return len(issues)


if __name__ == "__main__":
    sys.exit(main())
