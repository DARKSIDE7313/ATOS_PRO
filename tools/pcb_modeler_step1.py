"""
ATOS_PRO/tools/pcb_modeler_step1.py
Blender 4.2+ / Python 3.11 — PCB 超逼真建模脚本
步驟 1：初始化環境 + 基板微觀工藝 + 過孔陣列 + 差分信號線

使用方法：
    在 Blender Scripting 工作區貼上執行，或在終端：
    /Applications/Blender.app/Contents/MacOS/Blender --background --python this_file.py

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import math
import random

# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：初始化環境
# ══════════════════════════════════════════════════════════════════════════════

def init_environment():
    """
    清除 Blender 預設場景，設定 Cycles 渲染引擎與 GPU 加速。
    """
    print("[INIT] 清除默認物體...")

    # --- 清除所有默認物體（立方體、攝像機、燈光） ---
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    # 清除孤立數據（網格、材質等，避免殘留）
    for block in bpy.data.meshes:
        bpy.data.meshes.remove(block)
    for block in bpy.data.materials:
        bpy.data.materials.remove(block)

    print("[INIT] 設定 Cycles 渲染引擎...")

    # --- 設定渲染引擎為 Cycles（支援 GPU 加速） ---
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'

    # 嘗試啟用 GPU 計算（macOS 用 Metal，其他用 CUDA/OptiX）
    cycles_prefs = bpy.context.preferences.addons['cycles'].preferences
    cycles_prefs.refresh_devices()
    devices = cycles_prefs.devices

    gpu_found = False
    for d in devices:
        if d.type in ('METAL', 'CUDA', 'OPTIX'):
            d.use = True
            gpu_found = True
            print(f"  [GPU] 啟用設備: {d.name} ({d.type})")
        else:
            d.use = False  # 關閉 CPU 渲染以強制 GPU

    if gpu_found:
        cycles_prefs.compute_device_type = 'METAL'  # macOS
        scene.cycles.device = 'GPU'
        print("[INIT] 渲染設備 → GPU")
    else:
        scene.cycles.device = 'CPU'
        print("[INIT] 渲染設備 → CPU（未檢測到 GPU）")

    # --- 渲染參數：兼顧品質與速度 ---
    scene.cycles.samples = 256           # 取樣數
    scene.cycles.max_bounces = 8         # 最大反彈次數
    scene.cycles.diffuse_bounces = 4
    scene.cycles.glossy_bounces = 4
    scene.cycles.transmission_bounces = 12  # 透射（FR-4 半透明需要）

    # 降噪（保持細節）
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = 'OPENIMAGEDENOISE'

    # 色彩管理：Filmic 高動態範圍
    scene.view_settings.view_transform = 'Filmic'
    scene.view_settings.look = 'Medium High Contrast'

    print("[INIT] ✅ 環境初始化完成\n")


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：全域參數定義
# ══════════════════════════════════════════════════════════════════════════════

# PCB 主體尺寸（mm）— 標準 6 層板
PCB_L = 140.0       # 長度 X 軸
PCB_W = 90.0        # 寬度 Y 軸
PCB_H = 1.6         # 厚度 Z 軸（6 層 FR-4 疊構）

# 各層厚度（mm）
CU_THICKNESS = 0.035      # 銅箔（頂層/底層 1oz）
SOLDERMASK_THICKNESS = 0.02  # 阻焊層（綠油）
SILKSCREEN_THICKNESS = 0.01  # 絲印層（白字）

# 安裝孔參數
MOUNT_HOLE_DIAMETER = 3.2       # 孔徑
MOUNT_HOLE_MARGIN = 5.0         # 孔中心距板邊距離
MOUNT_PAD_DIAMETER = 5.0        # 外圍裸露焊盤直徑
PTH_THICKNESS = 0.05            # 鍍通孔銅層厚度

# 走線參數
TRACE_WIDTH = 0.15              # 信號線寬
TRACE_SPACING = 0.15            # 差動對線間距
TRACE_THICKNESS = 0.035         # 走線銅厚 = 1oz

# 過孔參數
VIA_OUTER_DIAMETER = 0.5        # 外徑（含焊環 annular ring）
VIA_INNER_DIAMETER = 0.25       # 內徑（鑽孔）
VIA_COUNT = 42                  # 過孔總數

print(f"[PARAMS] PCB: {PCB_L}×{PCB_W}×{PCB_H} mm")
print(f"[PARAMS] 安裝孔: Ø{MOUNT_HOLE_DIAMETER}mm, 距邊 {MOUNT_HOLE_MARGIN}mm")
print(f"[PARAMS] 過孔: 外徑 {VIA_OUTER_DIAMETER}mm / 內徑 {VIA_INNER_DIAMETER}mm × {VIA_COUNT}個")
print(f"[PARAMS] 差動線: 線寬 {TRACE_WIDTH}mm, 線距 {TRACE_SPACING}mm\n")


# ══════════════════════════════════════════════════════════════════════════════
# 材質工廠函數
# ══════════════════════════════════════════════════════════════════════════════

def create_fr4_material():
    """
    FR-4 基板材質：半透明啞光深綠，帶微觀玻璃纖維紋理。
    使用程序化噪波模擬玻璃纖維編織紋理。
    """
    mat = bpy.data.materials.new(name="PCB_FR4_Base")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # 清除默認節點
    nodes.clear()

    # --- 輸出節點 ---
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (400, 0)
    bsdf.inputs['Roughness'].default_value = 0.55       # 啞光
    bsdf.inputs['Transmission Weight'].default_value = 0.15  # 微半透明（FR-4 特性）
    bsdf.inputs['IOR'].default_value = 1.52              # 環氧樹脂折射率
    bsdf.inputs['Alpha'].default_value = 0.92            # 輕微半透明

    mat_output = nodes.new(type='ShaderNodeOutputMaterial')
    mat_output.location = (800, 0)
    links.new(bsdf.outputs['BSDF'], mat_output.inputs['Surface'])

    # --- 基礎顏色：深綠（FR-4 典型色 #1a3a1a） ---
    base_color = nodes.new(type='ShaderNodeRGB')
    base_color.location = (-400, 200)
    base_color.outputs[0].default_value = (0.102, 0.227, 0.102, 1.0)  # sRGB 近似

    # --- 玻璃纖維紋理：用 Wave Texture 模擬編織 ---
    # 第一層纖維（經線 warp）
    wave_warp = nodes.new(type='ShaderNodeTexWave')
    wave_warp.location = (-400, -100)
    wave_warp.wave_type = 'BANDS'
    wave_warp.wave_profile = 'SIN'
    wave_warp.inputs['Scale'].default_value = 80.0      # 纖維密度
    wave_warp.inputs['Distortion'].default_value = 2.0   # 輕微扭曲（模擬不均勻編織）
    wave_warp.inputs['Detail'].default_value = 4.0
    wave_warp.inputs['Detail Roughness'].default_value = 0.6

    # 第二層纖維（緯線 weft），旋轉 90 度
    wave_weft = nodes.new(type='ShaderNodeTexWave')
    wave_weft.location = (-400, -350)
    wave_weft.wave_type = 'BANDS'
    wave_weft.wave_profile = 'SIN'
    wave_weft.inputs['Scale'].default_value = 80.0
    wave_weft.inputs['Distortion'].default_value = 2.0
    wave_weft.inputs['Detail'].default_value = 4.0
    wave_weft.inputs['Detail Roughness'].default_value = 0.6

    # 旋轉緯線 90 度（透過 Mapping 節點的 Z 旋轉）
    mapping_weft = nodes.new(type='ShaderNodeMapping')
    mapping_weft.location = (-650, -350)
    mapping_weft.inputs['Rotation'].default_value = (0, 0, math.radians(90))
    links.new(mapping_weft.outputs['Vector'], wave_weft.inputs['Vector'])

    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-900, -100)
    links.new(tex_coord.outputs['UV'], wave_warp.inputs['Vector'])
    links.new(tex_coord.outputs['UV'], mapping_weft.inputs['Vector'])

    # --- 混合經緯線形成編織紋理 ---
    mix_weave = nodes.new(type='ShaderNodeMix')
    mix_weave.location = (-100, -100)
    mix_weave.data_type = 'FLOAT'
    mix_weave.blend_type = 'MULTIPLY'
    links.new(wave_warp.outputs['Fac'], mix_weave.inputs['A'])
    links.new(wave_weft.outputs['Fac'], mix_weave.inputs['B'])
    mix_weave.inputs['Factor'].default_value = 0.5

    # --- 用編織紋理微調粗糙度（纖維交點略粗糙） ---
    bump = nodes.new(type='ShaderNodeBump')
    bump.location = (100, -100)
    bump.inputs['Strength'].default_value = 0.08          # 極微弱的凹凸
    bump.inputs['Distance'].default_value = 0.002
    links.new(mix_weave.outputs['Result'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    # --- 添加微觀噪波（模擬樹脂不均勻） ---
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-150, -500)
    noise.inputs['Scale'].default_value = 200.0
    noise.inputs['Detail'].default_value = 8.0
    noise.inputs['Roughness'].default_value = 0.7

    # 將噪波混合到基礎顏色，模擬樹脂深淺變化
    mix_noise_color = nodes.new(type='ShaderNodeMix')
    mix_noise_color.location = (0, 200)
    mix_noise_color.data_type = 'RGBA'
    mix_noise_color.blend_type = 'MIX'
    links.new(base_color.outputs['Color'], mix_noise_color.inputs['A'])
    links.new(noise.outputs['Fac'], mix_noise_color.inputs['Factor'])

    # 噪波映射為輕微顏色變化
    darken = nodes.new(type='ShaderNodeRGB')
    darken.location = (-150, 100)
    darken.outputs[0].default_value = (0.06, 0.14, 0.06, 1.0)  # 略深色
    links.new(darken.outputs['Color'], mix_noise_color.inputs['B'])
    links.new(mix_noise_color.outputs['Result'], bsdf.inputs['Base Color'])

    print("[MAT] FR-4 基板材質建立完成")
    return mat


def create_copper_material(name="PCB_Copper"):
    """鍍銅／銅箔材質：亮銅色金屬"""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.85, 0.55, 0.35, 1.0)  # 銅色
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.15
    bsdf.inputs['IOR'].default_value = 1.18  # 銅的折射率

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    return mat


def create_gold_trace_material(name="PCB_Gold_Trace"):
    """ENIG 鍍金走線材質：亮金色"""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.95, 0.78, 0.25, 1.0)  # 金色
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.08  # 極光滑（ENIG 表面處理）
    bsdf.inputs['IOR'].default_value = 0.47  # 金的折射率

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    return mat


def create_solder_mask_material():
    """阻焊層材質：啞光深綠色"""
    mat = bpy.data.materials.new(name="PCB_SolderMask")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.08, 0.20, 0.08, 1.0)  # 深綠
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.45
    bsdf.inputs['IOR'].default_value = 1.5

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    return mat


def create_tin_pad_material():
    """鍍錫焊盤材質：銀灰色金屬（HASL 噴錫外觀）"""
    mat = bpy.data.materials.new(name="PCB_TinPad")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.75, 0.75, 0.72, 1.0)  # 錫灰色
    bsdf.inputs['Metallic'].default_value = 0.95
    bsdf.inputs['Roughness'].default_value = 0.25
    bsdf.inputs['IOR'].default_value = 1.8  # 錫的折射率

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 輔助幾何函數
# ══════════════════════════════════════════════════════════════════════════════

def create_rounded_rect_curve(name, width, length, radius, location=(0, 0, 0)):
    """
    建立一個帶圓角的矩形貝茲曲線（用於倒角擠出或布爾運算）。
    返回 Curve Object。

    參數：
        width:  X 軸方向長度（PCB 的長）
        length: Y 軸方向寬度（PCB 的寬）
        radius: 圓角半徑
        location: 位置
    """
    # 曲線數據
    curve_data = bpy.data.curves.new(name=name, type='CURVE')
    curve_data.dimensions = '2D'
    curve_data.resolution_u = 32  # 圓角平滑度

    # 建立 spline
    spline = curve_data.splines.new(type='POLY')

    hw = width / 2.0   # 半長
    hl = length / 2.0  # 半寬
    r = min(radius, hw, hl)  # 防止圓角超過板子一半

    # 計算內縮點（直線段端點）—— 從角點向內偏移 r
    # 矩形四個角的順序：左下 → 右下 → 右上 → 左上 → 閉合
    # 每個角用兩個控制點定義 Bezier 曲線段

    # 改用 Bezier 曲線以精確控制圓角
    curve_data.splines.remove(spline)
    spline = curve_data.splines.new(type='BEZIER')
    spline.resolution_u = 12

    # 定義 8 個點（4 個角 × 2 個控制點）+ 閉合
    # 順時針：從左下角開始
    corners = [
        # (角點 x, 角點 y, 入控制點偏移方向)
        (-hw, -hl),   # 左下
        ( hw, -hl),   # 右下
        ( hw,  hl),   # 右上
        (-hw,  hl),   # 左上
    ]

    # 為每個角產生 2 個 Bezier 點（進入 + 離開該角）
    pts = []
    for i, (cx, cy) in enumerate(corners):
        # 前一個角的方向（用於計算進入控制點）
        prev = corners[(i - 1) % 4]
        nxt = corners[(i + 1) % 4]

        # 從前一個角到這個角的方向 → 進入方向
        dx_in = cx - prev[0]
        dy_in = cy - prev[1]
        dist_in = math.hypot(dx_in, dy_in)
        if dist_in > 0:
            dx_in /= dist_in
            dy_in /= dist_in

        # 從這個角到下一個角的方向 → 離開方向
        dx_out = nxt[0] - cx
        dy_out = nxt[1] - cy
        dist_out = math.hypot(dx_out, dy_out)
        if dist_out > 0:
            dx_out /= dist_out
            dy_out /= dist_out

        # Bezier 控制點距離（用 kappa 近似 90° 弧 → kappa = 4/3 * tan(θ/4), θ=90° → 4/3*(√2-1) ≈ 0.552）
        kappa = 0.5522847498
        ctrl_dist = r * kappa

        # 點 1：從前一段線"到達"此角（線段終點在 r 之前的邊上）
        p1_x = cx - dx_in * r
        p1_y = cy - dy_in * r
        # 點 2：從此角"離開"到下一個邊（線段起點在 r 之後的邊上）
        p2_x = cx + dx_out * r
        p2_y = cy + dy_out * r

        pts.append({
            'co': (p1_x, p1_y),        # 頂點座標（線段終點）
            'handle_right': (dx_in * ctrl_dist, dy_in * ctrl_dist),  # 右控制柄（指向角點）
            'handle_left': (-dx_out * ctrl_dist, -dy_out * ctrl_dist),  # 左控制柄（也指向角點，用於下一個頂點）
        })

    # 設定 Bezier 點（每個角 2 個頂點，共 8 頂點 + 閉合）
    spline.bezier_points.add(len(pts) - 1)  # 會自動閉合 cyclic

    for idx, pt_data in enumerate(pts):
        bp = spline.bezier_points[idx]
        bp.co = (pt_data['co'][0], pt_data['co'][1], 0.0)
        bp.handle_right_type = 'FREE'
        bp.handle_left_type = 'FREE'

        # 左控制柄是進入控制點（指向角點）
        bp.handle_left = (
            pt_data['co'][0] + pt_data['handle_left'][0],
            pt_data['co'][1] + pt_data['handle_left'][1],
            0.0
        )
        # 右控制柄是離開控制點（指向角點）
        bp.handle_right = (
            pt_data['co'][0] + pt_data['handle_right'][0],
            pt_data['co'][1] + pt_data['handle_right'][1],
            0.0
        )

    spline.use_cyclic_u = True

    # 建立物件
    curve_obj = bpy.data.objects.new(name=name, object_data=curve_data)
    curve_obj.location = location
    bpy.context.collection.objects.link(curve_obj)

    return curve_obj


def cylindrical_hole(obj_name, x, y, z_bottom, z_top, radius, material=None):
    """
    在指定位置建立一個圓柱形孔洞（用於布爾差集運算）。

    參數：
        x, y: 孔中心 XY 座標
        z_bottom: 孔底部 Z（略低於板以確保完全穿透）
        z_top: 孔頂部 Z
        radius: 孔半徑
        material: 材質（如果為 None，則孔洞只是切割體）
    """
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64,
        radius=radius,
        depth=(z_top - z_bottom),
        location=(x, y, (z_bottom + z_top) / 2.0)
    )
    obj = bpy.context.active_object
    obj.name = obj_name
    if material:
        obj.data.materials.append(material)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# 第 3 部分：建立 PCB 基板（含安裝孔 + PTH 鍍銅）
# ══════════════════════════════════════════════════════════════════════════════

def create_pcb_base():
    """
    建立帶有 FR-4 材質、四角安裝孔 + PTH 鍍銅 + 焊盤的 PCB 基板。

    步驟：
        1. 建立帶 2.5mm 圓角的 FR-4 主體
        2. 四角布爾挖出安裝孔
        3. 每個孔內壁建立 PTH 鍍銅環
        4. 每個孔頂/底面建立裸露焊盤（ring pad）
    """
    print("[PCB] 開始建立基板...")

    # --- 3.1 建立 FR-4 基板主體（帶圓角） ---
    # 使用圓角矩形曲線擠出，而非簡單立方體
    curve_obj = create_rounded_rect_curve(
        name="PCB_Outline",
        width=PCB_L,
        length=PCB_W,
        radius=2.5,
        location=(0, 0, 0)
    )

    # 將曲線轉換為網格以便擠出
    bpy.context.view_layer.objects.active = curve_obj
    curve_obj.select_set(True)

    # 轉換曲線為網格
    bpy.ops.object.convert(target='MESH')

    # 擠出厚度（Z 軸方向）
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # 在編輯模式中擠出（Blender 4.2+ 使用 bpy.ops.mesh.extrude_region_move）
    # 注意：擠出方向沿面法線，這裡用的是 XY 平面曲線，法線方向為 Z
    bm_override = bpy.context.temp_override(edit_object=bpy.context.active_object)
    # 直接用 transform 來擠出
    bpy.ops.mesh.extrude_region_move(
        TRANSFORM_OT_translate={"value": (0, 0, PCB_H)}
    )

    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.active_object.name = "PCB_Base_Body"

    # 指定 FR-4 材質
    pcb_body = bpy.context.active_object
    fr4_mat = create_fr4_material()
    pcb_body.data.materials.append(fr4_mat)

    print(f"  [PCB] FR-4 主體: {PCB_L}×{PCB_W}×{PCB_H} mm, 圓角 R2.5")

    # --- 3.2 計算安裝孔座標 ---
    # 四角，孔中心距兩邊各 5mm
    hx = PCB_L / 2.0 - MOUNT_HOLE_MARGIN  # X 方向孔心距中心距離
    hy = PCB_W / 2.0 - MOUNT_HOLE_MARGIN  # Y 方向孔心距中心距離

    hole_positions = [
        (-hx, -hy),  # 左下
        ( hx, -hy),  # 右下
        ( hx,  hy),  # 右上
        (-hx,  hy),  # 左上
    ]
    print(f"  [PCB] 安裝孔位置: ({hx:.1f}, {hy:.1f}) 四角")

    # --- 3.3 用布爾運算挖出安裝孔 ---
    # 先準備材質
    copper_mat = create_copper_material("PCB_PTH_Copper")
    tin_mat = create_tin_pad_material()

    hole_radius = MOUNT_HOLE_DIAMETER / 2.0  # 1.6mm 半徑
    pth_outer_radius = hole_radius + PTH_THICKNESS  # 1.65mm PTH 外半徑
    pad_radius = MOUNT_PAD_DIAMETER / 2.0  # 2.5mm 焊盤半徑

    # 建立所有孔的布爾切割體並儲存引用
    hole_cutters = []

    for idx, (hx_pos, hy_pos) in enumerate(hole_positions):
        # --- 主孔：直徑 3.2mm 完全穿透 ---
        hole_name = f"MountHole_Cutter_{idx+1}"
        hole_obj = cylindrical_hole(
            obj_name=hole_name,
            x=hx_pos,
            y=hy_pos,
            z_bottom=-PCB_H / 2.0 - 1.0,  # 確保完全穿透（多出 1mm）
            z_top=PCB_H / 2.0 + 1.0,
            radius=hole_radius,
            material=None  # 切割體不需要材質
        )
        hole_cutters.append(hole_obj)

        # --- PTH 鍍銅環：外半徑 = 孔半徑 + 0.05mm ---
        # 先建立一個外徑稍大的圓柱，再從中間挖掉
        pth_outer = cylindrical_hole(
            obj_name=f"PTH_Outer_{idx+1}",
            x=hx_pos,
            y=hy_pos,
            z_bottom=-PCB_H / 2.0,
            z_top=PCB_H / 2.0,
            radius=pth_outer_radius,
            material=None
        )
        # 從 PTH 外環中減去內孔（用布爾）
        pth_inner = cylindrical_hole(
            obj_name=f"PTH_InnerCutter_{idx+1}",
            x=hx_pos,
            y=hy_pos,
            z_bottom=-PCB_H / 2.0 - 0.1,
            z_top=PCB_H / 2.0 + 0.1,
            radius=hole_radius,
            material=None
        )

        # 對 PTH 外環做布爾差集
        bool_mod = pth_outer.modifiers.new(name="PTH_Boolean", type='BOOLEAN')
        bool_mod.operation = 'DIFFERENCE'
        bool_mod.object = pth_inner
        bpy.context.view_layer.objects.active = pth_outer
        bpy.ops.object.modifier_apply(modifier=bool_mod.name)

        # 刪除內切割體
        bpy.data.objects.remove(pth_inner, do_unlink=True)

        # 指定 PTH 鍍銅材質
        pth_outer.name = f"PTH_Copper_{idx+1}"
        pth_outer.data.materials.append(copper_mat)
        print(f"  [PTH] 安裝孔 {idx+1}: PTH 鍍銅 Ø{2*pth_outer_radius:.2f}mm, 壁厚 {PTH_THICKNESS}mm")

        # --- 頂面焊盤 Pad（直徑 5.0mm 圓形，厚度 0.035mm） ---
        pad_top = cylindrical_hole(
            obj_name=f"Pad_Top_{idx+1}",
            x=hx_pos,
            y=hy_pos,
            z_bottom=PCB_H / 2.0,               # 從板面開始
            z_top=PCB_H / 2.0 + CU_THICKNESS,   # +0.035mm
            radius=pad_radius,
            material=None
        )
        pad_top.data.materials.append(tin_mat)

        # --- 底面焊盤 Pad ---
        pad_bottom = cylindrical_hole(
            obj_name=f"Pad_Bottom_{idx+1}",
            x=hx_pos,
            y=hy_pos,
            z_bottom=-PCB_H / 2.0 - CU_THICKNESS,
            z_top=-PCB_H / 2.0,
            radius=pad_radius,
            material=None
        )
        pad_bottom.data.materials.append(tin_mat)

        print(f"  [PAD] 安裝孔 {idx+1}: 焊盤 Ø{MOUNT_PAD_DIAMETER}mm × {CU_THICKNESS}mm (頂+底)")

    # --- 3.4 對主體執行布爾差集（挖孔） ---
    bpy.context.view_layer.objects.active = pcb_body
    pcb_body.select_set(True)

    for cutter in hole_cutters:
        bool_mod = pcb_body.modifiers.new(name=f"Bool_{cutter.name}", type='BOOLEAN')
        bool_mod.operation = 'DIFFERENCE'
        bool_mod.object = cutter
        bpy.ops.object.modifier_apply(modifier=bool_mod.name)
        # 隱藏切割體（保留在場景中以備後用）
        cutter.hide_viewport = True
        cutter.hide_render = True

    print("[PCB] ✅ 基板 + 4 個安裝孔 + PTH + 焊盤建立完成\n")
    return pcb_body


# ══════════════════════════════════════════════════════════════════════════════
# 第 4 部分：建立過孔陣列
# ══════════════════════════════════════════════════════════════════════════════

def create_vias_array():
    """
    在 PCB 上的密集走線區域佈置過孔陣列。

    過孔規格：
        - 外徑 0.5mm（含焊環 annular ring）
        - 內徑 0.25mm（鑽孔）
        - 中心中空穿透
        - 阻焊層開窗（solder mask opening = 外徑 + 0.1mm 擴張）

    佈局區域：
        主要佈在 PCB 中央偏上方區域，模擬 BGA 逃逸佈線（escape routing）的過孔陣列。
        間距 1.5mm（中心到中心），足以容納 0.5mm 外徑 + 0.15mm 走線通過。
    """
    print("[VIAS] 開始建立過孔陣列...")

    copper_mat = create_copper_material("PCB_Via_Copper")
    mask_mat = create_solder_mask_material()

    via_outer_r = VIA_OUTER_DIAMETER / 2.0  # 0.25mm 外半徑
    via_inner_r = VIA_INNER_DIAMETER / 2.0  # 0.125mm 內半徑
    via_pitch = 1.5  # 過孔間距（中心到中心）mm

    # --- 定義過孔陣列區域 ---
    # 區域 1：左上密集區（模擬高速數字晶片下方過孔陣列）
    # 區域範圍 X: [-45, -10], Y: [5, 35]
    region1_bounds = {
        'x_min': -45.0, 'x_max': -10.0,
        'y_min': 5.0, 'y_max': 35.0,
        'cols': 6, 'rows': 4
    }
    # 區域 2：右上密集區（模擬記憶體模組下方）
    region2_bounds = {
        'x_min': 15.0, 'x_max': 55.0,
        'y_min': 5.0, 'y_max': 35.0,
        'cols': 7, 'rows': 3
    }

    regions = [region1_bounds, region2_bounds]
    via_count = 0
    via_target = VIA_COUNT

    board_half_z = PCB_H / 2.0
    # Solder mask opening 半徑：比外徑大 0.1mm
    mask_opening_r = via_outer_r + 0.1  # 0.35mm

    for region_idx, region in enumerate(regions):
        # 計算網格間距
        x_step = (region['x_max'] - region['x_min']) / max(region['cols'] - 1, 1)
        y_step = (region['y_max'] - region['y_min']) / max(region['rows'] - 1, 1)

        for row in range(region['rows']):
            for col in range(region['cols']):
                if via_count >= via_target:
                    break

                # 計算過孔中心座標（加入輕微隨機偏移以模擬實際佈線不完美）
                x_pos = region['x_min'] + col * x_step + random.uniform(-0.05, 0.05)
                y_pos = region['y_min'] + row * y_step + random.uniform(-0.05, 0.05)

                idx = via_count + 1

                # --- 4.1 過孔外環（銅焊環） ---
                via_outer = cylindrical_hole(
                    obj_name=f"Via_Outer_{idx}",
                    x=x_pos,
                    y=y_pos,
                    z_bottom=-board_half_z,
                    z_top=board_half_z,
                    radius=via_outer_r,
                    material=None
                )
                via_outer.data.materials.append(copper_mat)

                # --- 4.2 中心鑽孔（中空穿透） ---
                # 建立一個比內徑稍大的切割體
                drill_cutter = cylindrical_hole(
                    obj_name=f"Via_Drill_{idx}",
                    x=x_pos,
                    y=y_pos,
                    z_bottom=-board_half_z - 0.2,
                    z_top=board_half_z + 0.2,
                    radius=via_inner_r,
                    material=None
                )

                # 布爾差集：外環減去內孔 = 中空環
                bool_mod = via_outer.modifiers.new(name="ViaDrill", type='BOOLEAN')
                bool_mod.operation = 'DIFFERENCE'
                bool_mod.object = drill_cutter
                bpy.context.view_layer.objects.active = via_outer
                bpy.ops.object.modifier_apply(modifier=bool_mod.name)

                # 隱藏鑽孔切割體
                drill_cutter.hide_viewport = True
                drill_cutter.hide_render = True

                # --- 4.3 頂面阻焊層開窗 ---
                # 阻焊層開窗 = 在阻焊層上挖掉比外徑大 0.1mm 的圓形區域
                # 這裡我們在頂面和底面分別建立一個薄圓盤（代表阻焊層）然後在過孔處開窗
                # 但為了簡化，我們用一個環形來表示"開窗效果"
                # 實際建模：阻焊層是覆蓋整板的一層，開窗處被挖掉
                # 此處建立微小的圓環標記開窗邊界即可

                # 頂面開窗環（銅環頂部的裸露區域標記）
                mask_top = cylindrical_hole(
                    obj_name=f"Via_MaskOpen_Top_{idx}",
                    x=x_pos,
                    y=y_pos,
                    z_bottom=board_half_z,
                    z_top=board_half_z + SOLDERMASK_THICKNESS,  # 0.02mm 阻焊層厚度
                    radius=mask_opening_r,
                    material=None
                )
                # 開窗處：將阻焊環中間挖掉（露出銅環），只保留外圈的 ring
                mask_inner = cylindrical_hole(
                    obj_name=f"Via_MaskInner_Top_{idx}",
                    x=x_pos,
                    y=y_pos,
                    z_bottom=board_half_z - 0.01,
                    z_top=board_half_z + SOLDERMASK_THICKNESS + 0.01,
                    radius=via_outer_r,  # 露出銅環
                    material=None
                )
                bool_mod2 = mask_top.modifiers.new(name="MaskOpen", type='BOOLEAN')
                bool_mod2.operation = 'DIFFERENCE'
                bool_mod2.object = mask_inner
                bpy.context.view_layer.objects.active = mask_top
                bpy.ops.object.modifier_apply(modifier=bool_mod2.name)
                bpy.data.objects.remove(mask_inner, do_unlink=True)
                mask_top.data.materials.append(mask_mat)
                mask_top.hide_viewport = True
                mask_top.hide_render = True  # 開窗標記在渲染中隱藏（太薄看不到）

                via_count += 1

            if via_count >= via_target:
                break
        if via_count >= via_target:
            break

    print(f"[VIAS] ✅ 過孔陣列建立完成: 共 {via_count} 個過孔")
    print(f"  - 外徑 Ø{VIA_OUTER_DIAMETER}mm, 內徑 Ø{VIA_INNER_DIAMETER}mm, 中心穿透")
    print(f"  - 阻焊開窗 Ø{2*mask_opening_r:.2f}mm\n")
    return via_count


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：建立差動信號蛇形走線
# ══════════════════════════════════════════════════════════════════════════════

def create_differential_traces():
    """
    建立 3 對嚴格平行的差分蛇形（Serpentine）高速信號走線。

    規格：
        - 線寬 0.15mm, 線距 0.15mm（邊到邊）, 差分阻抗 ~100Ω
        - 立體厚度 0.035mm（1oz 銅）
        - 拐角嚴格由兩個 45° 角組成（135° 鈍角轉彎）
        - 材質：ENIG 鍍亮金

    蛇形走線設計（Serpentine Routing）：
        高速並行匯流排（如 DDR 或 PCIe）為了滿足時序匹配（length matching），
        需要在較短的信號路徑上加入蛇形延遲線。

        蛇形線 = 主信號路徑 + 週期性左右擺動來增加總長度。

        每個週期包含：
            1. 45° 轉彎（向右偏移）
            2. 平行段（橫移後的直線）
            3. 45° 轉彎（回到原方向）
            4. 平行段
            5. 45° 轉彎（向左偏移）
            6. 平行段
            7. 45° 轉彎（回到原方向）
            8. 平行段
        這樣形成一個完整的"蛇形"凸起。
    """
    print("[TRACES] 開始建立差分蛇形走線...")

    gold_mat = create_gold_trace_material("PCB_DiffPair_Gold")

    # PCB 表面 Z 座標（走線在頂層銅箔上方）
    trace_z_bottom = PCB_H / 2.0                    # 板面上表面
    trace_z_top = trace_z_bottom + TRACE_THICKNESS  # +0.035mm 銅厚

    # --- 5.1 蛇形線數學模型 ---
    # 使用正弦函數疊加線性前進的曲線來定義蛇形中線路徑
    # 但為了精確控制（特別是 45° 拐角），我們改用分段折線法

    # 每個蛇形週期的幾何參數
    serpentine_amplitude = 2.0   # 蛇形擺幅（橫向偏移量）mm
    serpentine_period = 6.0      # 一個完整蛇形週期的長度 mm

    def generate_serpentine_centerline(start_x, start_y, direction_angle, total_length,
                                        amplitude, period, num_periods):
        """
        生成蛇形走線的中心線頂點序列。

        參數：
            start_x, start_y: 起點
            direction_angle: 主前進方向（弧度，0 = +X）
            total_length: 總長度
            amplitude: 蛇形振幅（垂直於主方向的偏移）
            period: 蛇形週期（沿主方向的長度）
            num_periods: 蛇形週期數

        返回：
            [(x, y), ...] 頂點列表

        設計細節：
            每個蛇形週期由 8 個線段組成：
            - 每個拐角使用兩個 45° 線段來合成一個 135° 鈍角
            - 這樣走線中不存在 90° 直角，符合高速信號完整性要求

            週期結構（從中線開始，以 +Y 方向擺動為例）：
              seg1: 45° 向上（dx=+step, dy=+step）  → 到達擺幅一半
              seg2: 45° 向上（dx=+step, dy=+step）  → 到達擺幅頂點
              seg3: 直線前進（dx=+step, dy=0）      → 平行段
              seg4: 45° 向下（dx=+step, dy=-step）  → 回到一半
              seg5: 45° 向下（dx=+step, dy=-step）  → 回到中線
              seg6: 直線前進（dx=+step, dy=0）      → 平行段
              seg7: 45° 向下（dx=+step, dy=-step）  → 到達擺幅一半（負）
              seg8: 45° 向下（dx=+step, dy=-step）  → 到達擺幅底點
              seg9: 直線前進（dx=+step, dy=0）      → 平行段
              seg10: 45° 向上（dx=+step, dy=+step） → 回到一半
              seg11: 45° 向上（dx=+step, dy=+step） → 回到中線
              seg12: 直線前進（dx=+step, dy=0）     → 平行段
        """
        cos_a = math.cos(direction_angle)
        sin_a = math.sin(direction_angle)

        # 計算 45° 對角線的步長
        # 45° 線段在 X 方向走 step_45，則 Y 方向也走 step_45
        # 兩段 45° 需要覆蓋半個振幅（amp/2）
        # 所以 step_45 = amp / 4（兩段 45° = amp/2）
        step_45 = amplitude / 4.0  # 每個 45° 線段的橫向位移

        # 直線段的步長（平行於主方向的行程）
        # 每個週期有 4 個 45° 對角線段 + 4 個直線段
        # 總前進距離 = 4 * step_45（對角線的前進分量）+ 4 * straight_len = period
        straight_len = (period - 4 * step_45) / 4.0

        points = [(start_x, start_y)]

        for p in range(num_periods):
            # 當前位置
            cx, cy = points[-1]

            # --- 上半週期（向右/上擺動） ---
            # Seg 1: 45° 前進 + 偏移
            points.append((cx + step_45 * cos_a - step_45 * sin_a,
                           cy + step_45 * sin_a + step_45 * cos_a))
            # Seg 2: 45° 前進 + 偏移（完成上半）
            cx2, cy2 = points[-1]
            points.append((cx2 + step_45 * cos_a - step_45 * sin_a,
                           cy2 + step_45 * sin_a + step_45 * cos_a))
            # Seg 3: 直線前進（保持偏移）
            cx3, cy3 = points[-1]
            points.append((cx3 + straight_len * cos_a,
                           cy3 + straight_len * sin_a))
            # Seg 4: 45° 回到中線（前半）
            cx4, cy4 = points[-1]
            points.append((cx4 + step_45 * cos_a + step_45 * sin_a,
                           cy4 + step_45 * sin_a - step_45 * cos_a))
            # Seg 5: 45° 回到中線（後半）
            cx5, cy5 = points[-1]
            points.append((cx5 + step_45 * cos_a + step_45 * sin_a,
                           cy5 + step_45 * sin_a - step_45 * cos_a))
            # Seg 6: 直線前進（在中線）
            cx6, cy6 = points[-1]
            points.append((cx6 + straight_len * cos_a,
                           cy6 + straight_len * sin_a))

            # --- 下半週期（向左/下擺動） ---
            # Seg 7: 45° 前進 - 偏移
            cx7, cy7 = points[-1]
            points.append((cx7 + step_45 * cos_a + step_45 * sin_a,
                           cy7 + step_45 * sin_a - step_45 * cos_a))
            # Seg 8: 45° 前進 - 偏移（完成下半）
            cx8, cy8 = points[-1]
            points.append((cx8 + step_45 * cos_a + step_45 * sin_a,
                           cy8 + step_45 * sin_a - step_45 * cos_a))
            # Seg 9: 直線前進（保持負偏移）
            cx9, cy9 = points[-1]
            points.append((cx9 + straight_len * cos_a,
                           cy9 + straight_len * sin_a))
            # Seg 10: 45° 回到中線（前半）
            cx10, cy10 = points[-1]
            points.append((cx10 + step_45 * cos_a - step_45 * sin_a,
                           cy10 + step_45 * sin_a + step_45 * cos_a))
            # Seg 11: 45° 回到中線（後半）
            cx11, cy11 = points[-1]
            points.append((cx11 + step_45 * cos_a - step_45 * sin_a,
                           cy11 + step_45 * sin_a + step_45 * cos_a))
            # Seg 12: 直線前進（在中線）
            cx12, cy12 = points[-1]
            points.append((cx12 + straight_len * cos_a,
                           cy12 + straight_len * sin_a))

        return points

    # --- 5.2 將中心線頂點序列轉換為 3D 網格（帶厚度） ---
    def centerline_to_mesh(name, points, width, z_bottom, z_top):
        """
        將 2D 中心線頂點序列轉換為帶寬度和厚度的 3D 網格。

        方法：
            沿中心線的每個線段建立一個四邊形面（兩個三角形），
            然後向下擠出形成立體厚度。

        參數：
            name: 物體名稱
            points: [(x, y), ...] 中心線頂點
            width: 走線總寬度
            z_bottom: 底部 Z
            z_top: 頂部 Z
        """
        half_w = width / 2.0

        # 建立頂面和底面頂點
        top_verts = []
        bottom_verts = []

        for i, (px, py) in enumerate(points):
            # 計算該點處的切線方向
            if i == 0:
                # 第一點：使用到下一點的方向
                dx = points[1][0] - px
                dy = points[1][1] - py
            elif i == len(points) - 1:
                # 最後一點：使用從上一點來的方向
                dx = px - points[-2][0]
                dy = py - points[-2][1]
            else:
                # 中間點：使用前後點的平均方向（使拐角平滑）
                dx = points[i + 1][0] - points[i - 1][0]
                dy = points[i + 1][1] - points[i - 1][1]

            length = math.hypot(dx, dy)
            if length < 1e-9:
                # 退化情況，使用前一個法線
                if i > 0:
                    dx = points[i][0] - points[i-1][0]
                    dy = points[i][1] - points[i-1][1]
                    length = math.hypot(dx, dy)
                if length < 1e-9:
                    length = 1.0

            # 垂直於切線的單位法向量
            nx = -dy / length
            ny = dx / length

            # 左右兩個邊界點
            left_x = px - nx * half_w
            left_y = py - ny * half_w
            right_x = px + nx * half_w
            right_y = py + ny * half_w

            top_verts.append(((left_x, left_y, z_top), (right_x, right_y, z_top)))
            bottom_verts.append(((left_x, left_y, z_bottom), (right_x, right_y, z_bottom)))

        # 建立網格
        mesh = bpy.data.meshes.new(name=name)
        obj = bpy.data.objects.new(name=name, object_data=mesh)
        bpy.context.collection.objects.link(obj)

        # 收集所有頂點和面
        verts = []
        faces = []

        # 每個中心線點 → 4 個頂點（頂左、頂右、底左、底右）
        for i in range(len(top_verts)):
            tl = top_verts[i][0]  # top-left
            tr = top_verts[i][1]  # top-right
            bl = bottom_verts[i][0]  # bottom-left
            br = bottom_verts[i][1]  # bottom-right

            idx_tl = len(verts)
            verts.append(tl)
            idx_tr = len(verts)
            verts.append(tr)
            idx_bl = len(verts)
            verts.append(bl)
            idx_br = len(verts)
            verts.append(br)

            # 與下一點之間的面
            if i < len(top_verts) - 1:
                # 下一組頂點的索引（先算好）
                next_base = len(verts)  # 下一個 i 的 tl 的索引
                next_tl = next_base
                next_tr = next_base + 1
                next_bl = next_base + 2
                next_br = next_base + 3

                # 頂面（順時針）
                faces.append((idx_tl, idx_tr, next_tr, next_tl))
                # 底面（逆時針）
                faces.append((idx_bl, idx_br, next_br, next_bl))
                # 前面（朝外側）
                faces.append((idx_tr, idx_br, next_br, next_tr))
                # 背面（朝內側）
                faces.append((idx_tl, idx_bl, next_bl, next_tl))

        # 建立網格
        mesh.from_pydata(verts, [], faces)
        mesh.update()

        return obj

    # --- 5.3 建立 3 對差分走線 ---
    # 每對差分走線包含兩條嚴格平行的蛇形線，間距 = TRACE_SPACING（邊到邊）
    # 中心到中心間距 = TRACE_WIDTH + TRACE_SPACING = 0.30mm

    pair_pitch = TRACE_WIDTH + TRACE_SPACING  # 差分對內兩線的中心間距

    # 定義 3 對差分走線的參數
    # 每對的位置和走向不同
    diff_pair_configs = [
        {
            'name': 'DiffPair_A_PCIe_TX',
            'start_x': -50.0, 'start_y': -25.0,
            'angle': 0.0,  # 沿 +X 方向
            'total_length': 80.0,
            'amplitude': 1.8,
            'period': 5.5,
            'num_periods': 5,
        },
        {
            'name': 'DiffPair_B_DDR_DQ0',
            'start_x': -45.0, 'start_y': -15.0,
            'angle': math.radians(15),  # 15° 斜向
            'total_length': 70.0,
            'amplitude': 1.5,
            'period': 5.0,
            'num_periods': 4,
        },
        {
            'name': 'DiffPair_C_USB3_SSRX',
            'start_x': -48.0, 'start_y': -5.0,
            'angle': math.radians(-10),  # -10° 斜向
            'total_length': 75.0,
            'amplitude': 2.0,
            'period': 6.0,
            'num_periods': 5,
        },
    ]

    trace_objects = []

    for pair_cfg in diff_pair_configs:
        pair_name = pair_cfg['name']
        print(f"  [DIFF] 建立差分對: {pair_name}")

        # 計算差分對中兩條線的偏移起點
        # P 線（正極）和 N 線（負極），間距 0.30mm 中心到中心
        # 偏移方向垂直於主前進方向
        perp_angle = pair_cfg['angle'] + math.pi / 2.0  # 垂直方向

        offset_p_x = math.cos(perp_angle) * (pair_pitch / 2.0)
        offset_p_y = math.sin(perp_angle) * (pair_pitch / 2.0)
        offset_n_x = -offset_p_x
        offset_n_y = -offset_p_y

        # --- P 線（正信號線） ---
        start_p_x = pair_cfg['start_x'] + offset_p_x
        start_p_y = pair_cfg['start_y'] + offset_p_y

        centerline_p = generate_serpentine_centerline(
            start_x=start_p_x,
            start_y=start_p_y,
            direction_angle=pair_cfg['angle'],
            total_length=pair_cfg['total_length'],
            amplitude=pair_cfg['amplitude'],
            period=pair_cfg['period'],
            num_periods=pair_cfg['num_periods']
        )

        trace_p = centerline_to_mesh(
            name=f"{pair_name}_P",
            points=centerline_p,
            width=TRACE_WIDTH,
            z_bottom=trace_z_bottom,
            z_top=trace_z_top
        )
        trace_p.data.materials.append(gold_mat)
        trace_objects.append(trace_p)

        # --- N 線（負信號線，嚴格平行於 P 線） ---
        start_n_x = pair_cfg['start_x'] + offset_n_x
        start_n_y = pair_cfg['start_y'] + offset_n_y

        centerline_n = generate_serpentine_centerline(
            start_x=start_n_x,
            start_y=start_n_y,
            direction_angle=pair_cfg['angle'],
            total_length=pair_cfg['total_length'],
            amplitude=pair_cfg['amplitude'],
            period=pair_cfg['period'],
            num_periods=pair_cfg['num_periods']
        )

        trace_n = centerline_to_mesh(
            name=f"{pair_name}_N",
            points=centerline_n,
            width=TRACE_WIDTH,
            z_bottom=trace_z_bottom,
            z_top=trace_z_top
        )
        trace_n.data.materials.append(gold_mat)
        trace_objects.append(trace_n)

        print(f"    線寬 {TRACE_WIDTH}mm, 線距 {TRACE_SPACING}mm, "
              f"蛇形週期 ×{pair_cfg['num_periods']}, 振幅 {pair_cfg['amplitude']}mm")

    print(f"[TRACES] ✅ 差分走線建立完成: 共 {len(diff_pair_configs)} 對 / {len(trace_objects)} 條走線\n")
    return trace_objects


# ══════════════════════════════════════════════════════════════════════════════
# 第 6 部分：場景燈光與相機
# ══════════════════════════════════════════════════════════════════════════════

def setup_scene():
    """建立渲染用的燈光和相機"""
    print("[SCENE] 建立燈光和相機...")

    # --- 環境光（HDRI 風格，用 Area Light 模擬） ---
    # 頂光（模擬無塵室頂燈）
    bpy.ops.object.light_add(type='AREA', location=(0, 0, 80))
    top_light = bpy.context.active_object
    top_light.name = "Light_Top"
    top_light.data.energy = 500
    top_light.data.size = 10.0
    top_light.data.shape = 'RECTANGLE'
    top_light.data.size_y = 6.0

    # 側光（45° 補光，突出 PCB 紋理和走線立體感）
    bpy.ops.object.light_add(type='AREA', location=(80, 60, 50))
    side_light1 = bpy.context.active_object
    side_light1.name = "Light_Side1"
    side_light1.data.energy = 300
    side_light1.data.size = 8.0
    side_light1.data.shape = 'RECTANGLE'
    side_light1.data.size_y = 5.0

    # 另一側補光
    bpy.ops.object.light_add(type='AREA', location=(-70, -50, 40))
    side_light2 = bpy.context.active_object
    side_light2.name = "Light_Side2"
    side_light2.data.energy = 200
    side_light2.data.size = 6.0

    # 底光（弱光，展現 FR-4 半透明）
    bpy.ops.object.light_add(type='AREA', location=(0, 0, -50))
    bot_light = bpy.context.active_object
    bot_light.name = "Light_Bottom"
    bot_light.data.energy = 80
    bot_light.data.size = 12.0

    # --- 相機 ---
    bpy.ops.object.camera_add(location=(120, -80, 70))
    camera = bpy.context.active_object
    camera.name = "Camera_Main"

    # 讓相機看向 PCB 中心
    look_at = bpy.data.objects.new("Camera_Target", None)
    bpy.context.collection.objects.link(look_at)
    look_at.location = (0, 0, 0)

    constraint = camera.constraints.new(type='TRACK_TO')
    constraint.target = look_at
    constraint.track_axis = 'TRACK_NEGATIVE_Z'
    constraint.up_axis = 'UP_Y'

    # 設定為場景相機
    bpy.context.scene.camera = camera

    # 渲染解析度
    bpy.context.scene.render.resolution_x = 1920
    bpy.context.scene.render.resolution_y = 1080
    bpy.context.scene.render.resolution_percentage = 100

    print("[SCENE] ✅ 燈光與相機建立完成\n")


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ATOS PRO — PCB 超逼真建模工具 v1.0")
    print("  步驟 1：初始化 + 基板 + 過孔 + 差分走線")
    print("=" * 60)
    print()

    # 1. 初始化
    init_environment()

    # 2. 建立 PCB 基板
    pcb = create_pcb_base()

    # 3. 建立過孔陣列
    vias = create_vias_array()

    # 4. 建立差分信號走線
    traces = create_differential_traces()

    # 5. 場景燈光與相機
    setup_scene()

    # --- 設定視口著色模式 ---
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'
                    space.shading.use_scene_lights = True
                    space.shading.use_scene_world = True
            break

    # 輸出統計
    total_objects = len(bpy.data.objects)
    total_vertices = sum(len(obj.data.vertices) for obj in bpy.data.objects if obj.type == 'MESH')
    print("=" * 60)
    print(f"  ✅ 步驟 1 完成！")
    print(f"  總物件數: {total_objects}")
    print(f"  總頂點數: {total_vertices:,}")
    print(f"  PCB: {PCB_L}×{PCB_W}×{PCB_H}mm FR-4")
    print(f"  安裝孔: 4 個 Ø{MOUNT_HOLE_DIAMETER}mm + PTH + 焊盤")
    print(f"  過孔陣列: {vias} 個 Ø{VIA_OUTER_DIAMETER}/{VIA_INNER_DIAMETER}mm")
    print(f"  差分走線: {len(traces)} 條, {TRACE_WIDTH}mm 線寬, ENIG 鍍金")
    print("=" * 60)
    print()
    print("📌 步驟 1 完成。等待下一步指令...")
