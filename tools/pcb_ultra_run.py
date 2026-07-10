"""
PCB Ultra 全流程執行器
用法: /Applications/Blender.app/Contents/MacOS/Blender --background --python tools/pcb_ultra_run.py
"""
import sys
import os

os.chdir("/Users/benson/ATOS_PRO")

# 使用共享全域命名空間，確保各步驟的函數互相可見
shared_globals = {
    '__name__': '__loaded__',  # 跳過各步驟的 if __name__ == "__main__"
    '__builtins__': __builtins__,
}

steps = [
    "tools/pcb_ultra_step1.py",
    "tools/pcb_ultra_step2.py",
    "tools/pcb_ultra_step3.py",
    "tools/pcb_ultra_step4.py",
]

for step_path in steps:
    print(f"[LOAD] 載入 {step_path} ...")
    with open(step_path) as f:
        code = f.read()
    exec(compile(code, step_path, 'exec'), shared_globals)

# 從共享命名空間取出 main 和 render_all_views
main = shared_globals['main']
render_all_views = shared_globals['render_all_views']

# 執行總裝
OUTPUT = "/Users/benson/ATOS_PRO/data/pcb_ultra_render"
print("\n[RUN] 開始全局總裝...")
result = main()

print("\n[RENDER] 開始渲染...")
render_all_views(OUTPUT)
print(f"\n✅ 完成！渲染輸出: {OUTPUT}")
