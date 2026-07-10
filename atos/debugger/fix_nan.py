#!/usr/bin/env python3
"""
ATOS Shadow Trader 紧急修复脚本
修复所有 last_price=nan 为 last_price=avg_price
然后重启 Shadow Trader
"""
import json, os, sys, signal, time, math

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE, "data", "shadow_state.json")

# 1. 修复 nan
fixed = 0
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        state = json.load(f)
    
    positions = state.get("positions", {})
    for sym, p in positions.items():
        if not isinstance(p, dict):
            continue
        last = p.get("last_price", 0)
        avg = p.get("avg_price", 0)
        if last is None or (isinstance(last, float) and math.isnan(last)):
            p["last_price"] = avg if avg and str(avg) != "nan" else 0
            fixed += 1
    
    state["equity"] = state.get("cash", 0) + sum(
        p["qty"] * p["last_price"]
        for p in positions.values()
        if isinstance(p, dict) and p.get("last_price", 0)
    )
    
    # 修复 equity_history 中的 nan
    for eh in state.get("equity_history", []):
        if eh.get("equity") is None or (isinstance(eh.get("equity"), float) and math.isnan(eh["equity"])):
            eh["equity"] = state["equity"]
    
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    
    print(f"修复完成: {fixed} 个 nan → 均价")

# 2. 停掉旧进程的 socket 锁
try:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(("127.0.0.1", 19999))
    s.close()
    print("Socket 锁端口 19999 已被占用 → 表明 Shadow 仍在运行")
except Exception:
    print("Socket 锁已释放")

# 3. 杀旧进程
old_pid = state.get("pid", 0) if 'state' in dir() else 0
try:
    for line in os.popen("ps aux | grep shadow_trader | grep -v grep").readlines():
        parts = line.split()
        if len(parts) > 1:
            pid = int(parts[1])
            os.kill(pid, signal.SIGTERM)
            print(f"已终止 Shadow Trader PID: {pid}")
except Exception:
    pass

time.sleep(2)

print("\n状态已修复，可以重启 Shadow Trader")
print("运行: cd /Users/benson/ATOS_PRO && source venv/bin/activate && python3 -m atos.shadow.shadow_trader")
