"""
ATOS_PRO/tools/pcb_modeler_step2.py
Blender 4.2+ / Python 3.11 — PCB 超逼真建模脚本
步驟 2：BGA 晶片 + 射頻屏蔽罩 + 板載天線

與步驟 1 的參數和材質命名完全相容。
執行前請先執行步驟 1，或將此檔案 import 後調用。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import math

# ══════════════════════════════════════════════════════════════════════════════
# 從步驟 1 繼承的全局參數（確保相容性）
# ══════════════════════════════════════════════════════════════════════════════

PCB_L = 140.0
PCB_W = 90.0
PCB_H = 1.6

CU_THICKNESS = 0.035       # 1oz 銅箔
SOLDERMASK_THICKNESS = 0.02
SILKSCREEN_THICKNESS = 0.01

# Z-Fighting 防止偏移量：所有重疊面至少偏移此值
ZF_OFFSET = 0.002  # mm

# PCB 頂面 Z 座標（所有元件放置在此面上方）
BOARD_TOP_Z = PCB_H / 2.0


# ══════════════════════════════════════════════════════════════════════════════
# 輔助工具函數
# ══════════════════════════════════════════════════════════════════════════════

def _make_cube(name, x, y, z, sx, sy, sz, material=None):
    """
    建立一個立方體（或扁平長方體），用於元件本體。

    參數：
        x, y, z: 中心位置
        sx, sy, sz: 三軸全長（不是半徑）
        material: 可選材質
    """
    bpy.ops.mesh.primitive_cube_add(
        size=1.0,
        location=(x, y, z)
    )
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (sx, sy, sz)

    # 應用縮放以便後續布爾運算正常
    bpy.ops.object.transform_apply(scale=True)

    if material:
        obj.data.materials.append(material)
    return obj


def _cylinder(name, x, y, z_bottom, z_top, radius, vertices=64, material=None):
    """
    建立圓柱體。返回物件引用。

    參數：
        z_bottom, z_top: Z 軸底部和頂部絕對座標
        radius: 半徑
    """
    height = z_top - z_bottom
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=vertices,
        radius=radius,
        depth=height,
        location=(x, y, (z_bottom + z_top) / 2.0)
    )
    obj = bpy.context.active_object
    obj.name = name
    if material:
        obj.data.materials.append(material)
    return obj


def _sphere(name, x, y, z, radius, material=None):
    """建立一個 UV 球體（用於錫球）。"""
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=radius,
        location=(x, y, z),
        segments=32,
        ring_count=16
    )
    obj = bpy.context.active_object
    obj.name = name
    if material:
        obj.data.materials.append(material)
    return obj


def _boolean_difference(target, cutter, apply=True):
    """
    對 target 執行布爾差集（target - cutter）。
    操作後可選擇刪除 cutter。

    返回 target。
    """
    # 確保 target 是 active
    bpy.context.view_layer.objects.active = target
    target.select_set(True)

    mod = target.modifiers.new(name=f"Bool_{cutter.name}", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter

    if apply:
        # Blender 4.2+ 的 modifier_apply 用法
        bpy.ops.object.modifier_apply(modifier=mod.name)
        # 隱藏 cutter（保留在場景中以備檢查）
        cutter.hide_viewport = True
        cutter.hide_render = True

    return target


def _apply_chamfer_to_top_edges(obj, chamfer_size, segments=3):
    """
    對物體頂面的邊緣執行倒角（Bevel）。
    這是用 Bevel modifier 實現的簡化版。

    策略：
        使用 Bevel modifier，限定只對頂面的邊進行倒角。
        透過 Weight 或 Angle 來控制哪些邊被倒角。

    簡化做法：對所有 ≥ 80° 的銳邊做倒角（這會涵蓋頂面邊緣）。
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mod = obj.modifiers.new(name="TopChamfer", type='BEVEL')
    mod.width = chamfer_size
    mod.segments = segments
    mod.limit_method = 'ANGLE'
    mod.angle_limit = 1.39626  # 弧度 ≈ 80°（只對接近 90° 的邊倒角）

    bpy.ops.object.modifier_apply(modifier=mod.name)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# 材質工廠（與步驟 1 命名相容）
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_create_material(name, color, metallic, roughness, ior=1.5,
                             transmission=0.0, alpha=1.0, create_func=None):
    """
    獲取已存在的材質，或建立新材質。
    這樣可以與步驟 1 的材質庫無縫整合。
    """
    # 先檢查是否已存在
    if name in bpy.data.materials:
        return bpy.data.materials[name]

    # 如果提供了自定義建立函數，使用它
    if create_func:
        return create_func()

    # 否則用通用 Principled BSDF
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (*color, 1.0)
    bsdf.inputs['Metallic'].default_value = metallic
    bsdf.inputs['Roughness'].default_value = roughness
    bsdf.inputs['IOR'].default_value = ior
    bsdf.inputs['Transmission Weight'].default_value = transmission
    bsdf.inputs['Alpha'].default_value = alpha

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print(f"  [MAT] 建立材質: {name}")
    return mat


