"""
ATOS_PRO/tools/pcb_modeler_step3.py
Blender 4.2+ / Python 3.11 — PCB 超逼真建模脚本
步驟 3：金手指 + 高密度排座 + 散熱片 + 大功率電感

與步驟 1、2 的參數和材質命名完全相容。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import math

# ══════════════════════════════════════════════════════════════════════════════
# 從步驟 1/2 繼承的全局參數（確保相容性）
# ══════════════════════════════════════════════════════════════════════════════

PCB_L = 140.0
PCB_W = 90.0
PCB_H = 1.6

CU_THICKNESS = 0.035
SOLDERMASK_THICKNESS = 0.02
SILKSCREEN_THICKNESS = 0.01

ZF_OFFSET = 0.002       # Z-Fighting 防止偏移量
BOARD_TOP_Z = PCB_H / 2.0  # PCB 頂面 Z 座標


# ══════════════════════════════════════════════════════════════════════════════
# 輔助工具函數
# ══════════════════════════════════════════════════════════════════════════════

def _cube(name, x, y, z, sx, sy, sz, material=None):
    """建立立方體，自動應用縮放。"""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, y, z))
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (sx, sy, sz)
    bpy.ops.object.transform_apply(scale=True)
    if material:
        obj.data.materials.append(material)
    return obj


def _cylinder(name, x, y, z_bottom, z_top, radius, vertices=64, material=None):
    """建立圓柱體。"""
    height = z_top - z_bottom
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=vertices, radius=radius, depth=height,
        location=(x, y, (z_bottom + z_top) / 2.0)
    )
    obj = bpy.context.active_object
    obj.name = name
    if material:
        obj.data.materials.append(material)
    return obj


def _boolean_difference(target, cutter, apply=True):
    """target - cutter 布爾差集。"""
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    mod = target.modifiers.new(name=f"Bool_{cutter.name}", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter
    if apply:
        bpy.ops.object.modifier_apply(modifier=mod.name)
        cutter.hide_viewport = True
        cutter.hide_render = True
    return target


def _make_mesh_from_data(obj_name, verts, faces, material=None):
    """從頂點和面列表直接建立網格物體。"""
    mesh = bpy.data.meshes.new(name=obj_name)
    obj = bpy.data.objects.new(name=obj_name, object_data=mesh)
    bpy.context.collection.objects.link(obj)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    if material:
        obj.data.materials.append(material)
    return obj


def _get_or_create_material(name, color, metallic, roughness, ior=1.5,
                             transmission=0.0, alpha=1.0):
    """重用已有材質或建立新材質（與步驟 1/2 相容）。"""
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
    bsdf.inputs['Transmission Weight'].default_value = transmission
    bsdf.inputs['Alpha'].default_value = alpha

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    print(f"  [MAT] 建立材質: {name}")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：邊緣金手指（Gold Fingers）
# ══════════════════════════════════════════════════════════════════════════════

def create_gold_fingers(board_bottom_edge_y, start_x, count=30):
    """
    在 PCB 底邊（Y = 板邊緣）建立一排標準 PCIe 風格的金手指觸點。

    金手指工藝規格（ENIG + 硬金電鍍）：
        - 單個觸點: 寬 1.0mm × 長 4.0mm × 厚 0.035mm（1oz 銅 + Ni/Au 鍍層）
        - 間距: 0.3mm（邊到邊），即中心距 1.3mm
        - 插入端 30° 斜切倒角（Beveling），便於插入插槽並減少磨損
        - 材質: 亮金色（Hard Gold over Nickel）

    參數：
        board_bottom_edge_y: PCB 底邊的 Y 座標（金手指從此處向內延伸）
        start_x: 金手指陣列起始 X 座標（左端）
        count: 金手指數量
    """
    print(f"\n[GOLD] 建立金手指: {count} 個觸點")

    # --- 金手指材質 ---
    gold_mat = _get_or_create_material(
        "PCB_HardGold", (0.92, 0.75, 0.22), 1.0, 0.06, ior=0.47
    )

    # --- 幾何參數 ---
    finger_width = 1.0        # 單個觸點寬度 mm
    finger_length = 4.0       # 觸點長度（從板邊向內）mm
    finger_thickness = CU_THICKNESS  # 0.035mm（銅 + 金鍍層總厚）
    finger_pitch = 1.3        # 中心距 = 1.0mm 寬 + 0.3mm 間隙
    bevel_angle = math.radians(30)  # 30° 斜切倒角

    # Z 軸位置（金手指在 PCB 頂面）
    finger_z_bottom = BOARD_TOP_Z + ZF_OFFSET
    finger_z_top = finger_z_bottom + finger_thickness

    # Y 軸位置
    # board_bottom_edge_y 是 PCB 底邊的 Y 值 = -PCB_W/2
    # 金手指從板邊開始向 Y+ 方向延伸 finger_length
    finger_y_start = board_bottom_edge_y  # 板邊
    finger_y_end = board_bottom_edge_y + finger_length  # 向內 4mm

    print(f"  [GOLD] 觸點: {finger_width}×{finger_length}mm, 間距 {finger_pitch}mm")
    print(f"  [GOLD] 斜切倒角: {math.degrees(bevel_angle):.0f}°")

    finger_objects = []

    for i in range(count):
        # 計算此觸點的中心 X 座標
        cx = start_x + i * finger_pitch
        cy = finger_y_start + finger_length / 2.0  # 觸點中心 Y
        cz = (finger_z_bottom + finger_z_top) / 2.0

        # --- 建立主體長方體 ---
        finger_name = f"GoldFinger_{i+1}"
        finger = _cube(
            name=finger_name,
            x=cx, y=cy, z=cz,
            sx=finger_width,
            sy=finger_length,
            sz=finger_thickness,
            material=gold_mat
        )
        finger_objects.append(finger)

        # --- 30° 斜切倒角（在 Y = board_bottom_edge_y 的邊緣上） ---
        # 倒角在插入端（板邊外側），斜面從頂面向前下方傾斜 30°
        # 這意味著我們需要在 finger 的板邊端切出一個斜面

        # 方法：用一個旋轉的立方體做布爾差集來切出斜面
        # 斜面沿 X 軸方向，在 YZ 平面內傾斜 30°
        # 切割體：一個長條形立方體，繞 X 軸旋轉 30°，放在觸點板邊端

        # 切割體參數
        cutter_size_x = finger_width + 0.1     # 稍寬以完全覆蓋
        cutter_size_y = finger_length * 0.35   # 斜面覆蓋長度的 35%
        cutter_size_z = finger_thickness * 1.5 # 稍高以確保切穿

        # 切割體中心（放在板邊緣）
        cutter_cx = cx
        cutter_cy = finger_y_start + cutter_size_y / 2.0 * math.cos(bevel_angle)
        cutter_cz = finger_z_bottom - cutter_size_z * 0.3

        cutter = _cube(
            name=f"{finger_name}_BevelCutter",
            x=cutter_cx, y=cutter_cy, z=cutter_cz,
            sx=cutter_size_x,
            sy=cutter_size_y,
            sz=cutter_size_z,
            material=None
        )
        # 繞 X 軸旋轉使底面形成 30° 斜面
        cutter.rotation_euler = (bevel_angle, 0, 0)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

        # 布爾差集
        _boolean_difference(finger, cutter, apply=True)

    print(f"[GOLD] ✅ 金手指完成: {count} 觸點 × {finger_width}mm, 30° 倒角")
    return finger_objects


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：雙排 SMT 排母連接器（含鷗翼引腳 + 焊錫爬升 Fillet）
# ══════════════════════════════════════════════════════════════════════════════

def create_smt_connector(cx, cy, name="J1_SMT_Header"):
    """
    建立一個完整的雙排 40-Pin SMT 表面貼裝排母連接器。

    結構（從上到下）：
        ┌──────────────────────────────────────┐
        │  黑色 LCP 塑料本體                   │  頂部
        │  ┌────┬────┬────┬ ... ┬────┬────┐  │
        │  │ ║  │ ║  │ ║  │ ... │ ║  │ ║  │  │  雙排插槽開口
        │  └────┴────┴────┴ ... ┴────┴────┘  │
        ├──────────────────────────────────────┤
        │  ╲  ╱  ╲  ╱  ╲  ╱  ...  ╲  ╱    │  鷗翼型引腳 (Gull-wing)
        │   ╲╱    ╲╱    ╲╱   ...   ╲╱      │  從本體兩側向外延伸
        ├───┬──────┬──────┬──────┬── ... ────┤
        │ ⊿ │  ⊿   │  ⊿   │  ⊿   │  ...     │  焊錫爬升 Fillet
        │═══════════════════════════════════│  ← PCB 頂面
        └──────────────────────────────────────┘

    規格：
        - 排針數: 雙排 × 20 = 40 針
        - 間距: 2.54mm（標準 0.1" pitch）
        - 排距: 2.54mm（雙排之間的距離）
        - 本體: 黑色 LCP 塑料，53.34mm × 5.08mm × 5.0mm
        - 鷗翼引腳: 從本體兩側向外，向下彎曲到 PCB 面
        - 焊錫 fillet: 引腳與焊盤交界處的梯形焊接過渡
    """
    print(f"\n[CONN] 建立 SMT 排母連接器 @ ({cx:.1f}, {cy:.1f})")

    # --- 材料 ---
    # LCP 塑料本體（黑色、啞光）
    lcp_mat = _get_or_create_material(
        "PCB_LCP_Plastic", (0.05, 0.05, 0.06), 0.0, 0.50, ior=1.65,
        alpha=0.98
    )

    # 金屬端子（磷青銅鍍金，用於插槽內的彈片和鷗翼引腳）
    terminal_mat = _get_or_create_material(
        "PCB_Terminal_Gold", (0.85, 0.70, 0.28), 1.0, 0.10, ior=0.47
    )

    # 焊錫 fillet 材質（與步驟 2 的焊錫球材質相容）
    solder_fillet_mat = _get_or_create_material(
        "PCB_SolderFillet", (0.72, 0.71, 0.68), 1.0, 0.18, ior=1.9
    )

    # 銅焊盤材質
    copper_mat = _get_or_create_material(
        "PCB_Copper", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18
    )

    # --- 幾何參數 ---
    pin_count = 20            # 每排針數
    pin_pitch = 2.54          # 針間距 mm（0.1"）
    row_spacing = 2.54        # 雙排間距 mm
    total_length = (pin_count - 1) * pin_pitch  # 針腳總跨度

    # 塑料本體
    body_length = total_length + 4.0     # 比針腳跨度各多 2mm
    body_width = row_spacing + 3.0       # 排距 + 兩側壁厚
    body_height = 5.0                    # 本體高度

    # 鷗翼引腳幾何
    pin_width = 0.5           # 引腳寬度（扁平部分）
    pin_thickness = 0.25      # 引腳厚度
    gull_wing_span = 2.0      # 鷗翼從本體邊緣水平延伸的距離
    gull_wing_drop = 1.0      # 鷗翼向下垂直段的高度
    gull_wing_foot = 1.5      # 鷗翼腳部（平行於 PCB 面的焊盤接觸段）長度

    # --- 2.1 建立塑料本體 ---
    body_z_bottom = BOARD_TOP_Z + gull_wing_drop + pin_thickness + ZF_OFFSET
    body_z_top = body_z_bottom + body_height
    body_z_center = (body_z_bottom + body_z_top) / 2.0

    body = _cube(
        name=f"{name}_Body",
        x=cx, y=cy, z=body_z_center,
        sx=body_length, sy=body_width, sz=body_height,
        material=lcp_mat
    )

    print(f"  [CONN] 塑料本體: {body_length:.1f}×{body_width:.1f}×{body_height:.1f}mm LCP")

    # --- 2.2 建立雙排插槽開口 ---
    # 兩排長條形凹槽，在本體頂面沿 X 軸方向
    slot_width = 0.7          # 插槽開口寬度
    slot_length = total_length + 1.0  # 插槽長度（略長於針腳跨度）
    slot_depth = 3.5          # 插槽深度

    # Row A（前排，Y+ 方向）
    slot_a_y = cy + row_spacing / 2.0
    # Row B（後排，Y- 方向）
    slot_b_y = cy - row_spacing / 2.0

    slot_z_bottom = body_z_top - slot_depth
    slot_z_top = body_z_top + 0.1
    slot_z_center = (slot_z_bottom + slot_z_top) / 2.0

    slot_a = _cube(
        name=f"{name}_SlotA_Cutter",
        x=cx, y=slot_a_y, z=slot_z_center,
        sx=slot_length, sy=slot_width, sz=slot_depth + 0.1,
        material=None
    )
    _boolean_difference(body, slot_a, apply=True)

    slot_b = _cube(
        name=f"{name}_SlotB_Cutter",
        x=cx, y=slot_b_y, z=slot_z_center,
        sx=slot_length, sy=slot_width, sz=slot_depth + 0.1,
        material=None
    )
    _boolean_difference(body, slot_b, apply=True)

    print(f"  [CONN] 雙排插槽: 2 × {slot_length:.1f}×{slot_width:.1f}×{slot_depth:.1f}mm")

    # --- 2.3 建立插槽內的金屬彈片（簡化表示） ---
    # 每個插槽位置有一對金屬彈片（接觸片），簡化為薄金屬矩形片
    contact_height = 2.5
    contact_width = 0.4
    contact_thickness = 0.15

    contact_objects = []

    for row_idx, slot_y in enumerate([slot_a_y, slot_b_y]):
        row_label = "A" if row_idx == 0 else "B"
        for pin_idx in range(pin_count):
            px = cx - total_length / 2.0 + pin_idx * pin_pitch

            # 每個 pin 位置有兩片金屬接觸片（左右對稱）
            for side, sign in [("L", -1), ("R", 1)]:
                contact = _cube(
                    name=f"{name}_Contact_{row_label}{pin_idx+1}{side}",
                    x=px,
                    y=slot_y + sign * (slot_width / 2.0 - contact_width / 2.0),
                    z=slot_z_bottom + contact_height / 2.0,
                    sx=contact_thickness,
                    sy=contact_width,
                    sz=contact_height,
                    material=terminal_mat
                )
                contact_objects.append(contact)

    print(f"  [CONN] 內部金屬彈片: {len(contact_objects)} 片")

    # --- 2.4 建立鷗翼型引腳 (Gull-Wing Leads) ---
    # 每個引腳從塑料本體內部 → 側面伸出 → 向下彎曲 → 水平接觸 PCB 焊盤
    #
    # 路徑（側視圖 YZ 平面）：
    #   本體內部 (Z 高) → 水平伸出 (Y 方向) → 向下彎曲 (Z 方向) → 水平腳部 (Y 方向, 在 PCB 面上)
    #
    #     本體側面
    #     │
    #     ├── 水平延伸段 (shoulder) ──┐
    #     │                          │  ← 彎曲半徑
    #     │                          ├── 垂直下降段 (drop)
    #     │                          │
    #     │                          ├── 彎曲半徑
    #     │                          │
    #     │                          └── 水平腳部 (foot, 在焊盤上)
    #                                  ═══ PCB 頂面

    pin_shoulder_length = 1.2   # 從本體邊緣水平伸出的距離
    pin_drop_height = gull_wing_drop      # 垂直下降高度
    pin_foot_length = gull_wing_foot      # 腳部水平長度（接觸焊盤）

    gull_wing_objects = []
    fillet_objects = []

    for row_idx in range(2):
        row_label = "A" if row_idx == 0 else "B"
        # 本體邊緣 Y 座標
        if row_idx == 0:  # Row A: 前排，引腳向 Y+ 方向伸出
            body_edge_y = cy + body_width / 2.0
            pin_direction = 1  # 向外（Y+）
        else:  # Row B: 後排，引腳向 Y- 方向伸出
            body_edge_y = cy - body_width / 2.0
            pin_direction = -1  # 向外（Y-）

        for pin_idx in range(pin_count):
            px = cx - total_length / 2.0 + pin_idx * pin_pitch

            # --- 鷗翼引腳的 4 個幾何段 ---
            # 我們用一系列細長立方體拼接成鷗翼形狀

            # 段 1：水平肩部（從本體邊緣伸出）
            shoulder_y_center = body_edge_y + pin_direction * pin_shoulder_length / 2.0
            shoulder_z = body_z_bottom - 1.0  # 引腳從本體下部伸出

            shoulder = _cube(
                name=f"{name}_Shoulder_{row_label}{pin_idx+1}",
                x=px,
                y=shoulder_y_center,
                z=shoulder_z,
                sx=pin_width,
                sy=pin_shoulder_length,
                sz=pin_thickness,
                material=terminal_mat
            )
            gull_wing_objects.append(shoulder)

            # 段 2：垂直下降段
            drop_y = body_edge_y + pin_direction * pin_shoulder_length
            drop_z_center = shoulder_z - pin_drop_height / 2.0

            drop = _cube(
                name=f"{name}_Drop_{row_label}{pin_idx+1}",
                x=px,
                y=drop_y,
                z=drop_z_center,
                sx=pin_width,
                sy=pin_thickness,
                sz=pin_drop_height,
                material=terminal_mat
            )
            gull_wing_objects.append(drop)

            # 段 3：水平腳部（接觸 PCB 焊盤）
            foot_y_center = drop_y + pin_direction * pin_foot_length / 2.0
            foot_z = BOARD_TOP_Z + CU_THICKNESS / 2.0 + ZF_OFFSET

            foot = _cube(
                name=f"{name}_Foot_{row_label}{pin_idx+1}",
                x=px,
                y=foot_y_center,
                z=foot_z,
                sx=pin_width,
                sy=pin_foot_length,
                sz=CU_THICKNESS,
                material=terminal_mat
            )
            gull_wing_objects.append(foot)

            # --- 2.5 焊錫爬升 Fillet（梯形過渡幾何體） ---
            # Solder fillet 模擬焊錫從焊盤沿引腳邊緣向上爬升的效果
            # 形狀：底部較寬（在焊盤上），向上逐漸變窄，形成梯形斜面
            #
            #    引腳
            #     │
            #    ╱│╲   ← 焊錫 fillet（梯形截面，焊錫沿引腳向上爬升）
            #   ╱ │ ╲
            #  ╱  │  ╲
            # ════╧════  ← PCB 焊盤
            #
            # 我們用一個從 4 面收窄的梯形來近似

            fillet_bottom_width = pin_width + 0.6    # 底部比引腳寬
            fillet_bottom_length = pin_foot_length + 0.4
            fillet_top_width = pin_width + 0.1       # 頂部接近引腳寬度
            fillet_top_length = pin_thickness + 0.2
            fillet_height = 0.3                       # 爬升高度

            # 使用自定義頂點建立梯形（截錐體）
            # 8 個頂點：底部 4 個 + 頂部 4 個
            fillet_z_bottom = foot_z + CU_THICKNESS / 2.0
            fillet_z_top = fillet_z_bottom + fillet_height

            hbw = fillet_bottom_width / 2.0   # half bottom width
            hbl = fillet_bottom_length / 2.0
            htw = fillet_top_width / 2.0
            htl = fillet_top_length / 2.0

            fillet_verts = [
                # 底部 4 頂點（在焊盤面上）
                (px - hbw, foot_y_center - hbl, fillet_z_bottom),  # 0: 底左前
                (px + hbw, foot_y_center - hbl, fillet_z_bottom),  # 1: 底右前
                (px + hbw, foot_y_center + hbl, fillet_z_bottom),  # 2: 底右後
                (px - hbw, foot_y_center + hbl, fillet_z_bottom),  # 3: 底左後
                # 頂部 4 頂點（收窄，包圍引腳）
                (px - htw, foot_y_center - htl, fillet_z_top),     # 4: 頂左前
                (px + htw, foot_y_center - htl, fillet_z_top),     # 5: 頂右前
                (px + htw, foot_y_center + htl, fillet_z_top),     # 6: 頂右後
                (px - htw, foot_y_center + htl, fillet_z_top),     # 7: 頂左後
            ]

            fillet_faces = [
                (0, 1, 2, 3),  # 底面
                (4, 7, 6, 5),  # 頂面
                (0, 4, 5, 1),  # 前面
                (1, 5, 6, 2),  # 右面
                (2, 6, 7, 3),  # 後面
                (3, 7, 4, 0),  # 左面
            ]

            fillet_obj = _make_mesh_from_data(
                obj_name=f"{name}_Fillet_{row_label}{pin_idx+1}",
                verts=fillet_verts,
                faces=fillet_faces,
                material=solder_fillet_mat
            )
            fillet_objects.append(fillet_obj)

    print(f"  [CONN] 鷗翼引腳: {len(gull_wing_objects)} 個幾何段")
    print(f"  [CONN] 焊錫 fillet: {len(fillet_objects)} 個")

    # --- 2.6 Pin 1 標記（絲印三角形） ---
    # 在塑料本體頂面，靠近 Pin 1 的位置放置一個小三角形標記
    pin1_mark_x = cx - total_length / 2.0 - 1.5
    pin1_mark_y = cy + body_width / 2.0 + 0.5
    pin1_mark_z = body_z_top + SILKSCREEN_THICKNESS / 2.0 + ZF_OFFSET

    pin1_tri = _cube(
        name=f"{name}_Pin1Mark",
        x=pin1_mark_x, y=pin1_mark_y, z=pin1_mark_z,
        sx=0.8, sy=0.8, sz=SILKSCREEN_THICKNESS,
        material=None
    )
    # 旋轉 45° 使其看起來像三角形
    pin1_tri.rotation_euler = (0, 0, math.radians(45))
    bpy.ops.object.transform_apply(rotation=True)

    # 賦予白色絲印材質
    silk_mat = _get_or_create_material(
        "PCB_Silkscreen", (0.95, 0.95, 0.95), 0.0, 0.60
    )
    pin1_tri.data.materials.append(silk_mat)

    print(f"[CONN] ✅ {name} 完成")
    print(f"  - 40-Pin 雙排 SMT, 間距 2.54mm")
    print(f"  - 鷗翼引腳 + 焊錫 fillet × 40")
    return {
        'body': body,
        'contacts': contact_objects,
        'gull_wings': gull_wing_objects,
        'fillets': fillet_objects,
        'pin1_mark': pin1_tri,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 3 部分：散熱片（陽極氧化鋁）
# ══════════════════════════════════════════════════════════════════════════════

def create_heatsink(cx, cy, name="HS1_Heatsink"):
    """
    建立一個鋁擠型散熱片，包含厚實基座 + 平行鰭片陣列。

    規格：
        - 整體: 20×20×8mm（寬×長×高）
        - 基座: 20×20×2mm 實心鋁板
        - 鰭片: 8 片, 每片厚 0.5mm, 高 6mm, 長 20mm
        - 材質: 陽極氧化鋁（藍色）, 啞光金屬質感

    結構（側視圖 YZ 平面）：
        ┌──┬──┬──┬──┬──┬──┬──┬──┐  ← 鰭片（0.5mm 厚, 6mm 高）
        │  │  │  │  │  │  │  │  │     8 片平行排列
        ├──┴──┴──┴──┴──┴──┴──┴──┤
        │                        │  ← 基座（2mm 厚實心底板）
        │    陽極氧化鋁基座      │
        └────────────────────────┘
          ═══════════════════════    ← PCB 頂面

    鰭片間距計算：
        總寬度 = 20mm, 8 片, 每片 0.5mm
        鰭片總佔用 = 8 × 0.5 = 4mm
        剩餘空間 = 20 - 4 = 16mm
        間隔數 = 8 - 1 = 7
        間距 = 16 / 7 ≈ 2.286mm
    """
    print(f"\n[HEATSINK] 建立散熱片 @ ({cx:.1f}, {cy:.1f})")

    # --- 材料：陽極氧化鋁（藍色） ---
    anodized_al_mat = _get_or_create_material(
        "PCB_AnodizedAluminum", (0.15, 0.25, 0.45), 0.85, 0.35, ior=1.35
    )
    # 覆寫：陽極氧化鋁的顏色略帶金屬藍
    # 完整材質設置在取得後再微調
    mat = bpy.data.materials.get("PCB_AnodizedAluminum")
    if mat and mat.use_nodes:
        bsdf = mat.node_tree.nodes.get('Principled BSDF')
        if bsdf:
            bsdf.inputs['Base Color'].default_value = (0.12, 0.28, 0.52, 1.0)  # 陽極藍
            bsdf.inputs['Metallic'].default_value = 0.7    # 陽極氧化後金屬度降低
            bsdf.inputs['Roughness'].default_value = 0.35  # 啞光
            bsdf.inputs['IOR'].default_value = 1.35

    # --- 幾何參數 ---
    hs_width = 20.0       # X 軸（鰭片長度方向）
    hs_length = 20.0      # Y 軸
    hs_total_height = 8.0 # Z 軸總高
    base_thickness = 2.0  # 基座厚度

    fin_count = 8
    fin_thickness = 0.5   # 每片鰭片厚度
    fin_height = hs_total_height - base_thickness  # 6mm
    fin_length = hs_width  # 與散熱片同寬（20mm）

    # 計算鰭片間距
    total_fin_width = fin_count * fin_thickness
    remaining_space = hs_length - total_fin_width
    gap_count = fin_count - 1
    fin_spacing = remaining_space / gap_count  # ≈ 2.286mm

    # Z 軸座標
    hs_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET  # 在銅箔上方
    base_z_top = hs_z_bottom + base_thickness
    base_z_center = hs_z_bottom + base_thickness / 2.0
    fin_z_bottom = base_z_top
    fin_z_top = fin_z_bottom + fin_height
    fin_z_center = fin_z_bottom + fin_height / 2.0

    # --- 3.1 建立基座 ---
    base = _cube(
        name=f"{name}_Base",
        x=cx, y=cy, z=base_z_center,
        sx=hs_width, sy=hs_length, sz=base_thickness,
        material=anodized_al_mat
    )
    print(f"  [HS] 基座: {hs_width}×{hs_length}×{base_thickness}mm")

    # --- 3.2 建立鰭片陣列 ---
    fin_objects = []

    # 鰭片沿 Y 軸排列（平行於 XZ 平面）
    # Y 起始位置：從散熱片前邊緣開始
    fin_start_y = cy - hs_length / 2.0 + fin_thickness / 2.0

    for i in range(fin_count):
        fy = fin_start_y + i * (fin_thickness + fin_spacing)

        fin = _cube(
            name=f"{name}_Fin_{i+1}",
            x=cx, y=fy, z=fin_z_center,
            sx=fin_length, sy=fin_thickness, sz=fin_height,
            material=anodized_al_mat
        )
        fin_objects.append(fin)

    print(f"  [HS] 鰭片: {fin_count} 片 × {fin_thickness}mm 厚, 間距 {fin_spacing:.2f}mm, 高 {fin_height}mm")

    # --- 3.3 基座底部輕微倒角（與 PCB 接觸的邊緣） ---
    # 使用 Bevel modifier（與 BGA 倒角類似）
    bpy.context.view_layer.objects.active = base
    base.select_set(True)
    mod = base.modifiers.new(name="BaseChamfer", type='BEVEL')
    mod.width = 0.15  # 輕微倒角
    mod.segments = 2
    mod.limit_method = 'ANGLE'
    mod.angle_limit = 1.39626  # ~80° 只倒銳邊
    bpy.ops.object.modifier_apply(modifier=mod.name)

    print(f"[HEATSINK] ✅ {name} 完成")
    print(f"  - 總高: {hs_total_height}mm")
    print(f"  - 散熱面積: ~{fin_count * fin_height * fin_length * 2:.0f}mm²（鰭片雙面）")
    return {
        'base': base,
        'fins': fin_objects,
        'top_z': fin_z_top,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 4 部分：大型一體成型功率電感
# ══════════════════════════════════════════════════════════════════════════════

def create_power_inductor(cx, cy, name="L1_PowerInductor"):
    """
    建立一個全封閉式大功率一體成型電感（Molded Power Inductor）。

    結構：
        ┌──────────────────────────┐
        │  ┌────────────────────┐  │  ← U 型金屬端子（鍍錫, 頂部）
        │  │                    │  │
        │  │  ┌──────────────┐  │  │
        │  │  │  鐵氧體本體  │  │  │  ← 暗灰色顆粒質感
        │  │  │  (Ferrite)   │  │  │     12×12×6mm
        │  │  │              │  │  │
        │  │  └──────────────┘  │  │
        │  │                    │  │
        │  └────────────────────┘  │  ← U 型金屬端子（底部, 焊接面）
        └──────────────────────────┘
          ═══════════════════════════    ← PCB 頂面

    規格（參考 Vishay IHLP-6767 或類似封裝）：
        - 本體: 12×12×6mm（長×寬×高）
        - 端子: U 型金屬片，包裹本體兩端
        - 材質: 鐵氧體複合材料（暗灰, 微顆粒感）, 鍍錫銅端子
    """
    print(f"\n[INDUCTOR] 建立功率電感 @ ({cx:.1f}, {cy:.1f})")

    # --- 材料 ---
    # 鐵氧體複合材料（暗灰、啞光、微顆粒質感）
    ferrite_mat = bpy.data.materials.new(name="PCB_Ferrite")
    ferrite_mat.use_nodes = True
    nodes = ferrite_mat.node_tree.nodes
    links = ferrite_mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.18, 0.18, 0.19, 1.0)  # 暗灰
    bsdf.inputs['Metallic'].default_value = 0.05       # 極低金屬度（鐵氧體是陶瓷）
    bsdf.inputs['Roughness'].default_value = 0.65      # 粗糙顆粒感
    bsdf.inputs['IOR'].default_value = 1.8             # 鐵氧體折射率

    # 顆粒質感：高頻噪波 → Bump + 輕微顏色變化
    noise_grain = nodes.new(type='ShaderNodeTexNoise')
    noise_grain.location = (-300, -150)
    noise_grain.inputs['Scale'].default_value = 400.0     # 高頻 → 細顆粒
    noise_grain.inputs['Detail'].default_value = 10.0
    noise_grain.inputs['Roughness'].default_value = 0.55

    bump = nodes.new(type='ShaderNodeBump')
    bump.location = (0, -150)
    bump.inputs['Strength'].default_value = 0.06
    bump.inputs['Distance'].default_value = 0.003
    links.new(noise_grain.outputs['Fac'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    # 顆粒引起的輕微明暗變化：將噪波映射為顏色變化
    color_ramp = nodes.new(type='ShaderNodeValToRGB')
    color_ramp.location = (-100, -300)
    color_ramp.color_ramp.elements[0].color = (0.15, 0.15, 0.16, 1.0)  # 暗區
    color_ramp.color_ramp.elements[1].color = (0.22, 0.22, 0.23, 1.0)  # 亮區
    links.new(noise_grain.outputs['Fac'], color_ramp.inputs['Fac'])

    # 將顆粒顏色變化直接連接到 Base Color
    # （Blender 4.5 的 Mix 節點：用 color_ramp 的顏色微微混合基準色）
    mix_color = nodes.new(type='ShaderNodeMix')
    mix_color.location = (0, -350)
    mix_color.data_type = 'RGBA'
    mix_color.blend_type = 'MIX'
    mix_color.inputs['Factor'].default_value = 0.3
    # A = 基準暗灰色
    mix_color.inputs['A'].default_value = (0.18, 0.18, 0.19, 1.0)
    links.new(color_ramp.outputs['Color'], mix_color.inputs['B'])
    links.new(mix_color.outputs['Result'], bsdf.inputs['Base Color'])

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (600, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    print("  [MAT] 鐵氧體顆粒材質建立完成")

    # 端子材質（厚實鍍錫）
    terminal_mat = _get_or_create_material(
        "PCB_InductorTerminal", (0.74, 0.73, 0.70), 0.95, 0.22, ior=1.8
    )

    # --- 幾何參數 ---
    body_size = 12.0        # 本體長寬 mm
    body_height = 6.0       # 本體高度 mm
    terminal_width = 13.0   # 端子總寬度（比本體略寬, 含彎折）
    terminal_thickness = 0.8  # 金屬端子厚度 mm
    terminal_height = body_height + 1.0  # 端子高度（從頂面到焊盤）
    terminal_depth = 3.5    # 端子沿 X 方向的深度（U 型包裹的深度）

    # Z 軸座標
    body_z_bottom = BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET
    body_z_top = body_z_bottom + body_height
    body_z_center = (body_z_bottom + body_z_top) / 2.0

    # 端子 Z 範圍（比本體頂部略高，底部與焊盤平齊）
    term_z_bottom = BOARD_TOP_Z + CU_THICKNESS / 2.0 + ZF_OFFSET
    term_z_top = body_z_top + 0.3  # 端子頂部略高於本體
    term_z_center = (term_z_bottom + term_z_top) / 2.0

    # --- 4.1 建立鐵氧體本體 ---
    body = _cube(
        name=f"{name}_FerriteBody",
        x=cx, y=cy, z=body_z_center,
        sx=body_size, sy=body_size, sz=body_height,
        material=ferrite_mat
    )

    # 輕微倒角（0.15mm，模擬模具圓角）
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    mod = body.modifiers.new(name="FerriteChamfer", type='BEVEL')
    mod.width = 0.15
    mod.segments = 2
    mod.limit_method = 'ANGLE'
    mod.angle_limit = 1.309  # 75°
    bpy.ops.object.modifier_apply(modifier=mod.name)

    print(f"  [IND] 鐵氧體本體: {body_size}×{body_size}×{body_height}mm")

    # --- 4.2 建立兩端 U 型金屬端子 ---
    # 端子位於本體的 X 軸兩端
    # U 型結構：頂面水平段 → 垂直段 → 底面水平段（焊接腳）
    #
    #   側視圖（XZ 平面）：
    #   ┌─────┐           ← 頂部水平段（覆蓋本體頂面邊緣）
    #   │     │
    #   │ 鐵  │  ← 本體
    #   │ 氧  │
    #   │ 體  │
    #   │     │
    #   └─────┘           ← 底部水平段（焊接到 PCB）
    #     ═══ PCB

    terminal_objects = []

    for end_idx, sign in enumerate([-1, 1]):  # 左端和右端
        end_label = "Left" if sign == -1 else "Right"
        # 端子 X 位置（在本體邊緣）
        term_x = cx + sign * (body_size / 2.0 - terminal_depth / 2.0)

        # --- 頂部水平段 ---
        top_seg = _cube(
            name=f"{name}_TermTop_{end_label}",
            x=term_x,
            y=cy,
            z=term_z_top - terminal_thickness / 2.0 + ZF_OFFSET,
            sx=terminal_depth,
            sy=terminal_width,
            sz=terminal_thickness,
            material=terminal_mat
        )
        terminal_objects.append(top_seg)

        # --- 垂直段（外側面） ---
        # 垂直段連接頂部和底部水平段
        vert_x = cx + sign * (body_size / 2.0 + terminal_thickness / 2.0)
        vert_seg = _cube(
            name=f"{name}_TermVert_{end_label}",
            x=vert_x + ZF_OFFSET * sign,  # ZF_OFFSET 防止穿透本體
            y=cy,
            z=term_z_center,
            sx=terminal_thickness,
            sy=terminal_width,
            sz=term_z_top - term_z_bottom,
            material=terminal_mat
        )
        terminal_objects.append(vert_seg)

        # --- 底部水平段（焊接腳，接觸 PCB 焊盤） ---
        bottom_seg = _cube(
            name=f"{name}_TermBottom_{end_label}",
            x=term_x,
            y=cy,
            z=term_z_bottom + CU_THICKNESS / 2.0 + ZF_OFFSET,
            sx=terminal_depth,
            sy=terminal_width,
            sz=CU_THICKNESS,
            material=terminal_mat
        )
        terminal_objects.append(bottom_seg)

        # --- 端子底部焊錫 fillet（端子與焊盤交界處） ---
        # 使用與排座相同的梯形 fillet
        fillet_bottom_w = terminal_width + 0.8
        fillet_top_w = terminal_width + 0.1
        fillet_bottom_d = terminal_depth + 0.6
        fillet_top_d = terminal_depth + 0.1
        fillet_h = 0.35  # fillet 爬升高度

        fillet_z_bot = term_z_bottom + CU_THICKNESS
        fillet_z_top = fillet_z_bot + fillet_h

        hbw = fillet_bottom_w / 2.0
        htw = fillet_top_w / 2.0
        hbd = fillet_bottom_d / 2.0
        htd = fillet_top_d / 2.0

        fillet_verts = [
            # 底面（在焊盤上）
            (term_x - hbd, cy - hbw, fillet_z_bot),
            (term_x + hbd, cy - hbw, fillet_z_bot),
            (term_x + hbd, cy + hbw, fillet_z_bot),
            (term_x - hbd, cy + hbw, fillet_z_bot),
            # 頂面（包圍端子底部）
            (term_x - htd, cy - htw, fillet_z_top),
            (term_x + htd, cy - htw, fillet_z_top),
            (term_x + htd, cy + htw, fillet_z_top),
            (term_x - htd, cy + htw, fillet_z_top),
        ]
        fillet_faces = [
            (0, 1, 2, 3), (4, 7, 6, 5),
            (0, 4, 5, 1), (1, 5, 6, 2),
            (2, 6, 7, 3), (3, 7, 4, 0),
        ]

        solder_mat = _get_or_create_material(
            "PCB_SolderFillet", (0.72, 0.71, 0.68), 1.0, 0.18, ior=1.9
        )

        fillet_obj = _make_mesh_from_data(
            obj_name=f"{name}_Fillet_{end_label}",
            verts=fillet_verts,
            faces=fillet_faces,
            material=solder_mat
        )
        terminal_objects.append(fillet_obj)

    print(f"  [IND] U 型端子: {len(terminal_objects)} 個幾何段")

    # --- 4.3 頂面標記（極性指示點） ---
    # 在電感頂面一角放置一個微小的半球形凹槽（標記 Pin 1 / 正極）
    dot_radius = 0.6
    dot_depth = 0.15
    dot_x = cx - body_size / 2.0 + 2.5
    dot_y = cy - body_size / 2.0 + 2.5

    dot_mark = _cylinder(
        name=f"{name}_PolarityDot",
        x=dot_x, y=dot_y,
        z_bottom=body_z_top - dot_depth,
        z_top=body_z_top + 0.05,
        radius=dot_radius,
        vertices=32,
        material=None
    )
    _boolean_difference(body, dot_mark, apply=True)

    print(f"[INDUCTOR] ✅ {name} 完成")
    print(f"  - 鐵氧體本體: {body_size}×{body_size}×{body_height}mm")
    print(f"  - U 型端子 ×2, 底部焊接 fillet")
    return {
        'body': body,
        'terminals': terminal_objects,
        'top_z': term_z_top,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：整合的 create_connectors 和 create_heavy_components
# ══════════════════════════════════════════════════════════════════════════════

def create_connectors():
    """
    在 PCB 上建立所有連接器元件。

    佈局：
        - 金手指：PCB 底邊（Y = -PCB_W/2），居中排列
        - SMT 排座：PCB 中央上方區域
    """
    print("\n" + "=" * 50)
    print("  建立連接器：金手指 + SMT 排座")
    print("=" * 50)

    # --- 金手指：PCB 底邊 ---
    # 30 個金手指，總跨度 = (30 - 1) × 1.3mm = 37.7mm
    finger_count = 30
    finger_pitch = 1.3
    finger_span = (finger_count - 1) * finger_pitch  # 37.7mm
    finger_start_x = -finger_span / 2.0              # 居中

    board_bottom_y = -PCB_W / 2.0  # PCB 底邊 Y = -45.0

    gold_fingers = create_gold_fingers(
        board_bottom_edge_y=board_bottom_y,
        start_x=finger_start_x,
        count=finger_count
    )

    # --- SMT 排座：PCB 中央上方 ---
    conn_cy = 25.0  # Y = 25mm（靠近 PCB 上方）
    conn_cx = 0.0   # X = 0（居中）

    smt_conn = create_smt_connector(conn_cx, conn_cy, name="J1_SMT_Header")

    print("\n[CONNECTORS] ✅ 全部連接器建立完成")
    return {
        'gold_fingers': gold_fingers,
        'smt_connector': smt_conn,
    }


def create_heavy_components():
    """
    在 PCB 上建立重型元件：散熱片 + 功率電感。

    佈局：
        - 散熱片：PCB 左下方（大型 IC 上方）
        - 功率電感：PCB 右上方（電源區）
    """
    print("\n" + "=" * 50)
    print("  建立重型元件：散熱片 + 功率電感")
    print("=" * 50)

    # --- 散熱片：放在 BGA 晶片附近（左下方） ---
    hs_x = -30.0   # 與 BGA 相同 X 位置
    hs_y = -25.0   # BGA 下方

    heatsink = create_heatsink(hs_x, hs_y, name="HS1_CPU_Heatsink")

    # --- 功率電感：電源區域（右上方） ---
    ind_x = 45.0
    ind_y = 25.0

    inductor = create_power_inductor(ind_x, ind_y, name="L1_PowerInductor")

    print("\n[HEAVY] ✅ 全部重型元件建立完成")
    return {
        'heatsink': heatsink,
        'inductor': inductor,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 獨立執行入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ATOS PRO — PCB 超逼真建模工具 v3.0")
    print("  步驟 3：金手指 + SMT 排座 + 散熱片 + 功率電感")
    print("=" * 60)
    print()
    print("⚠ 注意：請先執行步驟 1 建立 PCB 基板。")
    print("  如果已執行步驟 2, 元件會與 BGA/RF 共存。")
    print()

    # 檢查 PCB 基板
    pcb_exists = "PCB_Base_Body" in bpy.data.objects
    if not pcb_exists:
        print("[WARN] 未檢測到 PCB_Base_Body，將獨立建立元件（無基板）。")
    else:
        print("[INFO] PCB 基板已存在，元件將放置在正確高度。")

    # 執行
    connectors = create_connectors()
    heavy = create_heavy_components()

    # 統計
    total_objects = len(bpy.data.objects)
    total_vertices = sum(
        len(obj.data.vertices)
        for obj in bpy.data.objects
        if obj.type == 'MESH'
    )
    print("\n" + "=" * 60)
    print(f"  ✅ 步驟 3 完成！")
    print(f"  場景總物件數: {total_objects}")
    print(f"  場景總頂點數: {total_vertices:,}")
    print(f"  金手指: {len(connectors['gold_fingers'])} 觸點")
    print(f"  SMT 排座: 40-Pin 雙排 + 鷗翼引腳 + Fillet")
    print(f"  散熱片: 8 鰭片 陽極氧化鋁")
    print(f"  功率電感: 鐵氧體 + U 型端子")
    print("=" * 60)
    print()
    print("📌 步驟 3 完成。等待下一步指令...")
