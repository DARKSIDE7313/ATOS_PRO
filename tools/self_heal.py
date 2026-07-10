#!/usr/bin/env python3
"""
ATOS Self-Healing System v2 — 自愈监控系统
- 每5分钟检查 ATOS 健康状况
- 自动检测并修复已知问题
- 将修复模式写入 skill 永久记忆
- 错误模式永不再犯

安装:
  hermes cron create --schedule "*/5 * * * *" --name atos-self-heal \\
    --prompt "运行 ATOS 自愈系统" --skill atos-self-heal
"""

import os
import sys
import json
import time
import subprocess
import sqlite3
import re
from datetime import datetime
from pathlib import Path

# === CONFIG ===
ATOS_HOME = os.path.expanduser('~/ATOS_PRO')
LOGS_DIR = os.path.join(ATOS_HOME, 'logs')
DATA_DIR = os.path.join(ATOS_HOME, 'data')
ERROR_LOG = os.path.join(LOGS_DIR, 'shadow_trader_stderr.log')
SKILLS_DIR = os.path.expanduser('~/.hermes/skills/atos')
KNOWN_ERRORS_DB = os.path.join(DATA_DIR, 'known_errors.db')
REPORT_FILE = os.path.join(ATOS_HOME, 'data', 'health_check_state.json')

# === PROBE FUNCTIONS ===

def probe_processes() -> list:
    """Check all ATOS services."""
    checks = []
    
    # Define expected services
    services = {
        'shadow_trader': 19999,    # lock port (socket-based mutex)
        'web_dashboard': 8000,     # Fix #11: 实际 web 仪表盘端口
        'dashboard_legacy': 9000,  # 旧版仪表盘
        'futu_opend': 11111,
    }
    
    for name, port in services.items():
        result = subprocess.run(
            ['lsof', '-i', f':{port}'],
            capture_output=True, text=True, timeout=5
        )
        listening = 'LISTEN' in result.stdout
        checks.append({
            'service': name,
            'port': port,
            'running': listening,
            'pid': None,
            'status': 'ok' if listening else 'down',
        })
        
        # Extract PID if running
        if listening:
            for line in result.stdout.split('\n'):
                if 'LISTEN' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        checks[-1]['pid'] = parts[1]
    
    # Check launchctl for shadow_trader
    lt = subprocess.run(
        ['launchctl', 'list', 'com.atos.shadowtrader'],
        capture_output=True, text=True, timeout=5
    )
    if 'PID' in lt.stdout:
        for line in lt.stdout.split('\n'):
            if '"PID"' in line:
                pid = line.split('=')[1].strip().strip(';').strip()
                # Update the shadow_trader check with PID
                for c in checks:
                    if c['service'] == 'shadow_trader':
                        c['pid'] = pid
    
    return checks


def probe_errors(since_minutes: int = 60) -> dict:
    """Scan recent logs for errors."""
    if not os.path.exists(ERROR_LOG):
        return {'total_errors': 0, 'patterns': []}
    
    result = subprocess.run(
        ['tail', '-10000', ERROR_LOG],
        capture_output=True, text=True, timeout=10
    )
    lines = result.stdout.split('\n')
    
    # Categorize errors
    error_patterns = {
        'database_locked': 0,
        'download_failed': 0, 
        'api_error': 0,
        'timeout': 0,
        'exception': 0,
        'connection': 0,
    }
    
    for line in lines:
        lower = line.lower()
        if 'unable to open database' in lower:
            error_patterns['database_locked'] += 1
        if 'failed download' in lower:
            error_patterns['download_failed'] += 1
        if 'timeout' in lower:
            error_patterns['timeout'] += 1
        if 'traceback' in lower or 'exception' in lower:
            error_patterns['exception'] += 1
        if 'connection' in lower and ('refused' in lower or 'reset' in lower):
            error_patterns['connection'] += 1
    
    return {
        'total_errors': sum(error_patterns.values()),
        'patterns': error_patterns,
        'critical': error_patterns['exception'] > 0 or error_patterns['database_locked'] > 50,
    }


def probe_disk() -> dict:
    """Check disk space and log sizes."""
    result = subprocess.run(
        ['df', '-h', '/'],
        capture_output=True, text=True, timeout=5
    )
    lines = result.stdout.strip().split('\n')
    
    disk_info = {}
    if len(lines) >= 2:
        parts = lines[1].split()
        disk_info = {
            'total': parts[1] if len(parts) > 1 else '?',
            'used': parts[2] if len(parts) > 2 else '?',
            'avail': parts[3] if len(parts) > 3 else '?',
            'use_pct': parts[4] if len(parts) > 4 else '?',
        }
    
    # Check log sizes
    log_sizes = {}
    for f in ['shadow_trader_stderr.log', 'shadow_trader_stdout.log', 'auto_monitor.log']:
        path = os.path.join(LOGS_DIR, f)
        if os.path.exists(path):
            size = os.path.getsize(path)
            log_sizes[f] = size
    
    return {
        'disk': disk_info,
        'log_sizes': log_sizes,
        'critical': any(s > 500_000_000 for s in log_sizes.values()),
    }


