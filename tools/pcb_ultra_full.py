"""PCB Ultra 全流程 + PBR 照片級渲染"""
import sys, os
os.chdir("/Users/benson/ATOS_PRO")

shared = {'__name__': '__loaded__', '__builtins__': __builtins__}
for step in ["tools/pcb_ultra_step1.py","tools/pcb_ultra_step2.py",
             "tools/pcb_ultra_step3.py","tools/pcb_ultra_step4.py",
             "tools/pcb_ultra_shaders.py"]:
    with open(step) as f:
        exec(compile(f.read(), step, 'exec'), shared)

shared['main']()
shared['run_photoreal_upgrade']()
shared['render_all_views']("/Users/benson/ATOS_PRO/data/pcb_ultra_render")

print("\n✅ 全部完成！")
