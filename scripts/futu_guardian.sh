#!/bin/bash
# FutuOpenD 后台守护者 v4
# - 用 open -g 启动（不弹窗不弹Dock）
# - 只有端口真的不通超过3分钟才重启
# - 重启时自动隐藏窗口

PORT=11111
LOG=/Users/benson/ATOS_PRO/logs/futu_guardian.log
CHECK=60
COOLDOWN=600
FAILS=0
LAST_RESTART=0

hide_window() {
    osascript -e 'tell application "System Events" to set visible of process "Futu_OpenD" to false' 2>/dev/null
}

check() { nc -z -w 3 127.0.0.1 $PORT 2>/dev/null; }

echo "$(date) 🟢 守护者 v4 启动 (open -g 后台, 不弹窗)" >> $LOG

while true; do
    if check; then
        FAILS=0
    else
        FAILS=$((FAILS + 1))
        NOW=$(date +%s)
        if [ $FAILS -ge 3 ] && [ $((NOW - LAST_RESTART)) -ge $COOLDOWN ]; then
            echo "$(date) 🔴 重启Futu (open -g 后台)" >> $LOG
            LAST_RESTART=$NOW
            FAILS=0
            open -a Futu_OpenD -g 2>/dev/null
            for i in $(seq 1 15); do
                sleep 2
                check && { hide_window; echo "$(date) ✅ 已恢复" >> $LOG; break; }
            done
        fi
    fi
    sleep $CHECK
done