def create_molded_resin_material():
    """
    BGA 封裝樹脂材質：黑色霧面，頂部帶微小粗糙度變化。
    模擬 IC 封裝的 Epoxy Mold Compound (EMC)。
    """
    mat = bpy.data.materials.new(name="PCB_MoldedResin")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # --- 顏色（深黑帶極微暖色） ---
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.04, 0.04, 0.045, 1.0)  # 極深灰黑
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.55       # 霧面（EMC 典型粗糙度）
    bsdf.inputs['IOR'].default_value = 1.55             # 環氧樹脂折射率
    bsdf.inputs['Specular IOR Level'].default_value = 0.15  # 低反射

    # --- 微細表面紋理（封裝模具痕跡） ---
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-200, -200)
    noise.inputs['Scale'].default_value = 300.0
    noise.inputs['Detail'].default_value = 6.0
    noise.inputs['Roughness'].default_value = 0.65

    bump = nodes.new(type='ShaderNodeBump')
    bump.location = (0, -200)
    bump.inputs['Strength'].default_value = 0.03
    bump.inputs['Distance'].default_value = 0.005
    links.new(noise.outputs['Fac'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print("[MAT] BGA 封裝樹脂材質建立完成")
    return mat


def create_solder_ball_material():
    """
    SAC305 熔融焊錫球材質：亮銀灰色，完全金屬度。
    焊錫在回流焊後形成的平滑球面。
    """
    mat = bpy.data.materials.new(name="PCB_SolderBall")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    # 焊錫色：帶輕微暖色調的銀灰（SAC305 = Sn96.5/Ag3.0/Cu0.5）
    bsdf.inputs['Base Color'].default_value = (0.72, 0.71, 0.68, 1.0)
    bsdf.inputs['Metallic'].default_value = 1.0        # 完全金屬
    bsdf.inputs['Roughness'].default_value = 0.15      # 焊錫光澤
    bsdf.inputs['IOR'].default_value = 1.9             # 錫合金折射率

    # 輕微的各向異性（焊錫流動紋理）
    bsdf.inputs['Anisotropic'].default_value = 0.08
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.3

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print("[MAT] SAC305 焊錫球材質建立完成")
    return mat


def create_shield_can_material():
    """
    屏蔽罩材質：亮銀色拉絲金屬（Brushed Nickel / Tin-plated Steel）。
    使用各向異性反射模擬拉絲紋理。
    """
    mat = bpy.data.materials.new(name="PCB_ShieldCan")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    # 亮銀色（鍍錫鋼板 / 洋白銅）
    bsdf.inputs['Base Color'].default_value = (0.82, 0.83, 0.81, 1.0)
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.22      # 輕微拉絲粗糙度
    bsdf.inputs['IOR'].default_value = 1.45            # 鎳/鋼折射率

    # 拉絲紋理（各向異性）
    bsdf.inputs['Anisotropic'].default_value = 0.35    # 單向拉絲
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.0

    # 微細表面噪波（拉絲深淺變化）
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-200, -200)
    noise.inputs['Scale'].default_value = 500.0
    noise.inputs['Detail'].default_value = 4.0
    noise.inputs['Roughness'].default_value = 0.5

    # 將噪波映射到粗糙度的微調
    math_mult = nodes.new(type='ShaderNodeMath')
    math_mult.location = (0, -200)
    math_mult.operation = 'MULTIPLY'
    math_mult.inputs[1].default_value = 0.08  # 微小變動幅度
    links.new(noise.outputs['Fac'], math_mult.inputs[0])

    math_add = nodes.new(type='ShaderNodeMath')
    math_add.location = (100, -200)
    math_add.operation = 'ADD'
    math_add.inputs[0].default_value = bsdf.inputs['Roughness'].default_value
    links.new(math_mult.outputs['Value'], math_add.inputs[1])
    links.new(math_add.outputs['Value'], bsdf.inputs['Roughness'])

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print("[MAT] 屏蔽罩拉絲金屬材質建立完成")
    return mat


def create_antenna_copper_material():
    """
    天線銅箔材質：純銅色，與步驟 1 的銅材質相容但更亮。
    用於可見的天線走線區域。
    """
    mat = bpy.data.materials.new(name="PCB_AntennaCopper")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.88, 0.58, 0.36, 1.0)  # 純銅色
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.12
    bsdf.inputs['IOR'].default_value = 1.18  # 銅折射率

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print("[MAT] 天線銅箔材質建立完成")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 核心函數 1：BGA 核心晶片
# ══════════════════════════════════════════════════════════════════════════════

