"""
PCB 全流程執行腳本 — 依序跑 step1→4 並渲染輸出。
用法: /Applications/Blender.app/Contents/MacOS/Blender --background --python tools/pcb_run_all.py
"""
import sys
import os

# 確保工作目錄正確
os.chdir("/Users/benson/ATOS_PRO")

# 將 tools/ 加入 path
sys.path.insert(0, "/Users/benson/ATOS_PRO/tools")

print("=" * 60)
print("  ATOS PRO PCB — 一鍵建模 + 渲染")
print("=" * 60)

# ---- Step 1: 初始化 + 基板 + 過孔 + 差分走線 ----
print("\n>>> 執行 Step 1...")
exec(open("tools/pcb_modeler_step1.py").read())

# ---- Step 2: BGA + RF 模塊 ----
print("\n>>> 執行 Step 2...")
exec(open("tools/pcb_modeler_step2.py").read())

# ---- Step 3: 金手指 + 排座 + 散熱片 + 電感 ----
print("\n>>> 執行 Step 3...")
exec(open("tools/pcb_modeler_step3.py").read())

# ---- Step 4: 阻焊 + 絲印 + 銅箔 + 渲染 ----
print("\n>>> 執行 Step 4...")

# 渲染輸出目錄
OUTPUT_DIR = "/Users/benson/ATOS_PRO/data/pcb_render"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 將輸出目錄注入 sys.argv，讓 step4 的 __main__ 自動使用它
_orig_argv = sys.argv
sys.argv = ["blender_step4", OUTPUT_DIR]
exec(open("tools/pcb_modeler_step4.py").read())
sys.argv = _orig_argv

print("\n✅ 全部完成！輸出目錄:", OUTPUT_DIR)
