#!/bin/bash
# ⚠️ DEPRECATED — Phase 5 框架重塑 (2026-08-22) 归档
# 本脚本与 LaunchAgent com.atos.shadowtrader 的 KeepAlive 功能重复
# (双守护竞争风险, 报告 §3.5)。生产守护唯一路径 = LaunchAgent。
# 保留仅为历史参考, 请勿使用。
# ═══════════════════════════════════════════════════════════════
# ATOS Shadow Trader 守护脚本
# 如果挂了，10秒后自动重启。永远不停。

VENV="/Users/benson/ATOS_PRO/venv/bin/python"
MODULE="atos.shadow.shadow_trader"
LOG="/Users/benson/ATOS_PRO/logs/watchdog.log"

echo "[$(date)] 守护进程启动" >> "$LOG"

while true; do
    $VENV -m $MODULE >> "$LOG" 2>&1
    EXIT_CODE=$?
    echo "[$(date)] Shadow Trader 退出 (code=$EXIT_CODE)，10秒后重启..." >> "$LOG"
    sleep 10
done
