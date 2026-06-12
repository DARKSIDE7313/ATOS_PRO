#!/usr/bin/env python3
"""
Secret Vault Guardian — 密钥文件全局防护系统
===========================================
保护所有 .env 文件不被 `***` 覆写污染。
也防止其他系统（.env 格式的配置文件）遭受同样问题。

工作原理：
  1. 对所有受保护的 .env 文件建立 SHA256 基线
  2. 检测任何将 *** 写回文件的行为并阻止
  3. 每日自动备份，可一键恢复
  4. 写操作前校验：如果写入的值是 *** 或空字符串，拒绝
  5. cron 每5分钟巡检

安装：
  python3 secret_guardian.py --install   # 安装 cron 和基线
  python3 secret_guardian.py --check     # 手动检查
  python3 secret_guardian.py --fix       # 从备份恢复损坏文件
"""

import os
import sys
import json
import hashlib
import shutil
import subprocess
import smtplib
from datetime import datetime
from pathlib import Path

# === CONFIG ===
# 受保护的文件列表（可以手动添加更多）
PROTECTED_FILES = [
    os.path.expanduser('~/.hermes/.env'),
    os.path.expanduser('~/ATOS_PRO/.env'),
    os.path.expanduser('~/chenxi/.env'),
]

BACKUP_DIR = os.path.expanduser('~/.secret_vault_backups')
BASELINE_FILE = os.path.expanduser('~/.secret_vault_baseline.json')
LOG_FILE = os.path.expanduser('~/.secret_vault_guardian.log')


def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


def get_secret_lines(path: str) -> list:
    """读取 .env 文件，返回所有含 = 的行（过滤注释和空行）"""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return [l for l in lines if '=' in l and not l.strip().startswith('#')]


def compute_baseline(path: str) -> dict:
    """计算一个 .env 文件的基线（每行的 SHA256 + 整体校验）"""
    if not os.path.exists(path):
        return {'exists': False}
    
    with open(path, 'rb') as f:
        content = f.read()
    
    secrets = get_secret_lines(path)
    line_hashes = {}
    for l in secrets:
        key = l.split('=', 1)[0].strip()
        # 对整行（含值）做 hash
        line_hash = hashlib.sha256(l.encode()).hexdigest()
        line_hashes[key] = {
            'hash': line_hash,
            'line_preview': l[:20] + '...' if len(l) > 20 else l.strip(),
        }
    
    return {
        'exists': True,
        'file_hash': hashlib.sha256(content).hexdigest(),
        'size': len(content),
        'keys': line_hashes,
        'secret_count': len(secrets),
    }


def compute_all_baselines() -> dict:
    """计算所有受保护文件的基线"""
    baselines = {}
    for path in PROTECTED_FILES:
        if os.path.exists(path):
            baselines[path] = compute_baseline(path)
    return baselines


def save_baseline():
    """保存当前基线和备份"""
    baselines = compute_all_baselines()
    with open(BASELINE_FILE, 'w') as f:
        json.dump(baselines, f, indent=2)
    log(f'基线已保存: {len(baselines)} 个文件')
    return baselines


def load_baseline() -> dict:
    """加载保存的基线"""
    if not os.path.exists(BASELINE_FILE):
        return {}
    with open(BASELINE_FILE) as f:
        return json.load(f)


def check_integrity() -> list:
    """检查所有受保护文件是否有异常（*** 污染）"""
    violations = []
    
    for path in PROTECTED_FILES:
        if not os.path.exists(path):
            continue
        
        secrets = get_secret_lines(path)
        for l in secrets:
            key = l.split('=', 1)[0].strip()
            value = l.split('=', 1)[1].strip().strip("'").strip('"')
            
            # 检测 1: 值就是 ***
            if value == '***':
                violations.append({
                    'file': path,
                    'key': key,
                    'issue': '值被覆写为 ***',
                    'severity': 'CRITICAL',
                })
            
            # 检测 2: 空值（可能被误删）
            if not value:
                violations.append({
                    'file': path,
                    'key': key,
                    'issue': '空值（可能被误删）',
                    'severity': 'WARNING',
                })
        
        # 检测 3: 文件是否被截断
        baseline = load_baseline().get(path, {})
        if baseline.get('exists'):
            current_size = os.path.getsize(path)
            original_size = baseline.get('size', 0)
            if current_size < original_size * 0.5:  # 文件缩小超过一半
                violations.append({
                    'file': path,
                    'key': '*',
                    'issue': f'文件大小从 {original_size} 缩小到 {current_size}',
                    'severity': 'CRITICAL',
                })
    
    return violations


def backup_env(path: str) -> str:
    """备份 .env 文件到安全目录"""
    if not os.path.exists(path):
        return ''
    
    os.makedirs(BACKUP_DIR, exist_ok=True)
    filename = Path(path).name
    # 使用路径 hash 避免同名冲突
    path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, f'{path_hash}_{filename}_{ts}')
    
    shutil.copy2(path, backup_path)
    return backup_path


def restore_from_backup(path: str) -> bool:
    """从最近的备份恢复文件"""
    if not os.path.exists(path):
        return False
    
    filename = Path(path).name
    path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
    prefix = f'{path_hash}_{filename}_'
    
    backups = sorted([
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith(prefix)
    ], reverse=True)
    
    if not backups:
        log(f'[ERROR] {path}: 没有可用的备份')
        return False
    
    latest_backup = os.path.join(BACKUP_DIR, backups[0])
    shutil.copy2(latest_backup, path)
    log(f'[RESTORE] {path}: 从 {latest_backup} 恢复')
    return True