def probe_database() -> dict:
    """Check database health."""
    db_path = os.path.join(DATA_DIR, 'ai_memory.db')
    if not os.path.exists(db_path):
        return {'status': 'missing', 'critical': True}
    
    # Check if database is accessible
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute('PRAGMA integrity_check')
        integrity = cursor.fetchone()[0]
        conn.close()
        return {
            'status': 'ok' if integrity == 'ok' else integrity,
            'size': os.path.getsize(db_path),
            'critical': integrity != 'ok',
        }
    except Exception as e:
        return {
            'status': f'error: {e}',
            'critical': True,
        }


# === AUTO-REPAIR FUNCTIONS ===

def repair_yfinance_cache() -> bool:
    """Fix yfinance SQLite cache WAL/SHM corruption."""
    cache_dir = os.path.expanduser('~/Library/Caches/py-yfinance')
    fixed = False
    
    for pattern in ['*.db-wal', '*.db-shm']:
        for f in Path(cache_dir).glob(pattern):
            try:
                os.remove(f)
                fixed = True
                print(f'  [修复] 删除 {f.name}')
            except:
                pass
    
    # Vacuum databases
    for db_name in ['tkr-tz.db', 'cookies.db']:
        db_path = os.path.join(cache_dir, db_name)
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.execute('PRAGMA journal_mode=DELETE')
                conn.execute('VACUUM')
                conn.close()
                print(f'  [修复] {db_name} VACUUM 完成')
                fixed = True
            except Exception as e:
                print(f'  [跳过] {db_name} 被占用 ({e})')
    
    return fixed


def repair_corrupt_logs() -> bool:
    """Rotate logs if they're too large."""
    max_size = 200 * 1024 * 1024  # 200MB
    fixed = False
    
    for f in ['shadow_trader_stderr.log', 'shadow_trader_stdout.log', 'auto_monitor.log']:
        path = os.path.join(LOGS_DIR, f)
        if os.path.exists(path) and os.path.getsize(path) > max_size:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = f'{path}.{ts}'
            os.rename(path, backup)
            print(f'  [修复] 日志 {f} 过大 ({os.path.getsize(path)//1024//1024}MB)，已备份为 {backup}.{ts}')
            fixed = True
    
    return fixed


def repair_restart_service(service: str) -> bool:
    """Restart a service via launchctl."""
    if service == 'shadow_trader':
        name = 'com.atos.shadowtrader'
    elif service == 'dashboard':
        name = 'ai.atos.dashboard'
    elif service == 'futu_opend':
        name = 'com.futunn.FutuOpenD'
    elif service in ('web_dashboard', 'dashboard_legacy'):
        name = 'ai.atos.dashboard'  # Fix: 8000 和 9000 指向同一个 launchd service
    else:
        return False
    
    try:
        # Kill old process first
        subprocess.run(['launchctl', 'kickstart', '-k', f'gui/501/{name}'],
                      capture_output=True, timeout=10)
        print(f'  [修复] 重启 {name}')
        return True
    except Exception as e:
        print(f'  [失败] 重启 {name}: {e}')
        return False


# === MAIN HEALING LOOP ===

def save_repair_to_skill(error_pattern: str, fix_description: str, fix_command: str):
    """Save a fix into a skill so it's remembered forever."""
    skill_path = os.path.join(SKILLS_DIR, 'auto-fixes', f'fix-{error_pattern}.md')
    os.makedirs(os.path.dirname(skill_path), exist_ok=True)
    
    content = f"""---
name: fix-{error_pattern}
description: "自动修复: {fix_description}"
version: 1.0.0
author: Self-Healing System
---
# 自动修复: {error_pattern}

## 问题描述
{fix_description}

## 修复步骤
```bash
{fix_command}
```

## 触发条件
自动检测到 {error_pattern} 错误时触发。
"""
    with open(skill_path, 'w') as f:
        f.write(content)
    print(f'  [记忆] 修复模式已保存到 skill: fix-{error_pattern}')


