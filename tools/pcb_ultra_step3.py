"""
ATOS_PRO/tools/pcb_ultra_step3.py
Blender 4.2+ / Python 3.11 — PCB 超精密建模
第三部分：金手指 30° 斜切 + 40-Pin 鷗翼引腳 + 散熱片 + 鐵氧體電感

全部使用 bmesh / mesh.from_pydata + Matrix 變換。
與第一、第二部分的全域參數和材質完全相容。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import bmesh
import math

# ══════════════════════════════════════════════════════════════════════════════
# 全域參數（與步驟 1/2 一致）
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


# ══════════════════════════════════════════════════════════════════════════════
# 輔助函數
# ══════════════════════════════════════════════════════════════════════════════

def _bmesh_to_obj(bm, name, material=None):
    """bmesh → Mesh 物件 → 場景。"""
    mesh = bpy.data.meshes.new(name=name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name=name, object_data=mesh)
    bpy.context.collection.objects.link(obj)
    if material:
        obj.data.materials.append(material)
    return obj


def _apply_bool_diff(target, cutter):
    """target - cutter，應用後隱藏 cutter。"""
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    mod = target.modifiers.new(name=f"Bool_{cutter.name}", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)
    cutter.hide_viewport = True
    cutter.hide_render = True


def _solid_cyl_bm(radius, height, segments, z_bottom=0.0):
    """bmesh 實心圓柱。"""
    zt = z_bottom + height
    bm = bmesh.new()
    bc = bm.verts.new((0, 0, z_bottom))
    tc = bm.verts.new((0, 0, zt))
    br, tr = [], []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        x, y = radius * math.cos(a), radius * math.sin(a)
        br.append(bm.verts.new((x, y, z_bottom)))
        tr.append(bm.verts.new((x, y, zt)))
    bm.verts.ensure_lookup_table()
    for i in range(segments):
        j = (i + 1) % segments
        bm.faces.new([br[i], br[j], tr[j], tr[i]])
        bm.faces.new([bc, br[j], br[i]])
        bm.faces.new([tc, tr[i], tr[j]])
    return bm


def _get_or_create_mat(name, color, metallic, roughness, ior=1.5, anisotropic=0.0):
    """重用或建立材質。"""
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


# ══════════════════════════════════════════════════════════════════════════════
# 材質工廠
# ══════════════════════════════════════════════════════════════════════════════

def create_hard_gold_material():
    """
    硬鍍金材質（金手指）：#D4AF37 高光澤金色。
    Metallic=1.0, Roughness=0.08。
    """
    # #D4AF37 → (212/255, 175/255, 55/255) ≈ (0.831, 0.686, 0.216)
    mat = bpy.data.materials.new(name="PCB_HardGold")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.831, 0.686, 0.216, 1.0)  # #D4AF37
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.08
    bsdf.inputs['IOR'].default_value = 0.47

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    print("  [MAT] 硬鍍金: #D4AF37, Roughness=0.08")
    return mat


def create_lcp_plastic_material():
    """
    LCP 高密度塑料（排座本體）：黑色啞光。
    Metallic=0.0, Roughness=0.55。
    """
    mat = bpy.data.materials.new(name="PCB_LCP_Plastic")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # 微細表面噪波（模具痕跡）
    tex = nodes.new(type='ShaderNodeTexCoord')
    tex.location = (-400, 0)
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-150, 100)
    noise.inputs['Scale'].default_value = 250.0
    noise.inputs['Detail'].default_value = 6.0
    noise.inputs['Roughness'].default_value = 0.6
    links.new(tex.outputs['UV'], noise.inputs['Vector'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.04, 0.04, 0.045, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.55
    bsdf.inputs['IOR'].default_value = 1.65
    bsdf.inputs['Specular IOR Level'].default_value = 0.1

    bump = nodes.new(type='ShaderNodeBump')
    bump.location = (100, -100)
    bump.inputs['Strength'].default_value = 0.03
    bump.inputs['Distance'].default_value = 0.002
    links.new(noise.outputs['Fac'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    print("  [MAT] LCP 塑料: 黑色啞光, Roughness=0.55")
    return mat


def create_phosphor_bronze_material():
    """
    磷銅端子材質（鷗翼引腳）：金色調金屬。
    Metallic=1.0, Roughness=0.12。
    """
    return _get_or_create_mat(
        "PCB_PhosphorBronze", (0.82, 0.68, 0.35), 1.0, 0.12, ior=0.47)


def create_anodized_aluminum_material():
    """
    陽極氧化鋁（藍色散熱片）：金屬藍 + 各向異性拉絲。
    Metallic=0.75, Roughness=0.35, Anisotropic=0.4。
    """
    mat = bpy.data.materials.new(name="PCB_AnodizedAl_Blue")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # 拉絲紋理（沿 X 軸）
    tex = nodes.new(type='ShaderNodeTexCoord')
    tex.location = (-500, 0)
    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-300, 0)
    mapping.inputs['Scale'].default_value = (0.3, 20.0, 20.0)
    links.new(tex.outputs['UV'], mapping.inputs['Vector'])

    noise_brush = nodes.new(type='ShaderNodeTexNoise')
    noise_brush.location = (-100, 150)
    noise_brush.inputs['Scale'].default_value = 180.0
    noise_brush.inputs['Detail'].default_value = 6.0
    noise_brush.inputs['Roughness'].default_value = 0.5
    links.new(mapping.outputs['Vector'], noise_brush.inputs['Vector'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    # 陽極藍色
    bsdf.inputs['Base Color'].default_value = (0.14, 0.30, 0.55, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.75
    bsdf.inputs['Roughness'].default_value = 0.35
    bsdf.inputs['IOR'].default_value = 1.35
    bsdf.inputs['Anisotropic'].default_value = 0.4
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.0

    # 拉絲噪波 → Roughness ±0.06
    brush_range = nodes.new(type='ShaderNodeMapRange')
    brush_range.location = (80, 150)
    brush_range.inputs['From Min'].default_value = 0.0
    brush_range.inputs['From Max'].default_value = 1.0
    brush_range.inputs['To Min'].default_value = -0.06
    brush_range.inputs['To Max'].default_value = 0.06
    links.new(noise_brush.outputs['Fac'], brush_range.inputs['Value'])

    roughness_add = nodes.new(type='ShaderNodeMath')
    roughness_add.location = (180, 100)
    roughness_add.operation = 'ADD'
    rval = nodes.new(type='ShaderNodeValue')
    rval.location = (80, 60)
    rval.outputs[0].default_value = 0.35
    links.new(rval.outputs['Value'], roughness_add.inputs[0])
    links.new(brush_range.outputs['Result'], roughness_add.inputs[1])
    links.new(roughness_add.outputs['Value'], bsdf.inputs['Roughness'])

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    print("  [MAT] 陽極氧化鋁藍: Anisotropic=0.4 拉絲")
    return mat


def create_ferrite_material():
    """
    鐵氧體材質（功率電感）：暗灰 + 高頻 Bump 顆粒感。

    特徵：
        - 暗灰色基底 (#3A3A3E)
        - Noise Scale=500 (高頻) → Bump Strength=0.08 → 金屬粉末壓鑄粗糙感
        - Noise → 顏色微調 ±5%
        - Metallic=0.05（陶瓷本質）, Roughness=0.60
    """
    mat = bpy.data.materials.new(name="PCB_Ferrite_Grain")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    tex = nodes.new(type='ShaderNodeTexCoord')
    tex.location = (-600, 0)

    # 高頻顆粒噪波（模擬金屬粉末壓鑄表面）
    noise_hf = nodes.new(type='ShaderNodeTexNoise')
    noise_hf.location = (-350, 150)
    noise_hf.inputs['Scale'].default_value = 500.0     # 極高頻 → 細顆粒
    noise_hf.inputs['Detail'].default_value = 12.0
    noise_hf.inputs['Roughness'].default_value = 0.55
    links.new(tex.outputs['UV'], noise_hf.inputs['Vector'])

    # 中頻噪波（宏觀不均勻）
    noise_mf = nodes.new(type='ShaderNodeTexNoise')
    noise_mf.location = (-350, -100)
    noise_mf.inputs['Scale'].default_value = 80.0
    noise_mf.inputs['Detail'].default_value = 6.0
    noise_mf.inputs['Roughness'].default_value = 0.6
    links.new(tex.outputs['UV'], noise_mf.inputs['Vector'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (400, 0)
    bsdf.inputs['Base Color'].default_value = (0.227, 0.227, 0.243, 1.0)  # #3A3A3E
    bsdf.inputs['Metallic'].default_value = 0.05
    bsdf.inputs['Roughness'].default_value = 0.60
    bsdf.inputs['IOR'].default_value = 1.8
    bsdf.inputs['Specular IOR Level'].default_value = 0.08

    # 顏色：噪波引起 ±5% 明暗
    color_ramp = nodes.new(type='ShaderNodeValToRGB')
    color_ramp.location = (-100, 300)
    color_ramp.color_ramp.elements[0].color = (0.18, 0.18, 0.20, 1.0)
    color_ramp.color_ramp.elements[1].color = (0.26, 0.26, 0.28, 1.0)
    links.new(noise_mf.outputs['Fac'], color_ramp.inputs['Fac'])

    mix_color = nodes.new(type='ShaderNodeMix')
    mix_color.location = (150, 250)
    mix_color.data_type = 'RGBA'
    mix_color.blend_type = 'MIX'
    mix_color.inputs['Factor'].default_value = 0.4
    mix_color.inputs['A'].default_value = (0.227, 0.227, 0.243, 1.0)
    links.new(color_ramp.outputs['Color'], mix_color.inputs['B'])
    links.new(mix_color.outputs['Result'], bsdf.inputs['Base Color'])

    # Bump：高頻噪波 → 強凹凸（顆粒感）
    bump_hf = nodes.new(type='ShaderNodeBump')
    bump_hf.location = (150, 50)
    bump_hf.inputs['Strength'].default_value = 0.08
    bump_hf.inputs['Distance'].default_value = 0.001
    links.new(noise_hf.outputs['Fac'], bump_hf.inputs['Height'])

    # 中頻 Bump（宏觀凹凸）
    bump_mf = nodes.new(type='ShaderNodeBump')
    bump_mf.location = (150, -150)
    bump_mf.inputs['Strength'].default_value = 0.03
    bump_mf.inputs['Distance'].default_value = 0.005
    links.new(noise_mf.outputs['Fac'], bump_mf.inputs['Height'])

    # 合併兩層 Bump
    bump_add = nodes.new(type='ShaderNodeMath')
    bump_add.location = (280, -50)
    bump_add.operation = 'ADD'
    links.new(bump_hf.outputs['Normal'], bump_add.inputs[0])
    links.new(bump_mf.outputs['Normal'], bump_add.inputs[1])
    links.new(bump_add.outputs['Value'], bsdf.inputs['Normal'])

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (700, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    print("  [MAT] 鐵氧體: 高頻 Bump(Scale=500) + 中頻, Roughness=0.60")
    return mat


def create_hasl_terminal_material():
    """HASL 鍍錫端子材質：亮銀色。"""
    return _get_or_create_mat(
        "PCB_HASL_Terminal", (0.76, 0.77, 0.74), 0.95, 0.20, ior=1.8)


def create_solder_fillet_material():
    """焊錫 fillet 材質（與第二部分相容）。"""
    if "PCB_Solder_SAC305" in bpy.data.materials:
        return bpy.data.materials["PCB_Solder_SAC305"]
    return _get_or_create_mat(
        "PCB_Solder_SAC305", (0.710, 0.722, 0.694), 1.0, 0.12, ior=1.9)


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：create_precision_connectors()
# ══════════════════════════════════════════════════════════════════════════════

def create_precision_connectors():
    """
    建立金手指（30° 斜切）+ 40-Pin 鷗翼引腳 SMT 排座。

    金手指：
        - 30 個觸點, X:10~50mm, Y:0mm（PCB 底邊）
        - 每個 1.0×4.0×0.035mm, 間距 0.3mm（邊到邊）
        - 板邊端 30° 斜切倒角（矩陣 Shear 變換）
        - 材質: 硬鍍金 #D4AF37

    排座：
        - 本體 50.8×5.0×8.5mm LCP 塑料
        - 雙排 40 個金屬彈片插槽（布爾挖出）
        - 40 個鷗翼引腳, 每個 4 段折彎
        - 每腳與焊盤交界處有焊錫包覆凸起
    """
    print("\n" + "=" * 50)
    print("  [CONN] 建立精密連接器")
    print("=" * 50)

    gold_mat = create_hard_gold_material()
    lcp_mat = create_lcp_plastic_material()
    phosphor_mat = create_phosphor_bronze_material()
    solder_fillet_mat = create_solder_fillet_material()
    copper_mat = _get_or_create_mat("PCB_CopperPad", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18)

    # ===================================================================
    # 1A. 金手指（30 個，30° 斜切倒角）
    # ===================================================================
    print("\n  [GOLD] 建立 30 觸點金手指...")

    finger_count = 30
    finger_width = 1.0      # 單觸點寬
    finger_length = 4.0     # 從板邊向內延伸
    finger_thickness = CU_THICKNESS  # 0.035mm
    finger_gap = 0.3        # 邊到邊間隙
    finger_pitch = finger_width + finger_gap  # 1.3mm 中心距
    bevel_angle = math.radians(30)  # 30° 斜切

    # 金手指區域: X:10~50mm, Y:0mm (PCB 底邊)
    # 轉換到中心座標系: PCB 左下角 = (-70, -45)
    # X=10 → -60, X=50 → -20
    finger_start_x = 10.0 - PCB_L / 2.0  # -60
    finger_y = 0.0 - PCB_W / 2.0         # -45 (PCB 底邊)
    finger_z_bot = BOARD_TOP_Z + ZF_OFFSET
    finger_z_top = finger_z_bot + finger_thickness
    finger_z_center = (finger_z_bot + finger_z_top) / 2.0

    finger_span = (finger_count - 1) * finger_pitch

    finger_objects = []

    for i in range(finger_count):
        fx = finger_start_x + i * finger_pitch + finger_width / 2.0
        fy = finger_y + finger_length / 2.0

        # --- 使用 bmesh 建立單個金手指（長方體） ---
        bm_finger = bmesh.new()
        bmesh.ops.create_cube(bm_finger, size=2.0)
        bm_finger.verts.ensure_lookup_table()

        hw = finger_width / 2.0
        hl = finger_length / 2.0
        hh = finger_thickness / 2.0

        for v in bm_finger.verts:
            v.co = (v.co.x * hw, v.co.y * hl, v.co.z * hh)

        # --- 30° 斜切倒角（對板邊端 Y=-hl 的頂點進行 Shear 變換） ---
        # Shear 矩陣：將 Y 方向的位移耦合到 Z 方向
        # 對於 Y < 0 的頂點（板邊端），Z 座向上抬升模擬斜面
        # Shear factor = tan(30°) ≈ 0.577
        # Z' = Z + shear * (Y - Y_ref)
        shear_factor = math.tan(bevel_angle)
        y_ref = -hl  # 斜面起始 Y

        for v in bm_finger.verts:
            if v.co.y < y_ref + 0.001:  # 靠近板邊的頂點
                # 計算在 Y 方向上超過 y_ref 的距離
                dy = v.co.y - y_ref
                # 斜面向下削（Z 越低越靠近板邊）
                # 對於板邊尖端：Y = y_ref, Z 最高處被削掉
                if v.co.z > 0:  # 頂面頂點
                    v.co.z = v.co.z - shear_factor * abs(dy)
                # 底面頂點也同樣處理但方向相反（如果需要對稱斜面）

        # 更精確的做法：用布爾差集來切出斜面
        # 先用上面的近似，然後改用切割體
        # 建立斜面切割體（楔形）
        # 回到簡單方案：直接用一個旋轉的長方體做布爾差集
        bm_finger.verts.ensure_lookup_table()

        finger_obj = _bmesh_to_obj(bm_finger, f"GoldFinger_{i+1}", material=gold_mat)
        finger_obj.location = (fx, fy, finger_z_center)

        # --- 布爾斜面切割 ---
        # 建立一個繞 X 軸旋轉 30° 的薄長方體，放在板邊處切出斜面
        cutter_w = finger_width + 0.1
        cutter_l = finger_length * 0.4
        cutter_h = finger_thickness * 2.0

        bm_bevel_cutter = bmesh.new()
        bmesh.ops.create_cube(bm_bevel_cutter, size=2.0)
        bm_bevel_cutter.verts.ensure_lookup_table()
        for v in bm_bevel_cutter.verts:
            v.co = (v.co.x * cutter_w / 2.0,
                     v.co.y * cutter_l / 2.0,
                     v.co.z * cutter_h / 2.0)

        cutter_obj = _bmesh_to_obj(bm_bevel_cutter, f"FingerBevelCutter_{i+1}")
        # 旋轉使底面形成 30° 斜面（繞 X 軸旋轉）
        cutter_obj.rotation_euler = (bevel_angle, 0, 0)
        cutter_obj.location = (
            fx,
            finger_y + cutter_l * 0.3,
            finger_z_bot - cutter_h * 0.2
        )

        _apply_bool_diff(finger_obj, cutter_obj)
        finger_objects.append(finger_obj)

    print(f"  [GOLD] ✅ {finger_count} 個金手指, "
          f"{finger_width}mm×{finger_length}mm, "
          f"間隙 {finger_gap}mm, 30° 斜切")

    # ===================================================================
    # 1B. 40-Pin SMT 排座本體
    # ===================================================================
    print("\n  [CONN] 建立 40-Pin 雙排 SMT 排座...")

    conn_body_length = 50.8
    conn_body_width = 5.0
    conn_body_height = 8.5
    pin_count = 20            # 每排 20 針
    pin_pitch = 2.54          # 2.54mm (0.1")
    row_spacing = 2.54        # 雙排間距

    # 排座放置位置（PCB 中央偏上）
    conn_cx = 0.0
    conn_cy = 25.0

    # Z 軸：排座底部在 PCB 面上方（留出鷗翼引腳的空間）
    conn_z_bottom = BOARD_TOP_Z + CU_THICKNESS + 1.2 + ZF_OFFSET  # 引腳垂直段高度
    conn_z_top = conn_z_bottom + conn_body_height
    conn_z_center = (conn_z_bottom + conn_z_top) / 2.0

    # --- 本體（bmesh 長方體） ---
    bm_body = bmesh.new()
    bmesh.ops.create_cube(bm_body, size=2.0)
    bm_body.verts.ensure_lookup_table()
    hlx = conn_body_length / 2.0
    hly = conn_body_width / 2.0
    hlz = conn_body_height / 2.0
    for v in bm_body.verts:
        v.co = (v.co.x * hlx, v.co.y * hly, v.co.z * hlz)

    body_obj = _bmesh_to_obj(bm_body, "J1_SMT_Body", material=lcp_mat)
    body_obj.location = (conn_cx, conn_cy, conn_z_center)
    print(f"  [CONN] LCP 本體: {conn_body_length}×{conn_body_width}×{conn_body_height}mm")

    # --- 雙排插槽（布爾挖出） ---
    slot_width = 0.7
    slot_length = (pin_count - 1) * pin_pitch + 1.0  # 略長於針腳跨度
    slot_depth = 5.0

    slot_a_y = conn_cy + row_spacing / 2.0
    slot_b_y = conn_cy - row_spacing / 2.0

    slot_z_bottom = conn_z_top - slot_depth
    slot_z_center = slot_z_bottom + slot_depth / 2.0

    for slot_idx, slot_y in enumerate([slot_a_y, slot_b_y]):
        bm_slot = bmesh.new()
        bmesh.ops.create_cube(bm_slot, size=2.0)
        bm_slot.verts.ensure_lookup_table()
        for v in bm_slot.verts:
            v.co = (v.co.x * slot_length / 2.0,
                     v.co.y * slot_width / 2.0,
                     v.co.z * (slot_depth + 0.2) / 2.0)
        slot_obj = _bmesh_to_obj(bm_slot, f"J1_SlotCutter_{slot_idx+1}")
        slot_obj.location = (conn_cx, slot_y, slot_z_center)
        _apply_bool_diff(body_obj, slot_obj)

    print(f"  [CONN] 雙排插槽: 2 × {slot_length:.1f}mm")

    # --- 內部金屬彈片（每個插槽位置 2 片） ---
    contact_w = 0.3
    contact_t = 0.12
    contact_h = 3.5

    for row_idx, slot_y in enumerate([slot_a_y, slot_b_y]):
        for pin_i in range(pin_count):
            px = conn_cx - (pin_count - 1) * pin_pitch / 2.0 + pin_i * pin_pitch

            for side_sign in [-1, 1]:
                bm_contact = bmesh.new()
                bmesh.ops.create_cube(bm_contact, size=2.0)
                bm_contact.verts.ensure_lookup_table()
                for v in bm_contact.verts:
                    v.co = (v.co.x * contact_t / 2.0,
                             v.co.y * contact_w / 2.0,
                             v.co.z * contact_h / 2.0)
                contact_obj = _bmesh_to_obj(
                    bm_contact,
                    f"J1_Contact_R{row_idx+1}P{pin_i+1}S{side_sign:+}",
                    material=phosphor_mat
                )
                contact_obj.location = (
                    px,
                    slot_y + side_sign * (slot_width / 2.0 - contact_w / 2.0),
                    slot_z_bottom + contact_h / 2.0 + 0.1
                )

    print(f"  [CONN] 金屬彈片: {pin_count*2*2} 片")

    # ===================================================================
    # 1C. 鷗翼形（Gull-wing）引腳
    # ===================================================================
    # 每個引腳由 4 段折彎組成：
    #   Seg 1: 向下延伸（從本體側面下來的垂直段）
    #   Seg 2: 向外平折（離開本體的短水平段 = shoulder）
    #   Seg 3: 垂直貼地（下降到 PCB 面的垂直段 = drop）
    #   Seg 4: 尾部微翹（接觸焊盤後末端輕微上彎 = foot with toe-up）

    pin_thickness = 0.2     # 磷銅片厚度
    pin_width = 0.45        # 引腳寬度
    shoulder_len = 1.8      # Seg 2 肩部水平長度
    drop_height = 1.0       # Seg 3 垂直下降高度
    foot_len = 1.5          # Seg 4 腳部水平長度
    toe_up_angle = math.radians(8)  # 尾部微翹 8°

    # 引腳起始位置（本體側面底部）
    pin_z_start = conn_z_bottom  # 從本體底部開始

    gull_wing_objects = []
    fillet_objects = []

    for row_idx in range(2):
        row_label = "A" if row_idx == 0 else "B"
        # 引腳從本體側面伸出
        if row_idx == 0:  # Row A: 向 Y+ 方向
            body_edge_y = conn_cy + conn_body_width / 2.0
            direction = 1
        else:  # Row B: 向 Y- 方向
            body_edge_y = conn_cy - conn_body_width / 2.0
            direction = -1

        for pin_i in range(pin_count):
            px = conn_cx - (pin_count - 1) * pin_pitch / 2.0 + pin_i * pin_pitch

            # --- 計算 4 段折彎的頂點 ---
            # 各段的端點（以引腳截面中心線為基準）
            # P0: 本體側面起點
            p0 = (px, body_edge_y, pin_z_start)
            # P1: Seg1 向下延伸結束（同時也是 Seg2 起點）
            p1 = (px, body_edge_y, pin_z_start - drop_height * 0.15)
            # P2: Seg2 向外平折結束 = shoulder 端點
            p2 = (px, body_edge_y + direction * shoulder_len, p1[2])
            # P3: Seg3 垂直下降結束（下降到 PCB 面上方）
            p3 = (px, p2[1], BOARD_TOP_Z + CU_THICKNESS + pin_thickness / 2.0 + ZF_OFFSET)
            # P4: Seg4 水平腳部 + 尾部微翹
            p4 = (px, p3[1] + direction * foot_len, p3[2])

            # --- 建立 4 段幾何（每段都是一個細長方體） ---
            # Seg1: 垂直向下
            seg1_cy = (p0[1] + p1[1]) / 2.0
            seg1_cz = (p0[2] + p1[2]) / 2.0
            seg1_len = abs(p0[2] - p1[2])

            bm_s1 = bmesh.new()
            bmesh.ops.create_cube(bm_s1, size=2.0)
            bm_s1.verts.ensure_lookup_table()
            for v in bm_s1.verts:
                v.co = (v.co.x * pin_width / 2.0,
                         v.co.y * pin_thickness / 2.0,
                         v.co.z * seg1_len / 2.0)
            s1_obj = _bmesh_to_obj(bm_s1,
                f"J1_GullWing_{row_label}{pin_i+1}_S1", material=phosphor_mat)
            s1_obj.location = (px, p1[1], seg1_cz)
            gull_wing_objects.append(s1_obj)

            # Seg2: 水平向外（shoulder）
            seg2_len = shoulder_len
            seg2_cy = p1[1] + direction * seg2_len / 2.0

            bm_s2 = bmesh.new()
            bmesh.ops.create_cube(bm_s2, size=2.0)
            bm_s2.verts.ensure_lookup_table()
            for v in bm_s2.verts:
                v.co = (v.co.x * pin_width / 2.0,
                         v.co.y * seg2_len / 2.0,
                         v.co.z * pin_thickness / 2.0)
            s2_obj = _bmesh_to_obj(bm_s2,
                f"J1_GullWing_{row_label}{pin_i+1}_S2", material=phosphor_mat)
            s2_obj.location = (px, seg2_cy, p1[2])
            gull_wing_objects.append(s2_obj)

            # Seg3: 垂直下降（drop，連接到 PCB 面）
            seg3_len = abs(p2[2] - p3[2])
            seg3_cz = (p2[2] + p3[2]) / 2.0

            bm_s3 = bmesh.new()
            bmesh.ops.create_cube(bm_s3, size=2.0)
            bm_s3.verts.ensure_lookup_table()
            for v in bm_s3.verts:
                v.co = (v.co.x * pin_width / 2.0,
                         v.co.y * pin_thickness / 2.0,
                         v.co.z * seg3_len / 2.0)
            s3_obj = _bmesh_to_obj(bm_s3,
                f"J1_GullWing_{row_label}{pin_i+1}_S3", material=phosphor_mat)
            s3_obj.location = (px, p2[1], seg3_cz)
            gull_wing_objects.append(s3_obj)

            # Seg4: 水平腳部（foot），末端微翹
            seg4_len = foot_len
            seg4_cy = p3[1] + direction * seg4_len / 2.0

            bm_s4 = bmesh.new()
            bmesh.ops.create_cube(bm_s4, size=2.0)
            bm_s4.verts.ensure_lookup_table()
            for v in bm_s4.verts:
                v.co = (v.co.x * pin_width / 2.0,
                         v.co.y * seg4_len / 2.0,
                         v.co.z * pin_thickness / 2.0)
            s4_obj = _bmesh_to_obj(bm_s4,
                f"J1_GullWing_{row_label}{pin_i+1}_S4", material=phosphor_mat)
            s4_obj.location = (px, seg4_cy, p3[2])

            # 尾部微翹：繞 X 軸旋轉微小角度
            # 翹起方向：腳部末端（遠離本體的那端）向上
            toe_rotation = -direction * toe_up_angle
            s4_obj.rotation_euler = (toe_rotation, 0, 0)
            gull_wing_objects.append(s4_obj)

            # --- 焊錫包覆凸起（Solder Fillet） ---
            # 在引腳腳部與 PCB 焊盤之間建立一圈梯形凸起
            # 使用 from_pydata 建立凹面環形
            fillet_bot_w = pin_width + 0.5
            fillet_top_w = pin_width + 0.08
            fillet_bot_l = foot_len * 0.6
            fillet_top_l = pin_thickness + 0.1
            fillet_h = 0.25
            fillet_z_bot = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET * 2
            fillet_z_top = fillet_z_bot + fillet_h

            # 8 個頂點（截錐體）
            hbw, htw = fillet_bot_w / 2.0, fillet_top_w / 2.0
            hbl, htl = fillet_bot_l / 2.0, fillet_top_l / 2.0
            fcx = px
            fcy = p3[1] + direction * foot_len * 0.5

            f_verts = [
                (fcx - hbl, fcy - hbw, fillet_z_bot),
                (fcx + hbl, fcy - hbw, fillet_z_bot),
                (fcx + hbl, fcy + hbw, fillet_z_bot),
                (fcx - hbl, fcy + hbw, fillet_z_bot),
                (fcx - htl, fcy - htw, fillet_z_top),
                (fcx + htl, fcy - htw, fillet_z_top),
                (fcx + htl, fcy + htw, fillet_z_top),
                (fcx - htl, fcy + htw, fillet_z_top),
            ]
            f_faces = [
                (0, 1, 2, 3), (4, 7, 6, 5),
                (0, 4, 5, 1), (1, 5, 6, 2),
                (2, 6, 7, 3), (3, 7, 4, 0),
            ]
            f_mesh = bpy.data.meshes.new(name=f"J1_Fillet_{row_label}{pin_i+1}")
            f_mesh.from_pydata(f_verts, [], f_faces)
            f_mesh.update()
            f_obj = bpy.data.objects.new(
                name=f"J1_Fillet_{row_label}{pin_i+1}", object_data=f_mesh)
            bpy.context.collection.objects.link(f_obj)
            f_obj.data.materials.append(solder_fillet_mat)
            fillet_objects.append(f_obj)

    print(f"  [CONN] 鷗翼引腳: {len(gull_wing_objects)} 個幾何段 (4段×40腳)")
    print(f"  [CONN] 焊錫包覆: {len(fillet_objects)} 個 fillet")

    # --- Pin 1 標記（絲印三角形凹槽） ---
    pin1_x = conn_cx - conn_body_length / 2.0 + 2.0
    pin1_y = conn_cy + conn_body_width / 2.0 + 1.0
    bm_pin1 = _solid_cyl_bm(radius=0.5, height=0.2, segments=3, z_bottom=conn_z_top - 0.2)
    pin1_cutter = _bmesh_to_obj(bm_pin1, "J1_Pin1Mark_Cutter")
    pin1_cutter.location = (pin1_x, pin1_y, 0)
    pin1_cutter.rotation_euler = (0, 0, math.radians(30))
    _apply_bool_diff(body_obj, pin1_cutter)

    print(f"\n  [CONN] ✅ 全部連接器建立完成\n")
    return {
        'gold_fingers': finger_objects,
        'conn_body': body_obj,
        'gull_wings': gull_wing_objects,
        'fillets': fillet_objects,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：create_thermal_and_power_blocks()
# ══════════════════════════════════════════════════════════════════════════════

def create_thermal_and_power_blocks():
    """
    建立散熱片 + 一體成型功率電感。

    散熱片：
        - 20×20×8mm, 基座 2mm + 8 鰭片 (0.5×6mm, 間距 2mm)
        - 陽極氧化鋁藍, Anisotropic 拉絲

    功率電感：
        - 12×12×6mm, 邊緣 R0.5mm 圓角
        - 鐵氧體暗灰 + 高頻 Bump 顆粒
        - U 型 HASL 鍍錫端子（0.1mm 厚）
    """
    print("\n" + "=" * 50)
    print("  [THERMAL] 建立散熱片 + 功率電感")
    print("=" * 50)

    al_mat = create_anodized_aluminum_material()
    ferrite_mat = create_ferrite_material()
    hasl_mat = create_hasl_terminal_material()

    # ===================================================================
    # 2A. 散熱片
    # ===================================================================
    print("\n  [HS] 建立鋁散熱片...")

    hs_size = 20.0      # 長寬
    hs_total_h = 8.0     # 總高
    base_h = 2.0         # 基座厚度
    fin_count = 8
    fin_thickness = 0.5
    fin_height = hs_total_h - base_h  # 6mm
    fin_gap = 2.0  # 槽間距（中心到中心）

    hs_cx = -30.0
    hs_cy = -25.0

    hs_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    base_z_top = hs_z_bottom + base_h
    base_z_center = hs_z_bottom + base_h / 2.0
    fin_z_bottom = base_z_top
    fin_z_top = fin_z_bottom + fin_height
    fin_z_center = (fin_z_bottom + fin_z_top) / 2.0

    # --- 基座 ---
    bm_base = bmesh.new()
    bmesh.ops.create_cube(bm_base, size=2.0)
    bm_base.verts.ensure_lookup_table()
    for v in bm_base.verts:
        v.co = (v.co.x * hs_size / 2.0,
                 v.co.y * hs_size / 2.0,
                 v.co.z * base_h / 2.0)
    base_obj = _bmesh_to_obj(bm_base, "HS1_Base", material=al_mat)
    base_obj.location = (hs_cx, hs_cy, base_z_center)
    print(f"  [HS] 基座: {hs_size}×{hs_size}×{base_h}mm")

    # --- 平行鰭片（沿 Y 軸陣列） ---
    # 8 片鰭片分佈在 20mm 寬度內
    # 總鰭片厚度 = 8 × 0.5 = 4mm, 剩餘空間 = 16mm, 7 個槽
    # 實際間距 = (20 - 8*0.5) / 7 ≈ 2.286mm → 用戶指定槽間距 2.0mm 應為中心距
    # 重新計算: 若中心距 = 2.0mm, 則總佔用 = 0.5 + 7*2.0 = 14.5mm（太窄）
    # 使用 fin_gap=2.0 作為槽間距（邊到邊）, 中心距 = 0.5+2.0 = 2.5mm
    fin_pitch = fin_thickness + fin_gap  # 2.5mm
    fin_total_span = (fin_count - 1) * fin_pitch  # 17.5mm
    fin_start_y = hs_cy - fin_total_span / 2.0

    fin_objects = []
    for i in range(fin_count):
        fy = fin_start_y + i * fin_pitch

        bm_fin = bmesh.new()
        bmesh.ops.create_cube(bm_fin, size=2.0)
        bm_fin.verts.ensure_lookup_table()
        for v in bm_fin.verts:
            v.co = (v.co.x * hs_size / 2.0,
                     v.co.y * fin_thickness / 2.0,
                     v.co.z * fin_height / 2.0)
        fin_obj = _bmesh_to_obj(bm_fin, f"HS1_Fin_{i+1}", material=al_mat)
        fin_obj.location = (hs_cx, fy, fin_z_center)
        fin_objects.append(fin_obj)

    print(f"  [HS] 鰭片: {fin_count} 片 × {fin_thickness}mm, "
          f"高 {fin_height}mm, 間距 {fin_pitch}mm")

    # ===================================================================
    # 2B. 一體成型功率電感
    # ===================================================================
    print("\n  [IND] 建立功率電感...")

    ind_size = 12.0
    ind_height = 6.0
    ind_radius = 0.5  # 邊緣圓角半徑
    term_thickness = 0.1   # U 型端子厚度
    term_width = 13.0      # 端子總寬（比本體略寬）
    term_depth = 3.5       # 端子 U 型包裹深度

    ind_cx = 45.0
    ind_cy = 25.0

    ind_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    ind_z_top = ind_z_bottom + ind_height
    ind_z_center = (ind_z_bottom + ind_z_top) / 2.0

    # --- 鐵氧體本體（bmesh 立方體 + 倒角） ---
    bm_ind = bmesh.new()
    bmesh.ops.create_cube(bm_ind, size=2.0)
    bm_ind.verts.ensure_lookup_table()
    for v in bm_ind.verts:
        v.co = (v.co.x * ind_size / 2.0,
                 v.co.y * ind_size / 2.0,
                 v.co.z * ind_height / 2.0)

    # 對所有邊緣做 0.5mm 圓角
    all_edges = list(bm_ind.edges)
    if len(all_edges) > 0:
        bmesh.ops.bevel(
            bm_ind,
            geom=all_edges,
            offset=ind_radius,
            offset_type='OFFSET',
            segments=4,
            profile=0.5,
            affect='EDGES',
        )
        print(f"  [IND] 本體邊緣圓角: R{ind_radius}mm × 4 segments")

    ind_body = _bmesh_to_obj(bm_ind, "L1_FerriteBody", material=ferrite_mat)
    ind_body.location = (ind_cx, ind_cy, ind_z_center)

    # --- U 型 HASL 端子（兩端） ---
    terminal_objects = []

    for end_sign in [-1, 1]:  # 左端和右端
        end_label = "L" if end_sign == -1 else "R"

        # 端子 X 位置（在本體端面）
        term_x = ind_cx + end_sign * (ind_size / 2.0 - term_depth / 2.0)

        # U 型結構：3 段
        # 頂段（覆蓋本體頂面邊緣）
        top_z = ind_z_top + term_thickness / 2.0 + ZF_OFFSET
        bm_top = bmesh.new()
        bmesh.ops.create_cube(bm_top, size=2.0)
        bm_top.verts.ensure_lookup_table()
        for v in bm_top.verts:
            v.co = (v.co.x * term_depth / 2.0,
                     v.co.y * term_width / 2.0,
                     v.co.z * term_thickness / 2.0)
        top_obj = _bmesh_to_obj(bm_top, f"L1_TermTop_{end_label}", material=hasl_mat)
        top_obj.location = (term_x, ind_cy, top_z)
        terminal_objects.append(top_obj)

        # 垂直段（外側面）
        vert_x = ind_cx + end_sign * (ind_size / 2.0 + term_thickness / 2.0)
        bm_vert = bmesh.new()
        bmesh.ops.create_cube(bm_vert, size=2.0)
        bm_vert.verts.ensure_lookup_table()
        for v in bm_vert.verts:
            v.co = (v.co.x * term_thickness / 2.0,
                     v.co.y * term_width / 2.0,
                     v.co.z * ind_height / 2.0)
        vert_obj = _bmesh_to_obj(bm_vert, f"L1_TermVert_{end_label}", material=hasl_mat)
        vert_obj.location = (vert_x, ind_cy, ind_z_center)
        terminal_objects.append(vert_obj)

        # 底段（焊接腳，在 PCB 焊盤上）
        bottom_z = ind_z_bottom - term_thickness / 2.0 - ZF_OFFSET
        bm_bottom = bmesh.new()
        bmesh.ops.create_cube(bm_bottom, size=2.0)
        bm_bottom.verts.ensure_lookup_table()
        for v in bm_bottom.verts:
            v.co = (v.co.x * term_depth / 2.0,
                     v.co.y * term_width / 2.0,
                     v.co.z * term_thickness / 2.0)
        bottom_obj = _bmesh_to_obj(bm_bottom, f"L1_TermBottom_{end_label}", material=hasl_mat)
        bottom_obj.location = (term_x, ind_cy, bottom_z)
        terminal_objects.append(bottom_obj)

    print(f"  [IND] U 型 HASL 端子: {len(terminal_objects)} 段, 厚 {term_thickness}mm")

    # --- 頂面極性標記凹點 ---
    dot_r = 0.7
    bm_dot = _solid_cyl_bm(radius=dot_r, height=0.2, segments=24,
                            z_bottom=ind_z_top - 0.2)
    dot_cutter = _bmesh_to_obj(bm_dot, "L1_PolarityDot_Cutter")
    dot_cutter.location = (ind_cx - ind_size / 2.0 + 3.0,
                            ind_cy - ind_size / 2.0 + 3.0, 0)
    _apply_bool_diff(ind_body, dot_cutter)

    print(f"\n  [THERMAL] ✅ 散熱片 + 電感建立完成\n")
    return {
        'heatsink_base': base_obj,
        'heatsink_fins': fin_objects,
        'inductor_body': ind_body,
        'inductor_terminals': terminal_objects,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 整合執行
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ATOS PRO — PCB 超精密建模 v5.0")
    print("  第三部分：金手指 + 鷗翼引腳 + 散熱片 + 電感")
    print("=" * 60)
    print()

    # 檢查基板
    pcb_found = any('PCB_Base' in o.name for o in bpy.data.objects if o.type == 'MESH')
    if not pcb_found:
        print("[WARN] 未檢測到 PCB 基板。請先執行 pcb_ultra_step1.py。")

    # 執行
    conn = create_precision_connectors()
    thermal = create_thermal_and_power_blocks()

    # 統計
    total_objs = len(bpy.data.objects)
    total_verts = sum(len(o.data.vertices) for o in bpy.data.objects if o.type == 'MESH')
    total_faces = sum(len(o.data.polygons) for o in bpy.data.objects if o.type == 'MESH')

    print("=" * 60)
    print(f"  ✅ 第三部分完成")
    print(f"  總物件數: {total_objs}")
    print(f"  總頂點數: {total_verts:,}")
    print(f"  總面數:   {total_faces:,}")
    print(f"  金手指: 30 觸點 30° 斜切")
    print(f"  排座: 40-Pin 鷗翼引腳 ({len(conn['gull_wings'])} 段 + {len(conn['fillets'])} fillet)")
    print(f"  散熱片: {len(thermal['heatsink_fins'])} 鰭片")
    print(f"  電感: 鐵氧體 R0.5 + U型HASL端子")
    print("=" * 60)
    print()
    print("📌 第三部分完成。等待第四部分指令...")