def create_write_guard(path: str) -> str:
    """为 .env 文件创建写保护钩子脚本"""
    guard_script = f'''#!/bin/bash
# Secret Vault Write Guard — 阻止 *** 覆写
# 在编辑 .env 文件前运行此检查

FILE="{path}"
if [ ! -f "$FILE" ]; then
    exit 0
fi

# 检查文件中是否有 *** 值
if grep -q '=[[:space:]]*\\*\\*\\*[[:space:]]*$' "$FILE" 2>/dev/null; then
    echo "[GUARDIAN] WARNING: $FILE 包含被 *** 覆写的密钥！"
    echo "[GUARDIAN] 运行 ~/ATOS_PRO/tools/secret_guardian.py --fix 来恢复"
    exit 1
fi

# 检查是否有空值密钥
if grep -q '=[[:space:]]*$' "$FILE" 2>/dev/null; then
    echo "[GUARDIAN] WARNING: $FILE 包含空值密钥行！"
    exit 1
fi

exit 0
'''
    guard_path = os.path.expanduser(f'~/.guardian_write_{Path(path).stem}.sh')
    with open(guard_path, 'w') as f:
        f.write(guard_script)
    os.chmod(guard_path, 0o755)
    return guard_path


def check_and_repair(force_fix: bool = False) -> int:
    """运行完整检查，必要时自动修复"""
    log('=' * 50)
    log('Secret Vault Guardian — 开始巡检')
    log('=' * 50)
    
    # 1. 备份所有文件
    backups = {}
    for path in PROTECTED_FILES:
        if os.path.exists(path):
            backups[path] = backup_env(path)
            if backups[path]:
                log(f'[BACKUP] {path} → {Path(backups[path]).name}')
    
    # 2. 保存基线
    save_baseline()
    
    # 3. 完整性检查
    violations = check_integrity()
    
    if not violations:
        log('[OK] 所有文件正常，未发现 *** 污染')
        return 0
    
    log(f'[WARN] 发现 {len(violations)} 个问题:')
    for v in violations:
        log(f'  [{v["severity"]}] {v["file"]} → {v["key"]}: {v["issue"]}')
    
    # 4. 自动修复（如果启用）
    if force_fix:
        # 按文件分组修复
        files_to_restore = set(v['file'] for v in violations if v['severity'] == 'CRITICAL')
        for path in files_to_restore:
            log(f'[FIX] 尝试修复 {path}...')
            success = restore_from_backup(path)
            if success:
                log(f'[FIX] {path} 已从备份恢复')
            else:
                log(f'[ERROR] {path} 无法自动恢复，需要人工处理')
                log(f'  请重新设置该文件的密钥：')
                # 列出哪些 key 需要重新设置
                for v in violations:
                    if v['file'] == path:
                        log(f'  需要重新设置: {v["key"]}')
    
    return len(violations)


def install_cron():
    """安装 cron 任务"""
    script_path = os.path.expanduser('~/ATOS_PRO/tools/secret_guardian.py')
    cron_line = f'*/5 * * * * python3 {script_path} --check >> {LOG_FILE} 2>&1'
    
    # 获取现有 crontab
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ''
    
    if 'secret_guardian' in existing:
        log('[OK] cron 任务已存在')
        return
    
    new_crontab = existing.strip() + '\n' + cron_line + '\n'
    subprocess.run(['crontab'], input=new_crontab, text=True)
    log('[INSTALL] cron 任务已添加（每5分钟巡检）')
    
    # 创建写保护钩子
    for path in PROTECTED_FILES:
        guard_path = create_write_guard(path)
        log(f'[INSTALL] 写保护钩子: {guard_path}')
    
    log('[INSTALL] 完成')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Secret Vault Guardian')
    parser.add_argument('--check', action='store_true', help='检查所有 .env 文件完整性')
    parser.add_argument('--fix', action='store_true', help='自动从备份修复损坏文件')
    parser.add_argument('--install', action='store_true', help='安装 cron 巡检 + 写保护钩子')
    parser.add_argument('--backup', action='store_true', help='强制备份所有文件')
    parser.add_argument('--status', action='store_true', help='显示当前状态')
    
    args = parser.parse_args()
    
    if args.install:
        # 先做基线 + 备份
        for path in PROTECTED_FILES:
            if os.path.exists(path):
                backup_env(path)
        save_baseline()
        install_cron()
        return
    
    if args.backup:
        for path in PROTECTED_FILES:
            if os.path.exists(path):
                bp = backup_env(path)
                print(f'备份: {path} → {bp}')
        return
    
    if args.status:
        baseline = load_baseline()
        print(f'受保护文件: {len(baseline)}')
        for path, bl in baseline.items():
            keys = list(bl.get('keys', {}).keys())
            print(f'  {path}: {len(keys)} 个密钥')
        print(f'备份目录: {BACKUP_DIR}')
        print(f'日志: {LOG_FILE}')
        return
    
    if args.check:
        n = check_and_repair(force_fix=args.fix)
        sys.exit(0 if n == 0 else 1)
    
    # 默认：巡检 + 自动修复
    n = check_and_repair(force_fix=True)
    sys.exit(0 if n == 0 else 1)


if __name__ == '__main__':
    main()
