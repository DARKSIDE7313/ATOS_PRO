#!/bin/bash
# ATOS FutuOpenD 状态检查脚本 v5 (2026-06-28 修复)
# 只检查+报告，不再杀进程。
# FutuOpenD 的恢复由 futu_watchdog.py 统一管理。

PORT=11111
LOG=/Users/benson/ATOS_PRO/logs/futu_recovery.log

check_port() {
    lsof -i :$PORT 2>/dev/null | grep -q LISTEN
}

check_api() {
    python3 -c "
from futu import OpenQuoteContext, RET_OK
ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
ret, _ = ctx.get_market_snapshot(['US.SPY'])
ctx.close()
exit(0 if ret == RET_OK else 1)
" 2>/dev/null
}

if check_port; then
    if check_api; then
        # 一切正常，静默退出
        exit 0
    else
        echo "$(date): ⚠️ 端口11111通但API未就绪 (可能未登录)" >> $LOG
        exit 0  # 不杀进程! 等用户手动登录
    fi
else
    echo "$(date): ⚠️ 端口11111不通，FutuOpenD未运行" >> $LOG
    # 只尝试用 open -g 启动一次(不弹窗)，不杀任何进程
    if ! pgrep -f "Futu_OpenD" > /dev/null; then
        echo "$(date): 进程不存在，尝试 open -g 启动" >> $LOG
        open -a Futu_OpenD -g 2>/dev/null
    else
        echo "$(date): 进程存在但端口不通，等待中... (不杀进程)" >> $LOG
    fi
    exit 0
fi