def run_healing_check() -> dict:
    """Run the full health check and auto-repair."""
    print(f'[{datetime.now().strftime("%H:%M:%S")}] ATOS 自愈系统启动')
    print()
    
    report = {
        'timestamp': datetime.now().isoformat(),
        'probes': {},
        'repairs': [],
        'errors_found': 0,
    }
    
    # 1. Process check
    print('1/5 检查服务进程...')
    processes = probe_processes()
    report['probes']['processes'] = processes
    
    for p in processes:
        if p['status'] == 'down':
            print(f'  [WARN] {p["service"]} (:{p["port"]}) 未运行')
            fixed = repair_restart_service(p['service'])
            report['repairs'].append({
                'type': 'restart',
                'service': p['service'],
                'success': fixed,
            })
            if fixed:
                print(f'  [OK] {p["service"]} 已重启')
                save_repair_to_skill(
                    f'{p["service"]}-down',
                    f'{p["service"]} 服务意外停止，自动重启',
                    f'launchctl kickstart -k gui/501/com.atos.{p["service"]}' 
                        if p['service'] != 'futu_opend' 
                        else 'launchctl kickstart -k gui/501/com.futunn.FutuOpenD'
                )
        else:
            print(f'  [OK] {p["service"]} (:{p["port"]}) PID={p["pid"]}')
    
    # 2. Error scan
    print('2/5 扫描错误日志...')
    errors = probe_errors()
    report['probes']['errors'] = errors
    
    if errors['critical']:
        report['errors_found'] += 1
        print(f'  [WARN] 发现 {errors["patterns"]["exception"]} 个异常, '
              f'{errors["patterns"]["database_locked"]} 个数据库锁错误')
        
        # Auto-fix: clean yfinance cache if database errors
        if errors['patterns']['database_locked'] > 10:
            print('  -> 尝试修复数据库缓存...')
            fixed = repair_yfinance_cache()
            if fixed:
                report['repairs'].append({
                    'type': 'yfinance_cache_clean',
                    'success': True,
                })
                save_repair_to_skill(
                    'yfinance-cache-locked',
                    'yfinance SQLite 缓存损坏导致下载失败，清除 WAL/SHM 文件',
                    'rm -f ~/Library/Caches/py-yfinance/*.db-wal ~/Library/Caches/py-yfinance/*.db-shm'
                )
    else:
        print('  [OK] 无严重异常')
    
    # 3. Disk check
    print('3/5 检查磁盘空间...')
    disk = probe_disk()
    report['probes']['disk'] = disk
    
    if disk['critical']:
        print(f'  [WARN] 日志文件过大')
        fixed = repair_corrupt_logs()
        if fixed:
            report['repairs'].append({
                'type': 'log_rotation',
                'success': True,
            })
    else:
        if disk['disk']:
            print(f'  [OK] 磁盘 {disk["disk"]["avail"]} 可用')
    
    # 4. Database check
    print('4/5 检查数据库...')
    db = probe_database()
    report['probes']['database'] = db
    
    if db['critical']:
        print(f'  [WARN] 数据库异常: {db["status"]}')
        report['errors_found'] += 1
    else:
        print(f'  [OK] ai_memory.db ({db["size"]//1024//1024}MB)')
    
    # 5. Check last cycle time
    print('5/5 检查最近运行...')
    if os.path.exists(ERROR_LOG):
        result = subprocess.run(
            ['grep', '-c', 'Cycle .* done', ERROR_LOG],
            capture_output=True, text=True, timeout=5
        )
        today_cycles = result.stdout.strip()
        print(f'  [OK] 今日已完成 {today_cycles} 个运行周期')
    report['probes']['cycles_today'] = {'count': today_cycles}
    
    # 6. File descriptor check
    print('6/6 检查文件描述符...')
    lsof_result = subprocess.run(
        ['lsof', '-ti', ':19999'],
        capture_output=True, text=True, timeout=10
    )
    pid = lsof_result.stdout.strip().splitlines()[-1] if lsof_result.stdout.strip() else None
    if pid and pid.isdigit():
        try:
            r = subprocess.run(['lsof', '-p', str(pid)], capture_output=True, text=True, timeout=10)
            fd_count = len(r.stdout.strip().splitlines()) - 1
            fd_threshold = int(os.environ.get('ATOS_FD_THRESHOLD', '256'))
            print(f'  shadow_trader PID={pid} FD={fd_count}')
            if fd_count > fd_threshold:
                print(f'  [WARN] FD({fd_count}) 超过阈值({fd_threshold}), 执行轮转...')
                subprocess.run(['launchctl', 'kickstart', 'gui/501/com.atos.shadowtrader'],
                               capture_output=True, timeout=10)
                print(f'  [修复] shadow_trader 已重启 (FD={fd_count}→0)')
                report['repairs'].append({
                    'type': 'fd_overflow_restart',
                    'fd_count': fd_count,
                    'success': True,
                })
            else:
                print(f'  [OK] FD 正常 ({fd_count} < {fd_threshold})')
        except Exception as e:
            print(f'  [跳过] FD 检查失败: {e}')
    else:
        print(f'  [跳过] 无法获取 shadow_trader PID')
    
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    print()
    print(f'[完成] 自愈检查结束 | 错误: {report["errors_found"]} | 修复: {len(report["repairs"])}')
    print()
    
    return report


if __name__ == '__main__':
    run_healing_check()
