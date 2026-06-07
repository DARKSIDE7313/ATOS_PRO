#!/bin/bash
# ATOS Shadow Trader — 防休眠启动脚本
# caffeinate 确保合上电脑盖子也不休眠

# ⚠️ LaunchAgent 不读 .zshrc，这里手动设所有环境变量
export DEEPSEEK_API_KEY="sk-95fbf1c969c142b289a2644c317248a2"
export ATOS_EMAIL_USER="9275945.yaocp@gmail.com"
export ATOS_EMAIL_PASS="tqka algh rjhg dccv"
export PATH="$HOME/Library/Python/3.14/bin:$PATH"

VENV="/Users/benson/ATOS_PRO/venv/bin/python"
LOCK="/Users/benson/ATOS_PRO/data/.shadow_trader.lock"
LOG="/Users/benson/ATOS_PRO/logs/shadow.log"

# 清理僵尸锁
if [ -f "$LOCK" ]; then
    PID=$(cat "$LOCK")
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$LOCK"
    fi
fi

# 启动仪表盘（如果还没跑）
pkill -f "dashboard/server" 2>/dev/null
nohup $VENV /Users/benson/ATOS_PRO/dashboard/server.py > /Users/benson/ATOS_PRO/logs/dashboard.log 2>&1 &

# 启动外网隧道
pkill -f "cloudflared tunnel" 2>/dev/null
nohup cloudflared tunnel --url http://localhost:8899 > /Users/benson/ATOS_PRO/logs/cloudflared.log 2>&1 &

cd /Users/benson/ATOS_PRO

# caffeinate -s: 合盖不休眠，-i: 熄屏不休眠
exec caffeinate -i -s $VENV -m atos.shadow.shadow_trader >> "$LOG" 2>&1