def create_bga_chip(x, y, name="BGA_Chip"):
    """
    建立一個完整的 BGA（Ball Grid Array）封裝晶片。

    包含：
        1. 黑色霧面樹脂本體（15×15×1.2mm，頂部邊緣 0.2mm 倒角）
        2. 底部 12×12 錫球陣列（Ø0.4mm，間距 0.8mm）
        3. 每個錫球下方的圓形銅焊盤（Ø0.35mm，厚度 0.035mm）

    參數：
        x, y: 晶片中心在 PCB 上的 XY 座標
        name: 物件名稱前綴

    幾何層次結構（從上到下）：
        ┌─────────────────────────┐  ← 頂面，0.2mm 倒角
        │   Molded Resin Body     │  高度 1.2mm
        │   15×15mm               │
        ├─────────────────────────┤  ← 底面（Z = board_top + 0.035 + standoff）
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │  12×12 錫球陣列
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │  Ø0.4mm 球
        │  ... (12 rows)         │  間距 0.8mm
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │
        ├─────────────────────────┤  ← 焊盤層（PCB 頂面 + CU_THICKNESS + ZF_OFFSET）
        │  ○○○○○○○○○○○○  │  12×12 銅焊盤
        │  ═══════════════════  │  ← PCB 頂面
        └─────────────────────────┘

    尺寸參考：
        - 本體: 15×15×1.2mm（標準 BGA-144 封裝近似）
        - 錫球直徑: 0.4mm（回流後）
        - 錫球間距: 0.8mm（BGA pitch）
        - 焊盤直徑: 0.35mm（NSMD pad）
        - Standoff: 0.25mm（晶片底面到 PCB 面的距離）
    """
    print(f"\n[BGA] 建立 BGA 晶片 @ ({x:.1f}, {y:.1f})")

    # --- 材料 ---
    resin_mat = create_molded_resin_material()
    solder_mat = create_solder_ball_material()
    # 銅焊盤材質：嘗試重用步驟 1 的銅材質
    copper_mat = _get_or_create_material(
        "PCB_Copper", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18
    )

    # --- BGA 幾何參數 ---
    body_size = 15.0          # 本體長寬 mm
    body_height = 1.2         # 本體厚度 mm
    chamfer = 0.2             # 頂部邊緣倒角 mm
    ball_diameter = 0.4       # 錫球直徑 mm
    ball_pitch = 0.8          # 錫球間距（中心到中心）mm
    grid_size = 12            # N×N 陣列
    pad_diameter = 0.35       # 焊盤直徑（NSMD = 比球徑小）mm
    standoff = 0.25           # 晶片底面與 PCB 面的間距（含焊盤高度）mm

    # Z 軸座標計算：
    # PCB 頂面 = BOARD_TOP_Z
    # 焊盤頂面 = BOARD_TOP_Z + CU_THICKNESS（銅箔在板面上）
    # 錫球底部 = 焊盤頂面 + ZF_OFFSET（防止 Z-fighting）
    # 錫球中心 Z = 錫球底部 + 球半徑
    # 晶片底面 = 錫球頂部（壓在球上）
    # 晶片頂面 = 晶片底面 + body_height

    pad_top_z = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET     # 焊盤的頂面
    # 錫球是完整的球體，底部與焊盤接觸
    # 球心略高於焊盤頂面（因為球的下半部會"浸入"焊盤的 ZF_OFFSET 空間）
    ball_center_z = pad_top_z + ball_diameter / 2.0 - ZF_OFFSET * 2
    # 實際上，在回流焊中錫球會部分熔化與焊盤融合
    # 這裡將球體放在焊盤正上方，底部略為嵌入焊盤以模擬焊接效果
    ball_contact_z = pad_top_z + ZF_OFFSET  # 球底部在焊盤上方 ZF_OFFSET

    # 晶片本體底面 = 球心 + 球半徑（晶片坐在球的頂部）
    body_bottom_z = ball_center_z + ball_diameter / 2.0
    body_top_z = body_bottom_z + body_height
    body_center_z = (body_bottom_z + body_top_z) / 2.0

    # --- 1.1 建立 BGA 樹脂本體 ---
    # 先建立無倒角的立方體
    body = _make_cube(
        name=f"{name}_Body",
        x=x,
        y=y,
        z=body_center_z,
        sx=body_size,
        sy=body_size,
        sz=body_height,
        material=resin_mat
    )

    # 重新計算本體 Z 範圍（因為 make_cube 已應用縮放）
    body_half_h = body_height / 2.0

    # --- 1.2 頂部邊緣倒角（0.2mm） ---
    # 使用 Bevel modifier，只倒角頂面四邊和四個垂直邊的上半部
    # 為了精確控制，我們在編輯模式中選擇頂面頂點後手動倒角
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    # 切換到頂點選擇模式
    bpy.ops.mesh.select_mode(type='VERT')
    bpy.ops.mesh.select_all(action='DESELECT')

    # 獲取 mesh 數據
    mesh = body.data
    bm_verts = []

    # 找出所有 Z > body_center_z + body_half_h - 0.01 的頂點（即頂面頂點）
    # 在編輯模式中，我們遍歷頂點
    bpy.ops.object.mode_set(mode='OBJECT')

    top_vert_indices = []
    for vi, v in enumerate(mesh.vertices):
        if v.co.z > body_center_z + body_half_h - 0.05:
            top_vert_indices.append(vi)

    # 回到編輯模式選中頂面頂點
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')

    for vi in top_vert_indices:
        mesh.vertices[vi].select = True

    bpy.ops.object.mode_set(mode='EDIT')

    # 執行倒角（Ctrl+B 的等效 API）
    # 注意：bpy.ops.mesh.bevel 需要在編輯模式
    try:
        bpy.ops.mesh.bevel(
            offset=chamfer,
            offset_type='OFFSET',   # 絕對距離
            segments=3,             # 3段倒角 = 平滑圓角
            profile=0.5,            # 圓弧輪廓
            affect='VERTICES',      # 對選中的頂點做倒角（等效於頂面邊緣倒角）
        )
        print(f"  [BGA] 頂部邊緣倒角: {chamfer}mm")
    except Exception as e:
        print(f"  [BGA] ⚠ 倒角失敗（可能頂點選擇問題）: {e}")
        print(f"  [BGA] 使用 Bevel Modifier 替代方案...")
        bpy.ops.object.mode_set(mode='OBJECT')
        _apply_chamfer_to_top_edges(body, chamfer)

    bpy.ops.object.mode_set(mode='OBJECT')

    # --- 1.3 建立錫球陣列（12×12） ---
    # 計算陣列的起點（左上角）
    # 陣列總跨度 = (grid_size - 1) * ball_pitch
    grid_span = (grid_size - 1) * ball_pitch
    start_x = x - grid_span / 2.0  # 陣列左邊界
    start_y = y - grid_span / 2.0  # 陣列下邊界

    print(f"  [BGA] 錫球陣列: {grid_size}×{grid_size}, 間距 {ball_pitch}mm")
    print(f"  [BGA] 陣列範圍: [{start_x:.1f}, {start_x + grid_span:.1f}] × [{start_y:.1f}, {start_y + grid_span:.1f}]")

    ball_objects = []
    pad_objects = []

    for row in range(grid_size):
        for col in range(grid_size):
            # 計算此球的 XY 座標
            bx = start_x + col * ball_pitch
            by = start_y + row * ball_pitch

            idx = row * grid_size + col + 1  # 1-based 索引

            # --- 焊盤（薄圓盤，在 PCB 表面上） ---
            pad_name = f"{name}_Pad_R{row+1}C{col+1}"
            # 焊盤高度 = CU_THICKNESS（與 PCB 銅箔同厚）
            pad = _cylinder(
                name=pad_name,
                x=bx, y=by,
                z_bottom=BOARD_TOP_Z + ZF_OFFSET,  # 在板面上方 ZF_OFFSET
                z_top=BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET,
                radius=pad_diameter / 2.0,
                vertices=32,
                material=copper_mat
            )
            pad_objects.append(pad)

            # --- 錫球（完整球體） ---
            ball_name = f"{name}_Ball_R{row+1}C{col+1}"
            ball = _sphere(
                name=ball_name,
                x=bx, y=by,
                z=ball_center_z,
                radius=ball_diameter / 2.0,
                material=solder_mat
            )
            ball_objects.append(ball)

    total_balls = grid_size * grid_size
    print(f"  [BGA] 錫球: {total_balls} 顆 × Ø{ball_diameter}mm")
    print(f"  [BGA] 焊盤: {total_balls} 個 × Ø{pad_diameter}mm")

    # --- 1.4 建立 BGA 第一引腳標記（Pin 1 指示器） ---
    # 在封裝頂面一角放置一個小三角形凹槽（或絲印標記）
    # 凹槽 = 邊長 0.8mm 的三角形，深度 0.1mm
    pin1_corner_x = x - body_size / 2.0 + 1.5  # 靠近左下角
    pin1_corner_y = y - body_size / 2.0 + 1.5

    bpy.ops.mesh.primitive_cylinder_add(
        vertices=3,  # 三角形
        radius=0.5,
        depth=0.12,
        location=(pin1_corner_x, pin1_corner_y, body_top_z - 0.06)
    )
    pin1_mark = bpy.context.active_object
    pin1_mark.name = f"{name}_Pin1Marker"
    pin1_mark.rotation_euler = (0, 0, math.radians(30))

    # 布爾差集：在樹脂本體上切出 Pin 1 標記槽
    _boolean_difference(body, pin1_mark, apply=True)

    print(f"[BGA] ✅ {name} 建立完成")
    print(f"  - 本體: {body_size}×{body_size}×{body_height}mm 黑色樹脂")
    print(f"  - 錫球: {grid_size}×{grid_size} 陣列, Ø{ball_diameter}mm, 間距 {ball_pitch}mm")
    print(f"  - Standoff: {standoff}mm")

    return {
        'body': body,
        'balls': ball_objects,
        'pads': pad_objects,
        'center': (x, y),
        'top_z': body_top_z,
        'bottom_z': body_bottom_z,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 核心函數 2：射頻屏蔽罩 + 板載天線
# ══════════════════════════════════════════════════════════════════════════════

def create_rf_module(x, y, name="RF_Module"):
    """
    建立一個完整的射頻模塊，包含屏蔽罩和板載 PCB 天線。

    組成：
        1. 金屬屏蔽罩（Shielding Can）：20×15×3mm，壁厚 0.2mm
           - 頂部 4 個 Ø1.5mm 散熱通風孔
           - 材質：亮銀色拉絲金屬
        2. 板載倒 F 天線（IFA — Inverted-F Antenna）：
           - 2.4GHz ISM 頻段標準幾何
           - 位於屏蔽罩旁的 PCB 邊緣
           - 銅箔材質，線寬 1.0mm

    參數：
        x, y: 屏蔽罩中心在 PCB 上的 XY 座標
        name: 物件名稱前綴

    IFA 天線設計（2.4GHz 倒 F 型）：
        倒 F 天線由以下部分組成：
        - 主輻射臂（Main Arm）：長度約 λ/4 ≈ 31mm @ 2.45GHz
        - 短路短截線（Shorting Stub）：連接輻射臂到地
        - 饋電點（Feed Point）：50Ω 微帶線連接點

        佈局（從屏蔽罩右側延伸到 PCB 邊緣）：
          ┌──────────────┐
          │   Shielding  │  ← 屏蔽罩 (20×15mm)
          │     Can      │
          └──────┬───────┘
                 │ Feed (饋電點)
            ┌────┴────┐
            │  ═══════│═══  ← 主輻射臂 (平行於 PCB 邊緣)
            │  ║      │       λ/4 ≈ 31mm 長, 線寬 1.0mm
            │  ║ Short│       Shorting Stub (接地短截線)
            │  ║      │
            └─────────┘
             ↑ PCB Edge (板邊)
    """
    print(f"\n[RF] 建立射頻模塊 @ ({x:.1f}, {y:.1f})")

    # --- 材料 ---
    shield_mat = create_shield_can_material()
    antenna_mat = create_antenna_copper_material()
    # 嘗試重用步驟 1 的銅材質用於饋點
    copper_mat = _get_or_create_material(
        "PCB_Copper", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18
    )

    # --- 屏蔽罩幾何參數 ---
    can_size_x = 20.0      # 長（X 方向）
    can_size_y = 15.0      # 寬（Y 方向）
    can_height = 3.0       # 高（Z 方向）
    can_wall_thickness = 0.2  # 壁厚

    vent_hole_diameter = 1.5    # 散熱孔直徑
    vent_hole_radius = vent_hole_diameter / 2.0
    num_vents = 4               # 4 個散熱孔（2×2 排列在頂面）

    # --- 2.1 建立屏蔽罩主體（空心盒體） ---
    # 方法：建立外部實心塊 + 內部挖空塊（布爾差集）
    can_bottom_z = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET  # 在銅箔上方
    can_top_z = can_bottom_z + can_height
    can_center_z = (can_bottom_z + can_top_z) / 2.0

    # 外部實心塊
    outer_block = _make_cube(
        name=f"{name}_Shield_Outer",
        x=x, y=y,
        z=can_center_z,
        sx=can_size_x,
        sy=can_size_y,
        sz=can_height,
        material=shield_mat
    )

    # 內部挖空塊（比外部小 2×壁厚）
    inner_size_x = can_size_x - 2 * can_wall_thickness
    inner_size_y = can_size_y - 2 * can_wall_thickness
    inner_height = can_height - can_wall_thickness  # 頂部壁厚，底部開放

    inner_block = _make_cube(
        name=f"{name}_Shield_InnerCutter",
        x=x, y=y,
        # 內部立方體的中心：從底部上方 wall_thickness/2 開始（因為底部開口）
        z=can_center_z + can_wall_thickness / 2.0,
        sx=inner_size_x,
        sy=inner_size_y,
        sz=inner_height,
        material=None
    )

    # 布爾差集：outer - inner = 空心盒（底部開放）
    _boolean_difference(outer_block, inner_block, apply=True)

    shield_body = outer_block
    shield_body.name = f"{name}_ShieldCan"
    print(f"  [RF] 屏蔽罩: {can_size_x}×{can_size_y}×{can_height}mm, 壁厚 {can_wall_thickness}mm")

    # --- 2.2 頂部散熱通風孔（4 個 Ø1.5mm） ---
    # 通風孔排列：2×2，均勻分佈在頂面
    vent_spacing_x = can_size_x * 0.25   # 孔距中心的偏移量
    vent_spacing_y = can_size_y * 0.25

    vent_positions = [
        (x - vent_spacing_x, y - vent_spacing_y),  # 左下
        (x + vent_spacing_x, y - vent_spacing_y),  # 右下
        (x + vent_spacing_x, y + vent_spacing_y),  # 右上
        (x - vent_spacing_x, y + vent_spacing_y),  # 左上
    ]

    vent_cutters = []
    for vi, (vx, vy) in enumerate(vent_positions):
        vent = _cylinder(
            name=f"{name}_VentCutter_{vi+1}",
            x=vx, y=vy,
            z_bottom=can_top_z - can_wall_thickness - 0.1,  # 從頂面稍下方開始
            z_top=can_top_z + 0.1,                           # 穿透頂面
            radius=vent_hole_radius,
            vertices=48,  # 高分段數 → 更圓的孔
            material=None
        )
        vent_cutters.append(vent)

        # 逐個做布爾差集
        _boolean_difference(shield_body, vent, apply=True)

    print(f"  [RF] 散熱通風孔: {num_vents} 個 Ø{vent_hole_diameter}mm")

    # --- 2.3 屏蔽罩底部安裝腳 ---
    # 屏蔽罩邊緣有向下延伸的安裝腳，與 PCB 表面接觸
    # 在四角建立小方塊（用於 SMT 焊接），比壁厚稍寬
    foot_size = 1.0  # 安裝腳尺寸（正方形）

    foot_positions = [
        (x - can_size_x/2 + foot_size/2, y - can_size_y/2 + foot_size/2),  # 左下
        (x + can_size_x/2 - foot_size/2, y - can_size_y/2 + foot_size/2),  # 右下
        (x + can_size_x/2 - foot_size/2, y + can_size_y/2 - foot_size/2),  # 右上
        (x - can_size_x/2 + foot_size/2, y + can_size_y/2 - foot_size/2),  # 左上
    ]

    for fi, (fx, fy) in enumerate(foot_positions):
        foot = _make_cube(
            name=f"{name}_Foot_{fi+1}",
            x=fx, y=fy,
            z=can_bottom_z,  # 腳的頂面 = 罩的底面
            sx=foot_size,
            sy=foot_size,
            sz=CU_THICKNESS + ZF_OFFSET,  # 腳的高度 = 銅箔厚度
            material=copper_mat
        )
        # 微調：將腳的 Z 位置放在正確的位置
        foot.location.z = BOARD_TOP_Z + CU_THICKNESS / 2.0 + ZF_OFFSET

    print(f"  [RF] 安裝腳: 4 個 × {foot_size}mm")

    # --- 2.4 板載倒 F 天線（IFA） ---
    # 天線放在屏蔽罩右側，延伸到 PCB 邊緣
    # PCB 邊緣在 X = ±PCB_L/2

    antenna_x_start = x + can_size_x / 2.0 + 3.0  # 距屏蔽罩 3mm
    antenna_y_center = y                            # 與屏蔽罩中心對齊
    pcb_edge_x = PCB_L / 2.0 - 2.0                  # 距 PCB 邊緣 2mm

    print(f"  [RF] IFA 天線: 起點 X={antenna_x_start:.1f}, 延伸至板邊 X={pcb_edge_x:.1f}")

    # IFA 幾何參數（針對 2.45GHz）
    # 自由空間波長 λ = c/f = 3e8/2.45e9 ≈ 122.4mm
    # λ/4 ≈ 30.6mm（考慮 FR-4 基板的縮短效應，實際物理長度約 28-30mm）
    # FR-4 有效介電常數 ε_eff ≈ 3.8 (微帶線)
    # 導波波長 λ_g = λ_0 / sqrt(ε_eff) ≈ 122.4 / 1.95 ≈ 62.8mm
    # λ_g/4 ≈ 15.7mm（這是實際需要的物理長度）

    arm_length = 16.0      # 主輻射臂物理長度 mm（FR-4 上 λ_g/4）
    arm_width = 1.0        # 微帶線寬度 mm（50Ω 阻抗匹配用）
    arm_thickness = CU_THICKNESS  # 銅箔厚度
    short_stub_width = 0.5  # 短路短截線寬度

    # 饋電點間隙（Feed Gap）：主臂與饋線之間的小間隙
    feed_gap = 0.3

    # IFA 天線拓撲（從饋點開始，沿 X 軸正向往板邊）：
    #
    #   饋點 (Feed Point)
    #     │
    #     ├── 饋線 (垂直向上 Y+) ──┐
    #     │                   饋線臂 (轉 X+ 方向，平行於板邊)
    #     │                         │
    #     ├── 短截線 (垂直向下 Y-) ─┤ ← 短路接地點 (via 到地層)
    #     │                         │
    #     │                    輻射開路端 →
    #
    # 簡化的蛇形微帶天線（Meander Line 作為 IFA 變體）：
    # 為了節省空間，使用蛇形折疊結構來實現所需的電氣長度

    # 將天線定義為一系列線段頂點
    # 使用 2D 折線定義，然後擠出為帶寬度的銅箔

    def build_antenna_trace(points_2d, trace_width, z_bottom, z_top, obj_name, material):
        """
        從 2D 頂點列表建立一個帶寬度和厚度的銅箔走線。

        使用與步驟 1 相同的 centerline_to_mesh 邏輯。
        """
        half_w = trace_width / 2.0

        top_verts = []
        bottom_verts = []

        for i, (px, py) in enumerate(points_2d):
            # 計算切線方向
            if i == 0:
                dx = points_2d[1][0] - px
                dy = points_2d[1][1] - py
            elif i == len(points_2d) - 1:
                dx = px - points_2d[-2][0]
                dy = py - points_2d[-2][1]
            else:
                dx = points_2d[i + 1][0] - points_2d[i - 1][0]
                dy = points_2d[i + 1][1] - points_2d[i - 1][1]

            length = math.hypot(dx, dy)
            if length < 1e-9:
                length = 1.0

            nx = -dy / length
            ny = dx / length

            left_x = px - nx * half_w
            left_y = py - ny * half_w
            right_x = px + nx * half_w
            right_y = py + ny * half_w

            top_verts.append(((left_x, left_y, z_top), (right_x, right_y, z_top)))
            bottom_verts.append(((left_x, left_y, z_bottom), (right_x, right_y, z_bottom)))

        mesh = bpy.data.meshes.new(name=obj_name)
        obj = bpy.data.objects.new(name=obj_name, object_data=mesh)
        bpy.context.collection.objects.link(obj)

        verts = []
        faces = []

        for i in range(len(top_verts)):
            tl = top_verts[i][0]
            tr = top_verts[i][1]
            bl = bottom_verts[i][0]
            br = bottom_verts[i][1]

            idx_tl = len(verts); verts.append(tl)
            idx_tr = len(verts); verts.append(tr)
            idx_bl = len(verts); verts.append(bl)
            idx_br = len(verts); verts.append(br)

            if i < len(top_verts) - 1:
                nb = len(verts)  # next base
                ntl, ntr, nbl, nbr = nb, nb+1, nb+2, nb+3

                faces.append((idx_tl, idx_tr, ntr, ntl))  # 頂面
                faces.append((idx_bl, idx_br, nbr, nbl))  # 底面
                faces.append((idx_tr, idx_br, nbr, ntr))  # 右面
                faces.append((idx_tl, idx_bl, nbl, ntl))  # 左面

        mesh.from_pydata(verts, [], faces)
        mesh.update()
        obj.data.materials.append(material)
        return obj

    # 定義蛇形微帶天線的中心線路徑
    #
    # 拓撲說明：
    #   饋點在屏蔽罩右邊緣，天線沿 X+ 方向朝板邊延伸。
    #   由於可用空間有限（約 15-20mm），使用蛇形折疊
    #   來在有限空間內實現 λ_g/4 ≈ 16mm 的電氣長度。
    #
    #   路徑（從饋點開始）：
    #     1. 從饋點垂直向上(Y+)走 4mm
    #     2. 轉 90° 朝 X+ 走 10mm（主輻射臂第一段）
    #     3. 轉 90° 朝 Y- 走 1.5mm（折返）
    #     4. 轉 90° 朝 X- 走 8mm（返回段）
    #     5. 轉 90° 朝 Y- 走 1.5mm
    #     6. 轉 90° 朝 X+ 走 10mm（主輻射臂第二段，開路端）
    #   總物理長度 ≈ 4 + 10 + 1.5 + 8 + 1.5 + 10 = 35mm
    #   這超過了 λ_g/4，但蛇形天線的有效電氣長度主要由
    #   最長的平行段決定，折返段提供額外的電感匹配。

    # 簡化為標準倒 F 型：
    #   - 主水平臂（平行於 PCB 邊緣，即 Y 方向）
    #   - 饋電垂直臂（X 方向，從屏蔽罩出來）
    #   - 短路垂直臂（X 方向，接地）

    # 重新設計：標準 IFA 拓撲
    #
    #     屏蔽罩右邊緣
    #     │
    #     │  ← 饋電點 (Feed point) ─── 水平主臂 (沿 Y+ 方向延伸) ─── 開路端
    #     │       │                                            ↑
    #     │       │ (饋電垂直臂, X+ 方向)                       │
    #     │       │                                            │
    #     │       ├── 短路臂 (X+ 方向, Y- 偏移) ─── GND via ──┘
    #     │
    #  PCB 邊緣 →

    # 饋點位置（在屏蔽罩右邊緣）
    feed_x = x + can_size_x / 2.0 + ZF_OFFSET
    feed_y = y  # 與屏蔽罩中心對齊

    # 主水平臂（沿 Y 軸，平行於 PCB 右邊緣）
    arm_y_length = arm_length  # 16mm（沿 Y 方向的長度）

    # 定義天線路徑頂點（2D 俯視圖）
    # 單位：mm，原點在饋點
    antenna_path = [
        # 饋點 → 饋電短臂（X+ 方向，遠離屏蔽罩）
        (feed_x, feed_y),
        (feed_x + 2.0, feed_y),                        # 饋電短臂 2mm

        # 轉角 → 主水平臂向上（Y+ 方向）
        (feed_x + 2.0, feed_y + arm_y_length),          # 主臂終端（開路）

        # （此處省略短路臂的建模，以簡化天線結構）
        # 在完整設計中，短路臂會從饋點向下(Y-)走一小段，透過 via 接地
    ]

    # 短路臂（從饋點向下，模擬接地）
    short_path = [
        (feed_x, feed_y),
        (feed_x + 1.5, feed_y - 2.5),                   # 短路接地點
    ]

    antenna_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    antenna_z_top = antenna_z_bottom + arm_thickness

    # 建立主輻射臂
    antenna_main = build_antenna_trace(
        points_2d=antenna_path,
        trace_width=arm_width,
        z_bottom=antenna_z_bottom,
        z_top=antenna_z_top,
        obj_name=f"{name}_IFA_MainArm",
        material=antenna_mat
    )
    print(f"  [RF] IFA 主臂: 長 ~{arm_y_length}mm, 線寬 {arm_width}mm")

    # 建立短路臂
    antenna_short = build_antenna_trace(
        points_2d=short_path,
        trace_width=short_stub_width,
        z_bottom=antenna_z_bottom,
        z_top=antenna_z_top,
        obj_name=f"{name}_IFA_ShortStub",
        material=antenna_mat
    )
    print(f"  [RF] IFA 短路臂: 線寬 {short_stub_width}mm")

    # --- 2.5 天線饋電點焊盤 ---
    # 一個小的矩形焊盤，連接饋線到屏蔽罩出口
    feed_pad = _make_cube(
        name=f"{name}_FeedPad",
        x=feed_x + 1.0,
        y=feed_y,
        z=antenna_z_bottom + arm_thickness / 2.0,
        sx=1.5,  # 饋電點焊盤寬度
        sy=arm_width,
        sz=arm_thickness,
        material=copper_mat
    )

    # --- 2.6 天線接地過孔（在短路臂末端） ---
    short_end_x = short_path[-1][0]
    short_end_y = short_path[-1][1]

    via_gnd = _cylinder(
        name=f"{name}_GND_Via",
        x=short_end_x,
        y=short_end_y,
        z_bottom=-BOARD_TOP_Z,
        z_top=BOARD_TOP_Z + CU_THICKNESS,
        radius=0.3,  # 接地過孔半徑
        vertices=32,
        material=copper_mat
    )
    print(f"  [RF] 接地過孔 @ ({short_end_x:.1f}, {short_end_y:.1f})")

    # --- 2.7 天線區域的阻焊層開窗 ---
    # 天線輻射區域需要裸露銅箔（無阻焊層覆蓋）
    # 建立一個矩形區域標記開窗
    antenna_clearance_x = feed_x + 1.0
    antenna_clearance_y = feed_y + arm_y_length / 2.0
    clearance_size_x = 6.0   # 開窗區域寬度
    clearance_size_y = arm_y_length + 4.0  # 開窗區域長度

    clearance_marker = _make_cube(
        name=f"{name}_AntennaClearance",
        x=antenna_clearance_x,
        y=antenna_clearance_y,
        z=BOARD_TOP_Z + CU_THICKNESS + SOLDERMASK_THICKNESS / 2.0 + ZF_OFFSET,
        sx=clearance_size_x,
        sy=clearance_size_y,
        sz=SOLDERMASK_THICKNESS,
        material=None
    )
    clearance_marker.hide_viewport = True
    clearance_marker.hide_render = True  # 標記物件，不參與渲染

    print(f"  [RF] 阻焊開窗: {clearance_size_x}×{clearance_size_y}mm")

    print(f"[RF] ✅ {name} 建立完成")
    print(f"  - 屏蔽罩: {can_size_x}×{can_size_y}×{can_height}mm, 壁厚 {can_wall_thickness}mm")
    print(f"  - 散熱孔: {num_vents} 個 Ø{vent_hole_diameter}mm")
    print(f"  - IFA 天線: 2.4GHz λ/4, 主臂 {arm_y_length}mm")

    return {
        'shield_body': shield_body,
        'antenna_main': antenna_main,
        'antenna_short': antenna_short,
        'feed_pad': feed_pad,
        'ground_via': via_gnd,
        'center': (x, y),
        'top_z': can_top_z,
        'bottom_z': can_bottom_z,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 場景整合函數（可選：在步驟 1 後調用）
# ══════════════════════════════════════════════════════════════════════════════

def place_components():
    """
    將 BGA 晶片和 RF 模塊放置在 PCB 上的合理位置。

    位置佈局（俯視圖）：
        PCB: 140×90mm

        ┌──────────────────────────────────────────────┐
        │  ◎                                  ◎       │ ← 安裝孔
        │                                              │
        │   ┌──────────┐    ┌──────────────────┐      │
        │   │  BGA     │    │   RF Shield      │══╡  │ ← IFA 天線
        │   │  15×15   │    │   20×15          │   │  │
        │   └──────────┘    └──────────────────┘   │  │
        │                                           │  │
        │                                           │  │
        │  ◎                                  ◎    │  │
        └──────────────────────────────────────────────┘
    """
    print("\n[LAYOUT] 開始放置元件...")

    # BGA 晶片放在 PCB 中央偏左
    bga_x = -30.0
    bga_y = 0.0
    bga = create_bga_chip(bga_x, bga_y, name="U1_MCU_BGA144")

    # RF 模塊放在 PCB 右側（靠近邊緣以利天線輻射）
    rf_x = 35.0
    rf_y = 0.0
    rf = create_rf_module(rf_x, rf_y, name="U2_RF_Transceiver")

    print("\n[LAYOUT] ✅ 元件放置完成")
    print(f"  U1 BGA: ({bga_x}, {bga_y})")
    print(f"  U2 RF:  ({rf_x}, {rf_y})")
    print(f"  總物件數: {len(bpy.data.objects)}")

    return {'bga': bga, 'rf': rf}


# ══════════════════════════════════════════════════════════════════════════════
# 獨立執行入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ATOS PRO — PCB 超逼真建模工具 v2.0")
    print("  步驟 2：BGA 晶片 + 射頻屏蔽罩 + 板載天線")
    print("=" * 60)
    print()
    print("⚠ 注意：請先執行步驟 1 建立 PCB 基板。")
    print("  如果 PCB 基板已存在，將在此基礎上添加元件。")
    print()

    # 檢查 PCB 基板是否存在
    pcb_exists = "PCB_Base_Body" in bpy.data.objects
    if not pcb_exists:
        print("[WARN] 未檢測到 PCB_Base_Body，將獨立建立元件（無基板）。")
        print("[WARN] 元件可能會懸浮在空中。建議先執行 step1。")
    else:
        print("[INFO] PCB 基板已存在，元件將放置在正確高度。")

    # 放置元件
    result = place_components()

    # 輸出統計
    total_objects = len(bpy.data.objects)
    total_vertices = sum(
        len(obj.data.vertices)
        for obj in bpy.data.objects
        if obj.type == 'MESH'
    )
    print("\n" + "=" * 60)
    print(f"  ✅ 步驟 2 完成！")
    print(f"  場景總物件數: {total_objects}")
    print(f"  場景總頂點數: {total_vertices:,}")
    print(f"  BGA 錫球: {len(result['bga']['balls'])} 顆")
    print(f"  RF 天線: IFA 2.4GHz λ/4")
    print("=" * 60)
    print()
    print("📌 步驟 2 完成。等待下一步指令...")
