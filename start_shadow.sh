#!/bin/bash
# ATOS Shadow Trader — 手动调试启动脚本
# ════════════════════════════════════════════════════════════════════
# Phase 5 (2026-08-22) 重写 — 单一启动/守护路径收敛:
#   生产守护 = LaunchAgent com.atos.shadowtrader (RunAtLoad + KeepAlive,
#   崩溃自动重启, 端口锁 :19999 防重复实例)。本脚本仅供手动调试;
#   生产环境请勿直接运行 (会与 LaunchAgent 实例竞争端口锁, 后启动者静默退出)。
#
# 修复 (报告 §3.5):
#   1. 移除内嵌明文 DEEPSEEK_API_KEY / ATOS_EMAIL_PASS → 统一 source .env
#   2. 移除过期 cloudflared 隧道段 (错误指向 8899/Vibe 端口;
#      隧道由 com.atos.cloudflared-tunnel LaunchAgent 托管, 正确目标 9000/Dashboard)
#   3. 移除 dashboard pkill/启动段 (由 ai.atos.dashboard LaunchAgent 托管)
# ════════════════════════════════════════════════════════════════════

cd /Users/benson/ATOS_PRO

# 密钥统一从 .env 读取 (不再内嵌明文; set -a 自动导出全部变量)
set -a; source .env; set +a
export PATH="$HOME/Library/Python/3.14/bin:$PATH"

VENV="/Users/benson/ATOS_PRO/venv/bin/python"
LOCK="/Users/benson/ATOS_PRO/data/.shadow_trader.lock"
LOG="/Users/benson/ATOS_PRO/logs/shadow.log"

# 生产守护提示
if launchctl list | grep -q com.atos.shadowtrader; then
    echo "⚠️  LaunchAgent com.atos.shadowtrader 已在托管 shadow_trader。"
    echo "   生产重启请用: launchctl kickstart -k gui/\$(id -u)/com.atos.shadowtrader"
    echo "   继续手动启动将因端口锁 :19999 被占用而静默退出。"
fi

# 清理僵尸锁
if [ -f "$LOCK" ]; then
    PID=$(cat "$LOCK")
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$LOCK"
    fi
fi

# caffeinate -s: 合盖不休眠，-i: 熄屏不休眠
exec caffeinate -i -s $VENV -m atos.shadow.shadow_trader >> "$LOG" 2>&1
