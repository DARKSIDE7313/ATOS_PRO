"""
ATOS_PRO/tools/pcb_ultra_step2.py
Blender 4.2+ / Python 3.11 — PCB 超精密建模
第二部分：BGA 晶片（微觀錫球 + 焊錫爬升）+ 射頻屏蔽罩 + IFA 天線

全部使用 bmesh / mesh.from_pydata 低級 API。
與第一部分 pcb_ultra_step1.py 的全域參數和材質完全相容。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import bmesh
import math

# ══════════════════════════════════════════════════════════════════════════════
# 從第一部分繼承的全域參數
# ══════════════════════════════════════════════════════════════════════════════

PCB_L = 140.0
PCB_W = 90.0
PCB_H = 1.6
CU_THICKNESS = 0.035
SOLDERMASK_THICKNESS = 0.02
SILKSCREEN_THICKNESS = 0.01
ZF_OFFSET = 0.002
BOARD_TOP_Z = PCB_H / 2.0
BOARD_BOTTOM_Z = -PCB_H / 2.0

# 通用圓周分段數（高精度）
SEGMENTS_HI = 48   # 小元件（錫球、過孔）
SEGMENTS_MID = 32  # 中等元件


# ══════════════════════════════════════════════════════════════════════════════
# 輔助函數：bmesh 幾何建立
# ══════════════════════════════════════════════════════════════════════════════

def bmesh_to_object(bm, name, material=None):
    """將 bmesh 寫入 Mesh 物件並連結到場景。返回 obj。"""
    mesh = bpy.data.meshes.new(name=name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name=name, object_data=mesh)
    bpy.context.collection.objects.link(obj)
    if material:
        obj.data.materials.append(material)
    return obj


def _solid_cylinder_bm(radius, height, segments, z_bottom=0.0):
    """bmesh 實心圓柱（用於布爾切割）。底部在 z_bottom。"""
    z_top = z_bottom + height
    bm = bmesh.new()
    bc = bm.verts.new((0, 0, z_bottom))
    tc = bm.verts.new((0, 0, z_top))
    b_ring, t_ring = [], []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        x, y = radius * math.cos(a), radius * math.sin(a)
        b_ring.append(bm.verts.new((x, y, z_bottom)))
        t_ring.append(bm.verts.new((x, y, z_top)))
    bm.verts.ensure_lookup_table()
    for i in range(segments):
        j = (i + 1) % segments
        bm.faces.new([b_ring[i], b_ring[j], t_ring[j], t_ring[i]])  # 側面
        bm.faces.new([bc, b_ring[j], b_ring[i]])                     # 底面
        bm.faces.new([tc, t_ring[i], t_ring[j]])                     # 頂面
    return bm


def _hollow_cylinder_bm(outer_r, inner_r, height, segments):
    """bmesh 中空雙壁圓柱體（外壁+內壁+頂環+底環）。中心在原點。"""
    z_bot = -height / 2.0
    z_top = height / 2.0
    bm = bmesh.new()
    ob, ot, ib, it = [], [], [], []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        ca, sa = math.cos(a), math.sin(a)
        ob.append(bm.verts.new((outer_r * ca, outer_r * sa, z_bot)))
        ot.append(bm.verts.new((outer_r * ca, outer_r * sa, z_top)))
        ib.append(bm.verts.new((inner_r * ca, inner_r * sa, z_bot)))
        it.append(bm.verts.new((inner_r * ca, inner_r * sa, z_top)))
    bm.verts.ensure_lookup_table()
    for i in range(segments):
        j = (i + 1) % segments
        bm.faces.new([ob[i], ob[j], ot[j], ot[i]])  # 外壁
        bm.faces.new([ib[j], ib[i], it[i], it[j]])  # 內壁
        bm.faces.new([ot[i], ot[j], it[j], it[i]])  # 頂環
        bm.faces.new([ob[j], ob[i], ib[i], ib[j]])  # 底環
    return bm


def _apply_boolean_diff(target_obj, cutter_obj):
    """target - cutter 布爾差集，應用後隱藏 cutter。"""
    bpy.context.view_layer.objects.active = target_obj
    target_obj.select_set(True)
    mod = target_obj.modifiers.new(name=f"Bool_{cutter_obj.name}", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter_obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    cutter_obj.hide_viewport = True
    cutter_obj.hide_render = True


# ══════════════════════════════════════════════════════════════════════════════
# 材質工廠
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_create_mat(name, color, metallic, roughness, ior=1.5, anisotropic=0.0):
    """重用已有材質或建立新材質。"""
    if name in bpy.data.materials:
        return bpy.data.materials[name]
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
    bsdf.inputs['Anisotropic'].default_value = anisotropic
    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


def create_bga_epoxy_material():
    """
    BGA 封裝環氧樹脂：霧面黑色 + 細微噪波顆粒感。

    Roughness=0.65, Metallic=0.0
    Noise Texture 調製 Roughness 和輕微顏色變化，
    模擬 IC 封裝 Mold Compound 的表面微觀粗糙度。
    """
    mat = bpy.data.materials.new(name="PCB_BGA_Epoxy")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # 紋理座標
    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-600, 0)

    # 高頻噪波（模擬塑料顆粒）
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-350, 100)
    noise.inputs['Scale'].default_value = 350.0
    noise.inputs['Detail'].default_value = 10.0
    noise.inputs['Roughness'].default_value = 0.6
    links.new(tex_coord.outputs['UV'], noise.inputs['Vector'])

    # Principled BSDF
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.035, 0.035, 0.04, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.65
    bsdf.inputs['IOR'].default_value = 1.55
    bsdf.inputs['Specular IOR Level'].default_value = 0.12

    # 噪波 → Roughness 調製
    roughness_range = nodes.new(type='ShaderNodeMapRange')
    roughness_range.location = (0, 100)
    roughness_range.inputs['From Min'].default_value = 0.0
    roughness_range.inputs['From Max'].default_value = 1.0
    roughness_range.inputs['To Min'].default_value = -0.08
    roughness_range.inputs['To Max'].default_value = 0.08
    links.new(noise.outputs['Fac'], roughness_range.inputs['Value'])

    roughness_add = nodes.new(type='ShaderNodeMath')
    roughness_add.location = (120, 80)
    roughness_add.operation = 'ADD'
    roughness_base = nodes.new(type='ShaderNodeValue')
    roughness_base.location = (0, 50)
    roughness_base.outputs[0].default_value = 0.65
    links.new(roughness_base.outputs['Value'], roughness_add.inputs[0])
    links.new(roughness_range.outputs['Result'], roughness_add.inputs[1])
    links.new(roughness_add.outputs['Value'], bsdf.inputs['Roughness'])

    # 噪波 → 輕微凹凸
    bump = nodes.new(type='ShaderNodeBump')
    bump.location = (0, -150)
    bump.inputs['Strength'].default_value = 0.04
    bump.inputs['Distance'].default_value = 0.002
    links.new(noise.outputs['Fac'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    print("  [MAT] BGA 環氧樹脂: 霧面黑 + 顆粒 Noise")
    return mat


def create_solder_material():
    """
    焊錫材質：SAC305 亮銀錫色 #B5B8B1。
    Metallic=1.0, Roughness=0.12。
    """
    # #B5B8B1 → RGB (181/255, 184/255, 177/255) ≈ (0.710, 0.722, 0.694)
    mat = bpy.data.materials.new(name="PCB_Solder_SAC305")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.710, 0.722, 0.694, 1.0)  # #B5B8B1
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.12
    bsdf.inputs['IOR'].default_value = 1.9
    bsdf.inputs['Anisotropic'].default_value = 0.05

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    print("  [MAT] SAC305 焊錫: #B5B8B1, Metallic=1.0, Roughness=0.12")
    return mat


def create_shield_material():
    """
    屏蔽罩材質：亮銀色洋白銅，各向異性拉絲紋理。
    Metallic=1.0, Roughness=0.25, Anisotropic=0.35
    """
    mat = bpy.data.materials.new(name="PCB_Shield_NickelSilver")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # 拉絲紋理（沿 X 軸方向）
    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-400, 0)

    # 拉絲噪波（低頻單向拉伸）
    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-250, 0)
    mapping.inputs['Scale'].default_value = (0.5, 15.0, 15.0)  # X 方向拉伸
    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

    noise_brush = nodes.new(type='ShaderNodeTexNoise')
    noise_brush.location = (-50, 100)
    noise_brush.inputs['Scale'].default_value = 200.0
    noise_brush.inputs['Detail'].default_value = 5.0
    noise_brush.inputs['Roughness'].default_value = 0.5
    links.new(mapping.outputs['Vector'], noise_brush.inputs['Vector'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    # 洋白銅色（略帶暖銀色）
    bsdf.inputs['Base Color'].default_value = (0.83, 0.84, 0.82, 1.0)
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.25
    bsdf.inputs['IOR'].default_value = 1.45
    bsdf.inputs['Anisotropic'].default_value = 0.35
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.0

    # 拉絲噪波 → 輕微 Roughness 調製
    brush_range = nodes.new(type='ShaderNodeMapRange')
    brush_range.location = (100, 100)
    brush_range.inputs['From Min'].default_value = 0.0
    brush_range.inputs['From Max'].default_value = 1.0
    brush_range.inputs['To Min'].default_value = -0.06
    brush_range.inputs['To Max'].default_value = 0.06
    links.new(noise_brush.outputs['Fac'], brush_range.inputs['Value'])

    roughness_add = nodes.new(type='ShaderNodeMath')
    roughness_add.location = (180, 80)
    roughness_add.operation = 'ADD'
    roughness_val = nodes.new(type='ShaderNodeValue')
    roughness_val.location = (100, 40)
    roughness_val.outputs[0].default_value = 0.25
    links.new(roughness_val.outputs['Value'], roughness_add.inputs[0])
    links.new(brush_range.outputs['Result'], roughness_add.inputs[1])
    links.new(roughness_add.outputs['Value'], bsdf.inputs['Roughness'])

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    print("  [MAT] 屏蔽罩洋白銅: 各向異性拉絲, Anisotropic=0.35")
    return mat


def create_gold_trace_material():
    """ENIG 鍍金走線（與第一部分相容）。"""
    if "PCB_GoldTrace" in bpy.data.materials:
        return bpy.data.materials["PCB_GoldTrace"]
    return _get_or_create_mat("PCB_GoldTrace", (0.95, 0.78, 0.25), 1.0, 0.10, ior=0.47)


def create_copper_pad_material():
    """銅焊盤材質。"""
    if "PCB_Copper_PTH" in bpy.data.materials:
        return bpy.data.materials["PCB_Copper_PTH"]
    return _get_or_create_mat("PCB_CopperPad", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18)


# ══════════════════════════════════════════════════════════════════════════════
# Solder Fillet 幾何引擎
# ══════════════════════════════════════════════════════════════════════════════

def create_solder_fillet_bmesh(pad_radius, ball_radius, ball_center_z, fillet_height, segments):
    """
    使用 bmesh 建立一個內凹環形焊錫爬升 Fillet。

    幾何形狀（剖面圖）：
                ╭─── 錫球表面
               ╱
          ╭──╱  ← fillet 頂部（與球體相切）
          │ ╱
          │╱   ← 凹面（表面張力造成的內凹弧度）
         ╱│
        ╱ │
       ╱  │
      ╱   │
     ╱    │
    ╱─────╯  ← fillet 底部（與焊盤相交）
    ═══════   ← PCB 焊盤表面

    實現方法：
        沿 Z 軸堆疊多個同心圓環。每個環的半徑 r(z) 遵循凹面曲線：
        r(z) = r_pad + (r_contact - r_pad) * (z/h)² - concave * sin(π*z/h)

        其中 concave 控制內凹程度（正值 = 向內凹）。

    參數：
        pad_radius: 焊盤半徑（fillet 底部半徑）
        ball_radius: 錫球半徑
        ball_center_z: 錫球球心 Z 座標（相對於焊盤表面 = 0）
        fillet_height: fillet 從焊盤向上爬升的高度
        segments: 圓周分段

    返回: bmesh 物件
    """
    bm = bmesh.new()

    # fillet 在焊盤上方，焊盤表面在 Z=0
    z_min = 0.0
    z_max = fillet_height

    # 在 fillet 頂部 (z_max) 處，錫球的截面半徑
    # 球心在 (0, 0, ball_center_z)，球面方程: x² + y² + (z - ball_center_z)² = ball_radius²
    # 在 z = z_max 處的球截面半徑:
    dz = ball_center_z - z_max
    if dz >= ball_radius:
        # fillet 頂部超出球體範圍
        r_contact = 0.0
    else:
        r_contact = math.sqrt(max(0, ball_radius**2 - dz**2))

    # 內凹參數（控制 meniscus 的向內彎曲程度）
    # 較小的 ball_radius 產生更明顯的內凹
    concave = pad_radius * 0.15

    # 堆疊層數（Z 方向）
    z_layers = 8
    ring_verts = []  # [layer][ring_index]

    for layer in range(z_layers + 1):
        t = layer / z_layers  # 0 → 1
        z = z_min + t * z_max

        # 凹面半徑函數
        r = (pad_radius
             + (r_contact - pad_radius) * (t ** 2)
             - concave * math.sin(math.pi * t))

        # 確保半徑為正
        r = max(0.005, r)

        ring = []
        for i in range(segments):
            angle = 2.0 * math.pi * i / segments
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            ring.append(bm.verts.new((x, y, z)))
        ring_verts.append(ring)

    bm.verts.ensure_lookup_table()

    # 建立面（相鄰層之間的側面 + 頂環 + 底環）
    for layer in range(z_layers):
        curr = ring_verts[layer]
        nxt = ring_verts[layer + 1]
        for i in range(segments):
            j = (i + 1) % segments
            bm.faces.new([curr[i], curr[j], nxt[j], nxt[i]])

    # 底面環（與焊盤接觸）
    bot = ring_verts[0]
    for i in range(segments):
        j = (i + 1) % segments
        bm.faces.new([bot[j], bot[i], bm.verts.new((0, 0, z_min)),
                       bm.verts.new((0, 0, z_min))])
        # 修正：使用扇形三角形填補底面

    # 修正底面（重新建立扇形三角形）
    # 刪除剛才錯誤建立的面，改用正確方式
    # 為了簡化：底面不封閉（被焊盤遮擋），頂面接觸錫球也不封閉

    # 頂面環（接觸錫球底部，不封閉以融入錫球）
    # 兩端開放 = 側面形成一個完整的環形帶

    return bm


def create_solder_fillet_simple(pad_radius, ball_radius, ball_center_z,
                                 fillet_height, segments):
    """
    簡化版的焊錫 fillet：使用內凹環形。

    這個版本更可靠：直接用 from_pydata 建立頂點和面。
    """
    z_min = 0.0
    z_max = fillet_height

    # 接觸點半徑
    dz = ball_center_z - z_max
    if dz >= ball_radius:
        r_contact = 0.01
    else:
        r_contact = math.sqrt(max(0, ball_radius**2 - dz**2))

    concave = pad_radius * 0.12  # 內凹量
    z_layers = 10
    segments_local = segments

    verts = []
    faces = []

    for layer in range(z_layers + 1):
        t = layer / z_layers
        z = z_min + t * z_max
        # 凹面半徑：二次函數 + 正弦內凹
        r = pad_radius + (r_contact - pad_radius) * (t * t)
        r -= concave * math.sin(math.pi * t)
        r = max(0.008, r)

        for i in range(segments_local):
            angle = 2.0 * math.pi * i / segments_local
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            verts.append((x, y, z))

    # 建立側面
    for layer in range(z_layers):
        base_idx = layer * segments_local
        next_base = (layer + 1) * segments_local
        for i in range(segments_local):
            j = (i + 1) % segments_local
            # 逆時針（從外部看），法線朝外
            faces.append((base_idx + i, base_idx + j,
                          next_base + j, next_base + i))

    # 建立底面環（封閉，形成與焊盤的接觸面）
    # 使用扇形三角形
    center_bot = len(verts)
    verts.append((0, 0, z_min))
    bot_start = 0
    for i in range(segments_local):
        j = (i + 1) % segments_local
        faces.append((center_bot, bot_start + j, bot_start + i))

    # 建立頂面環（封閉）
    center_top = len(verts)
    verts.append((0, 0, z_max))
    top_start = z_layers * segments_local
    for i in range(segments_local):
        j = (i + 1) % segments_local
        faces.append((center_top, top_start + i, top_start + j))

    return verts, faces


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：create_ultra_bga_chip(x, y)
# ══════════════════════════════════════════════════════════════════════════════

def create_ultra_bga_chip(x, y, name="U_BGA"):
    """
    建立高精度 BGA-144 封裝晶片。

    結構（從上到下）：
        ┌──────────────────────────┐  ← 頂面 4 邊 0.2mm 45° 倒角
        │  黑色霧面環氧樹脂本體   │  15×15×1.2mm
        │  (Mold Compound)        │
        ├──────────────────────────┤  ← 本體底面
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │  12×12=144 顆錫球
        │  ... (12 rows)         │  Ø0.4mm, pitch=0.8mm
        │  ⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚⊚  │  Y 軸輕微壓扁（焊接變形）
        ├──────────┬──────────────┤
        │  ⊿⊿⊿⊿  │  焊錫 fillet │  每個錫球底部有凹面爬升
        │══════════╪══════════════│  ← PCB 頂面 + 銅焊盤
        └──────────┴──────────────┘

    參數：
        x, y: 晶片中心在 PCB 上的座標
        name: 物件名稱前綴
    """
    print(f"\n[ULTRA BGA] 建立 BGA 晶片 @ ({x:.1f}, {y:.1f})")

    # --- 材質 ---
    epoxy_mat = create_bga_epoxy_material()
    solder_mat = create_solder_material()
    copper_mat = create_copper_pad_material()

    # --- BGA 幾何參數 ---
    body_size = 15.0          # 本體長寬
    body_height = 1.2         # 本體厚度
    bevel_size = 0.2          # 頂部邊緣 45° 倒角
    ball_diameter = 0.4       # 錫球直徑
    ball_radius = ball_diameter / 2.0
    ball_pitch = 0.8          # 錫球間距
    grid_size = 12            # 12×12
    pad_diameter = 0.35       # 銅焊盤直徑
    pad_radius = pad_diameter / 2.0

    # Z 軸計算
    # 焊盤在 PCB 頂面上: z = BOARD_TOP_Z + CU_THICKNESS
    pad_z_top = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET

    # 錫球中心 Z: 球體直徑 0.4mm，球底部在焊盤上方
    # 實際上錫球會部分"沉入"焊盤（焊接融合），球心略高於焊盤
    # 球心 Z ≈ pad_z_top + ball_radius * 0.85（球體底部 15% 融入焊盤）
    ball_standoff = ball_radius * 0.30  # standoff = 球底部到焊盤的距離
    ball_center_z = pad_z_top + ball_standoff + ball_radius

    # 晶片本體底面 Z（坐在錫球上方）
    # 錫球被輕微壓扁，本體底面略低於球的頂部
    body_bottom_z = ball_center_z + ball_radius * 0.88
    body_top_z = body_bottom_z + body_height
    body_center_z = (body_bottom_z + body_top_z) / 2.0

    # --- 1.1 建立晶片本體（bmesh + 頂部邊緣倒角） ---
    # 使用 bmesh create_cube 然後縮放
    bm_body = bmesh.new()
    bmesh.ops.create_cube(bm_body, size=2.0)  # 2×2×2 的立方體

    # 縮放到 15×15×1.2
    scale_matrix = (
        body_size / 2.0, 0, 0, 0,
        0, body_size / 2.0, 0, 0,
        0, 0, body_height / 2.0, 0,
        0, 0, 0, 1,
    )
    # bmesh 縮放：直接修改每個頂點的座標
    bm_body.verts.ensure_lookup_table()
    for v in bm_body.verts:
        v.co = (
            v.co.x * (body_size / 2.0),
            v.co.y * (body_size / 2.0),
            v.co.z * (body_height / 2.0),
        )

    # --- 頂部 4 邊緣 45° 倒角 ---
    # 選取所有頂面水平邊緣（Z ≈ body_height/2 的邊）
    top_z = body_height / 2.0
    top_edges = []
    for edge in bm_body.edges:
        v1_z = edge.verts[0].co.z
        v2_z = edge.verts[1].co.z
        # 兩端點都在頂面附近的邊 = 頂面邊緣
        if v1_z > top_z - 0.01 and v2_z > top_z - 0.01:
            # 這包括了頂面的 4 條水平邊 + 4 條對角邊（如果有的話）
            # 對於 cube，頂面有 4 條水平邊
            # 檢查是否為水平邊（Z 座標相同且都在頂面）
            if abs(v1_z - v2_z) < 0.001:
                top_edges.append(edge)

    if len(top_edges) >= 4:
        # 使用 bmesh bevel 操作
        geom_input = top_edges
        bmesh.ops.bevel(
            bm_body,
            geom=geom_input,
            offset=bevel_size,
            offset_type='OFFSET',     # 絕對距離
            segments=3,               # 3 段 = 平滑過渡
            profile=0.5,              # 0.5 = 圓弧輪廓
            affect='EDGES',
        )
        print(f"  [BGA] 頂部倒角: {bevel_size}mm × 3 segments, {len(top_edges)} 條邊")
    else:
        print(f"  [BGA] ⚠ 未找到足夠的頂面邊緣（找到 {len(top_edges)} 條），跳過倒角")

    bm_body.verts.ensure_lookup_table()

    body_obj = bmesh_to_object(bm_body, f"{name}_Body", material=epoxy_mat)
    body_obj.location = (x, y, body_center_z)

    # --- 1.2 建立錫球陣列 + 焊盤 + fillet ---
    grid_span = (grid_size - 1) * ball_pitch
    start_x = x - grid_span / 2.0
    start_y = y - grid_span / 2.0

    print(f"  [BGA] 錫球陣列: {grid_size}×{grid_size}, pitch={ball_pitch}mm")
    print(f"  [BGA] 陣列範圍: [{start_x:.1f}, {start_x+grid_span:.1f}] × "
          f"[{start_y:.1f}, {start_y+grid_span:.1f}]")

    ball_objects = []
    pad_objects = []
    fillet_objects = []

    # Y 軸壓扁比例（焊接後球體受壓變形，Y 軸方向輕微變扁）
    squash_y = 0.88  # 球體高度變為原來的 88%

    for row in range(grid_size):
        for col in range(grid_size):
            bx = start_x + col * ball_pitch
            by = start_y + row * ball_pitch
            idx = row * grid_size + col + 1

            # --- 銅焊盤（在 PCB 表面） ---
            bm_pad = bmesh.new()
            # 圓形焊盤：分段圓柱，高度 = CU_THICKNESS
            pad_bot = pad_z_top - CU_THICKNESS
            pad_top = pad_z_top
            p_center_bot = bm_pad.verts.new((0, 0, pad_bot))
            p_center_top = bm_pad.verts.new((0, 0, pad_top))
            p_bot_ring, p_top_ring = [], []
            for i in range(SEGMENTS_MID):
                a = 2.0 * math.pi * i / SEGMENTS_MID
                px = pad_radius * math.cos(a)
                py = pad_radius * math.sin(a)
                p_bot_ring.append(bm_pad.verts.new((px, py, pad_bot)))
                p_top_ring.append(bm_pad.verts.new((px, py, pad_top)))
            bm_pad.verts.ensure_lookup_table()
            for i in range(SEGMENTS_MID):
                j = (i + 1) % SEGMENTS_MID
                bm_pad.faces.new([p_bot_ring[i], p_bot_ring[j],
                                   p_top_ring[j], p_top_ring[i]])
                bm_pad.faces.new([p_center_bot, p_bot_ring[j], p_bot_ring[i]])
                bm_pad.faces.new([p_center_top, p_top_ring[i], p_top_ring[j]])

            pad_obj = bmesh_to_object(bm_pad, f"{name}_Pad_R{row+1}C{col+1}",
                                       material=copper_mat)
            pad_obj.location = (bx, by, 0)
            pad_objects.append(pad_obj)

            # --- 錫球（使用 bmesh UV 球體，Y 軸壓扁） ---
            # UV 球體：用經緯線建立
            bm_ball = bmesh.new()
            lat_segments = 16   # 緯線
            lon_segments = 24   # 經線

            ball_verts_grid = []  # [lat][lon]
            for la in range(lat_segments + 1):
                phi = math.pi * la / lat_segments  # 0 (頂) → π (底)
                row_verts = []
                for lo in range(lon_segments):
                    theta = 2.0 * math.pi * lo / lon_segments
                    # 標準球面座標
                    sx = ball_radius * math.sin(phi) * math.cos(theta)
                    sy = ball_radius * math.sin(phi) * math.sin(theta) * squash_y  # Y 壓扁
                    sz = ball_radius * math.cos(phi)
                    row_verts.append(bm_ball.verts.new((sx, sy, sz)))
                ball_verts_grid.append(row_verts)

            bm_ball.verts.ensure_lookup_table()

            # 建立面
            for la in range(lat_segments):
                for lo in range(lon_segments):
                    lo_next = (lo + 1) % lon_segments
                    v00 = ball_verts_grid[la][lo]
                    v01 = ball_verts_grid[la][lo_next]
                    v10 = ball_verts_grid[la + 1][lo]
                    v11 = ball_verts_grid[la + 1][lo_next]
                    bm_ball.faces.new([v00, v01, v11, v10])

            ball_obj = bmesh_to_object(bm_ball, f"{name}_Ball_R{row+1}C{col+1}",
                                        material=solder_mat)
            ball_obj.location = (bx, by, ball_center_z)
            ball_objects.append(ball_obj)

            # --- 焊錫 fillet（內凹環形） ---
            fillet_height = ball_radius * 0.55  # fillet 爬升約 0.11mm
            f_verts, f_faces = create_solder_fillet_simple(
                pad_radius=pad_radius,
                ball_radius=ball_radius,
                ball_center_z=ball_center_z - pad_z_top,  # 相對於焊盤表面
                fillet_height=fillet_height,
                segments=SEGMENTS_MID,
            )

            f_mesh = bpy.data.meshes.new(name=f"{name}_Fillet_R{row+1}C{col+1}")
            f_mesh.from_pydata(f_verts, [], f_faces)
            f_mesh.update()
            f_obj = bpy.data.objects.new(
                name=f"{name}_Fillet_R{row+1}C{col+1}",
                object_data=f_mesh
            )
            bpy.context.collection.objects.link(f_obj)
            f_obj.data.materials.append(solder_mat)
            # fillet 底部在焊盤表面
            f_obj.location = (bx, by, pad_z_top)
            fillet_objects.append(f_obj)

    total_balls = grid_size * grid_size
    print(f"  [BGA] 錫球: {total_balls} 顆 Ø{ball_diameter}mm (Y軸壓扁 {squash_y})")
    print(f"  [BGA] 焊盤: {total_balls} 個 Ø{pad_diameter}mm")
    print(f"  [BGA] Fillet: {total_balls} 個凹面焊錫爬升")

    # --- 1.3 Pin 1 標記（本體頂面角落三角凹槽） ---
    pin1_x = x - body_size / 2.0 + 2.0
    pin1_y = y - body_size / 2.0 + 2.0
    pin1_r = 0.6  # 圓形標記半徑

    bm_pin1 = _solid_cylinder_bm(radius=pin1_r, height=0.15, segments=24,
                                  z_bottom=body_top_z - 0.15)
    pin1_cutter = bmesh_to_object(bm_pin1, f"{name}_Pin1_Cutter")
    pin1_cutter.location = (pin1_x, pin1_y, 0)

    _apply_boolean_diff(body_obj, pin1_cutter)

    print(f"[ULTRA BGA] ✅ {name} 建立完成\n")
    return {
        'body': body_obj,
        'balls': ball_objects,
        'pads': pad_objects,
        'fillets': fillet_objects,
        'top_z': body_top_z,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：create_rf_shield_and_antenna(x, y)
# ══════════════════════════════════════════════════════════════════════════════

def create_rf_shield_and_antenna(x, y, name="U_RF"):
    """
    建立射頻屏蔽罩 + 板載倒 F 型天線。

    屏蔽罩結構：
        ┌──────────────────────────┐
        │  ○    ○    ○    ○       │  ← 4 個 Ø1.5mm 散熱孔（布爾挖出）
        │                          │
        │     20×15×3mm           │  ← 壁厚 0.2mm 的五面體殼
        │     亮銀色洋白銅        │     （底部開口）
        │                          │
        ├──┬──┬──┬──┬──┬──┬──┤
        │▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐│  ← 底部焊接引腳
        │══════════════════════│  ← PCB 頂面
        └──────────────────────┘

        天線（在屏蔽罩旁）：
        ┌──────────┐
        │ Shielding│  ═══╡  ← IFA 天線（倒 F 型）
        │   Can    │   │      2.4GHz λ/4 ≈ 16mm
        └──────────┘   │      線寬 0.5mm, 厚 0.035mm
                       │      材質：ENIG 鍍金
                   ═══╡  ← PCB 邊緣

    參數：
        x, y: 屏蔽罩中心在 PCB 上的座標
    """
    print(f"\n[ULTRA RF] 建立射頻模塊 @ ({x:.1f}, {y:.1f})")

    # --- 材質 ---
    shield_mat = create_shield_material()
    gold_mat = create_gold_trace_material()
    copper_mat = create_copper_pad_material()

    # --- 屏蔽罩幾何參數 ---
    can_size_x = 20.0
    can_size_y = 15.0
    can_height = 3.0
    wall_thickness = 0.2

    vent_hole_diameter = 1.5
    vent_hole_radius = vent_hole_diameter / 2.0
    vent_count = 4  # 2×2

    # Z 軸：屏蔽罩底部在 PCB 銅箔上方
    can_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    can_z_top = can_z_bottom + can_height
    can_z_center = (can_z_bottom + can_z_top) / 2.0

    # --- 2.1 建立屏蔽罩外殼（五面體，bmesh） ---
    # 方法：建立實心塊 → 內部挖空（布爾）→ 頂部挖散熱孔
    hx = can_size_x / 2.0
    hy = can_size_y / 2.0
    hz = can_height / 2.0

    bm_outer = bmesh.new()
    bmesh.ops.create_cube(bm_outer, size=2.0)
    bm_outer.verts.ensure_lookup_table()
    for v in bm_outer.verts:
        v.co = (v.co.x * hx, v.co.y * hy, v.co.z * hz)

    outer_obj = bmesh_to_object(bm_outer, f"{name}_Shield_Solid", material=shield_mat)
    outer_obj.location = (x, y, can_z_center)

    # 內部挖空塊（比外部小 2×壁厚）
    inner_hx = hx - wall_thickness
    inner_hy = hy - wall_thickness
    inner_hz = hz - wall_thickness  # 頂部壁厚
    inner_z_offset = wall_thickness / 2.0  # 內部塊向上偏移（底部開口）

    bm_inner = bmesh.new()
    bmesh.ops.create_cube(bm_inner, size=2.0)
    bm_inner.verts.ensure_lookup_table()
    for v in bm_inner.verts:
        v.co = (v.co.x * inner_hx, v.co.y * inner_hy, v.co.z * inner_hz)
    inner_cutter = bmesh_to_object(bm_inner, f"{name}_InnerCutter")
    inner_cutter.location = (x, y, can_z_center + wall_thickness / 2.0)

    _apply_boolean_diff(outer_obj, inner_cutter)
    shield_body = outer_obj
    shield_body.name = f"{name}_ShieldCan"
    print(f"  [RF] 屏蔽罩外殼: {can_size_x}×{can_size_y}×{can_height}mm, "
          f"壁厚 {wall_thickness}mm, 底部開口")

    # --- 2.2 頂部散熱通風孔（4 個 Ø1.5mm） ---
    vent_spacing_x = can_size_x * 0.22
    vent_spacing_y = can_size_y * 0.22

    vent_positions = [
        (x - vent_spacing_x, y - vent_spacing_y),
        (x + vent_spacing_x, y - vent_spacing_y),
        (x + vent_spacing_x, y + vent_spacing_y),
        (x - vent_spacing_x, y + vent_spacing_y),
    ]

    for vi, (vx, vy) in enumerate(vent_positions):
        bm_vent = _solid_cylinder_bm(
            radius=vent_hole_radius,
            height=can_height,
            segments=48,
            z_bottom=can_z_bottom
        )
        vent_cutter = bmesh_to_object(bm_vent, f"{name}_VentCutter_{vi+1}")
        vent_cutter.location = (vx, vy, 0)
        _apply_boolean_diff(shield_body, vent_cutter)

    print(f"  [RF] 散熱孔: {vent_count} 個 Ø{vent_hole_diameter}mm (2×2)")

    # --- 2.3 底部焊接引腳 ---
    # 在屏蔽罩四周底部邊緣，每隔 4mm 放置一個引腳
    # 引腳尺寸：長 0.4mm (Y方向), 寬 0.6mm (X方向), 高 0.2mm (Z方向)
    pin_length = 0.6   # 沿屏蔽罩邊緣方向
    pin_width = 0.4    # 垂直於邊緣方向（向外突出）
    pin_height = 0.2   # Z 軸（沉入焊盤）

    # 四個邊的引腳位置
    pin_objects = []

    # 計算每條邊的長度和引腳數量
    edges = [
        # (起始x, 起始y, 方向dx, 方向dy, 邊長, 引腳旋轉)
        (x - hx, y - hy,  1,  0, can_size_x, 0),           # 下邊（Y=-hy）
        (x - hx, y + hy,  1,  0, can_size_x, 0),           # 上邊（Y=+hy）
        (x - hx, y - hy,  0,  1, can_size_y, math.pi/2),   # 左邊（X=-hx）
        (x + hx, y - hy,  0,  1, can_size_y, math.pi/2),   # 右邊（X=+hx）
    ]

    pin_spacing = 4.0  # 引腳間距

    for edge_idx, (ex, ey, edx, edy, edge_len, erot) in enumerate(edges):
        # 計算這條邊上可以放多少個引腳
        num_pins = max(2, int(edge_len / pin_spacing))
        actual_spacing = edge_len / max(num_pins - 1, 1)

        for pi in range(num_pins):
            t = pi / max(num_pins - 1, 1)
            px = ex + edx * edge_len * t
            py = ey + edy * edge_len * t

            # 建立引腳（小方塊，用 bmesh）
            bm_pin = bmesh.new()
            bmesh.ops.create_cube(bm_pin, size=2.0)
            bm_pin.verts.ensure_lookup_table()
            for v in bm_pin.verts:
                v.co = (v.co.x * pin_length / 2.0,
                         v.co.y * pin_width / 2.0,
                         v.co.z * pin_height / 2.0)

            pin_obj = bmesh_to_object(
                bm_pin, f"{name}_Pin_E{edge_idx+1}_{pi+1}",
                material=shield_mat
            )

            # 引腳位置：在屏蔽罩底部邊緣，向內偏移半個引腳寬度
            # 垂直於邊緣的方向
            if edx == 1 and edy == 0:    # 水平邊
                perp_x, perp_y = 0, 0  # pin_width 向 Y 方向
                # 下邊：引腳向 Y+（向內）, 上邊：引腳向 Y-（向內）
                inward = 1 if edge_idx == 0 else -1
                pin_obj.location = (
                    px,
                    py + inward * (wall_thickness / 2.0),
                    can_z_bottom - pin_height / 2.0 + ZF_OFFSET
                )
            else:  # edx == 0 and edy == 1: 垂直邊
                inward = 1 if edge_idx == 2 else -1
                pin_obj.location = (
                    px + inward * (wall_thickness / 2.0),
                    py,
                    can_z_bottom - pin_height / 2.0 + ZF_OFFSET
                )

            pin_objects.append(pin_obj)

    print(f"  [RF] 焊接引腳: {len(pin_objects)} 個, 間距 {pin_spacing}mm")

    # --- 2.4 板載 IFA 天線（倒 F 型微帶天線） ---
    # 天線在屏蔽罩右側（X+ 方向），向 PCB 邊緣延伸
    # IFA 拓撲：
    #   饋點 ─── 主輻射臂 (沿 Y+ 方向, 平行 PCB 邊緣, λ/4 ≈ 16mm)
    #   │
    #   └── 短路臂 (沿 Y- 方向, 接地 via)

    ant_start_x = x + hx + 2.0  # 距屏蔽罩 2mm
    ant_y = y
    ant_width = 0.5      # 線寬
    ant_thickness = CU_THICKNESS
    ant_arm_length = 16.0  # 主臂長度（FR-4 上 λ_g/4 @ 2.45GHz）

    ant_z = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    ant_z_extrude = ant_thickness

    # --- 主輻射臂路徑（倒 F 形狀） ---
    # 路徑頂點（俯視圖）：
    #   饋點 (ant_start_x, ant_y)
    #   → 短水平饋線 (X+ 2mm)
    #   → 主臂向上 (Y+ ant_arm_length)
    #
    #   另外從饋點分出短路臂向下到接地點

    # 主臂路徑
    main_arm_path = [
        (ant_start_x, ant_y, ant_z + ant_z_extrude / 2.0),
        (ant_start_x + 2.0, ant_y, ant_z + ant_z_extrude / 2.0),  # 饋線
        (ant_start_x + 2.0, ant_y + ant_arm_length, ant_z + ant_z_extrude / 2.0),  # 主臂
    ]

    # 短路臂路徑（從饋點向下到 GND via）
    short_stub_path = [
        (ant_start_x, ant_y, ant_z + ant_z_extrude / 2.0),
        (ant_start_x + 1.5, ant_y - 2.5, ant_z + ant_z_extrude / 2.0),
    ]

    def _build_trace_from_path(points, width, extrude_h, obj_name, material):
        """使用 Blender Curve + bevel_object 建立微帶走線。"""
        # 建立矩形截面 bevel object
        bevel_curve_data = bpy.data.curves.new(
            name=f"{obj_name}_BevelProfile", type='CURVE')
        bevel_curve_data.dimensions = '2D'
        bevel_spline = bevel_curve_data.splines.new(type='POLY')
        hw_ = width / 2.0
        hh_ = extrude_h / 2.0
        bevel_pts = [(-hw_, -hh_, 0, 1), (hw_, -hh_, 0, 1),
                      (hw_, hh_, 0, 1), (-hw_, hh_, 0, 1)]
        bevel_spline.points.add(len(bevel_pts) - 1)
        for i, pt in enumerate(bevel_pts):
            bevel_spline.points[i].co = pt
        bevel_spline.use_cyclic_u = True
        bevel_obj = bpy.data.objects.new(
            name=f"{obj_name}_BevelObj", object_data=bevel_curve_data)
        bpy.context.collection.objects.link(bevel_obj)
        bevel_obj.hide_viewport = True
        bevel_obj.hide_render = True

        # 主路徑曲線
        curve_data = bpy.data.curves.new(name=f"{obj_name}_Path", type='CURVE')
        curve_data.dimensions = '3D'
        spline = curve_data.splines.new(type='POLY')
        spline.points.add(len(points) - 1)
        for i, pt in enumerate(points):
            spline.points[i].co = (*pt, 1.0)

        curve_obj = bpy.data.objects.new(name=obj_name, object_data=curve_data)
        bpy.context.collection.objects.link(curve_obj)

        # 設定截面
        curve_obj.data.bevel_object = bevel_obj
        curve_obj.data.extrude = 0  # 不額外擠出（bevel_object 已定義截面）

        # 轉換為 mesh
        bpy.context.view_layer.objects.active = curve_obj
        curve_obj.select_set(True)
        bpy.ops.object.convert(target='MESH')
        curve_obj.data.materials.append(material)

        return curve_obj

    # 建立主輻射臂
    trace_main = _build_trace_from_path(
        points=main_arm_path,
        width=ant_width,
        extrude_h=ant_thickness,
        obj_name=f"{name}_IFA_MainArm",
        material=gold_mat,
    )
    print(f"  [RF] IFA 主臂: {ant_arm_length}mm, 線寬 {ant_width}mm")

    # 建立短路臂
    trace_short = _build_trace_from_path(
        points=short_stub_path,
        width=ant_width * 0.6,
        extrude_h=ant_thickness,
        obj_name=f"{name}_IFA_ShortStub",
        material=gold_mat,
    )
    print(f"  [RF] IFA 短路臂: 線寬 {ant_width*0.6}mm")

    # --- 2.5 饋電點焊盤 ---
    feed_pad_w = 1.2
    bm_feed = bmesh.new()
    bmesh.ops.create_cube(bm_feed, size=2.0)
    bm_feed.verts.ensure_lookup_table()
    for v in bm_feed.verts:
        v.co = (v.co.x * feed_pad_w / 2.0,
                 v.co.y * ant_width / 2.0,
                 v.co.z * ant_thickness / 2.0)
    feed_obj = bmesh_to_object(bm_feed, f"{name}_FeedPad", material=copper_mat)
    feed_obj.location = (ant_start_x + 1.0, ant_y, ant_z + ant_thickness / 2.0)

    # --- 2.6 接地過孔（在短路臂末端） ---
    gnd_via_r = 0.3
    short_end = short_stub_path[-1]
    bm_gnd_via = _hollow_cylinder_bm(
        outer_r=gnd_via_r,
        inner_r=gnd_via_r * 0.5,
        height=PCB_H,
        segments=24,
    )
    gnd_via = bmesh_to_object(bm_gnd_via, f"{name}_GND_Via", material=copper_mat)
    gnd_via.location = (short_end[0], short_end[1], 0)
    print(f"  [RF] 接地過孔 @ ({short_end[0]:.1f}, {short_end[1]:.1f})")

    print(f"[ULTRA RF] ✅ {name} 建立完成\n")
    return {
        'shield_body': shield_body,
        'pins': pin_objects,
        'antenna_main': trace_main,
        'antenna_short': trace_short,
        'feed_pad': feed_obj,
        'ground_via': gnd_via,
        'top_z': can_z_top,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 整合執行
# ══════════════════════════════════════════════════════════════════════════════

def place_all_components():
    """在 PCB 上放置所有元件。"""
    # BGA 晶片：PCB 中央偏左
    bga = create_ultra_bga_chip(x=-30.0, y=0.0, name="U1_BGA144")

    # RF 模塊：PCB 右側（靠近邊緣以利天線輻射）
    rf = create_rf_shield_and_antenna(x=35.0, y=0.0, name="U2_RF")

    return {'bga': bga, 'rf': rf}


if __name__ == "__main__":
    print("=" * 60)
    print("  ATOS PRO — PCB 超精密建模 v5.0")
    print("  第二部分：BGA 晶片 + 射頻屏蔽罩 + IFA 天線")
    print("  核心 API: bmesh / mesh.from_pydata")
    print("=" * 60)
    print()

    # 檢查 PCB 基板
    pcb_found = False
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and 'PCB_Base' in obj.name:
            pcb_found = True
            print(f"[INFO] PCB 基板已存在: {obj.name}")
            break
    if not pcb_found:
        print("[WARN] 未檢測到 PCB 基板。請先執行 pcb_ultra_step1.py。")
        print("[WARN] 將繼續建立元件（可能懸浮在空中）。")

    # 放置元件
    result = place_all_components()

    # 統計
    total_objs = len(bpy.data.objects)
    total_verts = sum(len(o.data.vertices) for o in bpy.data.objects if o.type == 'MESH')
    total_faces = sum(len(o.data.polygons) for o in bpy.data.objects if o.type == 'MESH')

    print("=" * 60)
    print(f"  ✅ 第二部分完成")
    print(f"  總物件數: {total_objs}")
    print(f"  總頂點數: {total_verts:,}")
    print(f"  總面數:   {total_faces:,}")
    print(f"  BGA: 144 錫球 + 144 焊盤 + 144 Fillet")
    print(f"  RF:  屏蔽罩 (4 散熱孔 + {len(result['rf']['pins'])} 引腳) + IFA 天線")
    print("=" * 60)
    print()
    print("📌 第二部分完成。等待第三部分指令...")
