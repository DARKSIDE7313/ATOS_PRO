#!/usr/bin/env python3
"""
repair_shadow_state.py — 修复 shadow_state.json 的资金一致性问题
运行一次即可: python3 scripts/repair_shadow_state.py
"""

import json
import os
from pathlib import Path

BASE = Path(__file__).parent.parent if '__file__' in dir() else Path(os.getcwd())
STATE_FILE = BASE / "data" / "shadow_state.json"

def repair():
    if not STATE_FILE.exists():
        print(f"State file not found: {STATE_FILE}")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    # 1. 标准化仓位键名
    positions = state.get("positions", {})
    for sym, p in positions.items():
        # 确保 shares 和 qty 都存在
        shares = p.get("shares", p.get("qty", p.get("quantity", 0)))
        p["shares"] = shares
        p["qty"] = shares

    # 2. 重新计算权益
    cash = state.get("cash", 0)
    pos_val = sum(
        p.get("shares", 0) * p.get("last_price", p.get("avg_price", 0))
        for p in positions.values()
    )
    correct_equity = cash + pos_val
    reported_equity = state.get("equity", 0)

    print(f"Cash:           ${cash:,.2f}")
    print(f"Position value: ${pos_val:,.2f}")
    print(f"Correct equity: ${correct_equity:,.2f}")
    print(f"Reported equity: ${reported_equity:,.2f}")
    print(f"Difference:     ${correct_equity - reported_equity:,.2f}")

    # 3. 更新
    state["equity"] = correct_equity
    state["prev_equity"] = correct_equity

    # 4. 备份 + 写入
    backup = STATE_FILE.with_suffix(".json.bak")
    import shutil
    shutil.copy2(STATE_FILE, backup)
    print(f"\nBackup: {backup}")

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"✅ State repaired: {STATE_FILE}")

    # 清理 pycache
    for pycache in BASE.rglob("__pycache__"):
        import shutil
        shutil.rmtree(pycache, ignore_errors=True)
    print("✅ __pycache__ cleared")

if __name__ == "__main__":
    repair()
