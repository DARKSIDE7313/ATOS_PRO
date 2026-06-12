#!/usr/bin/env bash
# ATOS PRO — Debug 补丁脚本
# 使用方式: cd /Users/benson/ATOS_PRO && bash /tmp/apply_atos_patches.sh
set -e
cd "$(dirname "$0")" 2>/dev/null || true

echo "=== ATOS PRO: 应用 Debug 补丁 ==="

# ---- 备份 ----
TS=$(date +%Y%m%d_%H%M%S)
for f in atos/live/kelly.py atos/live/risk_manager.py atos/live/strategy_v3.py atos/shadow/shadow_trader.py; do
    cp "$f" "${f}.bak.${TS}"
    echo "  备份: ${f}.bak.${TS}"
done

# ---- P4: kelly.py ----
python3 - << 'PY'
import pathlib
f = pathlib.Path("atos/live/kelly.py")
txt = f.read_text()

patches = [
    (
        "    if score >= 0.80:\n        base_pct = 0.12\n    elif score >= 0.70:\n        base_pct = 0.08\n    elif score >= 0.50:\n        base_pct = 0.05",
        "    if score >= 0.80:\n        base_pct = 0.07      # FIX P4\n    elif score >= 0.70:\n        base_pct = 0.05      # FIX P4\n    elif score >= 0.55:\n        base_pct = 0.03      # FIX P4"
    ),
    (
        "    if has_news_catalyst:\n        after_dd *= 1.5\n\n    # Hard cap at 30% (aggressive but not reckless)\n    final = min(after_dd, 0.30)",
        "    if has_news_catalyst:\n        after_dd *= 1.15    # FIX P4: 1.5->1.15\n\n    # FIX P4: Hard cap 30%->10%\n    final = min(after_dd, 0.10)"
    ),
]

for old, new in patches:
    old_r = old.replace("\\n", "\n")
    new_r = new.replace("\\n", "\n")
    if old_r in txt:
        txt = txt.replace(old_r, new_r, 1)
        print(f"  Applied: {old_r[:40]!r}...")
    else:
        print(f"  WARN: pattern not found: {old_r[:40]!r}")

if "get_logger" not in txt:
    txt = "from atos.core.logging import get_logger\nlogger = get_logger(\"kelly\")\n" + txt

txt = txt.replace(
    'print(f"[crouching]',
    'logger.debug(f"[crouching]'
)
f.write_text(txt)
print("  P4 kelly.py done")
PY

# ---- P5: risk_manager.py ----
python3 - << 'PY'
import pathlib
f = pathlib.Path("atos/live/risk_manager.py")
txt = f.read_text()

patches = [
    ("STOP_LOSS_PCT = 0.12", "STOP_LOSS_PCT = 0.08           # FIX P5"),
    ("TAKE_PROFIT_PCT = 0.15", "TAKE_PROFIT_PCT = 0.12         # FIX P5"),
    ("effective_stop = max(STOP_LOSS_PCT, vol_stop)  # 取较宽者", "effective_stop = min(STOP_LOSS_PCT, vol_stop)  # FIX P5: max->min"),
    ("(atr_val / px) * 2.5", "(atr_val / px) * 2.0  # FIX P5"),
]
for old, new in patches:
    if old in txt:
        txt = txt.replace(old, new, 1)
        print(f"  Applied: {old[:50]!r}")
    else:
        print(f"  WARN: not found: {old[:50]!r}")
f.write_text(txt)
print("  P5 risk_manager.py done")
PY

# ---- P6: strategy_v3.py ----
python3 - << 'PY'
import pathlib
f = pathlib.Path("atos/live/strategy_v3.py")
txt = f.read_text()

old = '''WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.25, "breakout": 0.15, "reversal": 0.30},
    "HIGH_VOL":    {"momentum": 0.10, "trend": 0.20, "breakout": 0.10, "reversal": 0.60},
    "BEAR":        {"momentum": 0.20, "trend": 0.20, "breakout": 0.10, "reversal": 0.50},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.25, "breakout": 0.25, "reversal": 0.25},
}'''

new = '''# FIX P6: 降低HIGH_VOL/BEAR的reversal权重，防止系统性接飞刀
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.30, "breakout": 0.20, "reversal": 0.20},
    "HIGH_VOL":    {"momentum": 0.15, "trend": 0.35, "breakout": 0.25, "reversal": 0.25},
    "BEAR":        {"momentum": 0.15, "trend": 0.40, "breakout": 0.30, "reversal": 0.15},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.30, "breakout": 0.20, "reversal": 0.25},
}'''

if old in txt:
    txt = txt.replace(old, new, 1)
    print("  Applied: WEIGHT_MATRIX")
else:
    print("  WARN: WEIGHT_MATRIX not found")
f.write_text(txt)
print("  P6 strategy_v3.py done")
PY

# ---- 语法验证 ----
echo ""
echo "=== 语法验证 ==="
python3 -m py_compile atos/live/kelly.py        && echo "  ✅ kelly.py OK"        || echo "  ❌ kelly.py FAIL"
python3 -m py_compile atos/live/risk_manager.py && echo "  ✅ risk_manager.py OK" || echo "  ❌ risk_manager.py FAIL"
python3 -m py_compile atos/live/strategy_v3.py  && echo "  ✅ strategy_v3.py OK"  || echo "  ❌ strategy_v3.py FAIL"
python3 -m py_compile atos/shadow/shadow_trader.py && echo "  ✅ shadow_trader.py OK" || echo "  ❌ shadow_trader.py FAIL"

echo ""
echo "所有补丁完成。备份后缀: .bak.${TS}"
