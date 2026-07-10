"""
ATOS_PRO/tools/pcb_modeler_step4.py
Blender 4.2+ / Python 3.11 — PCB 超逼真建模脚本
步驟 4：全板阻焊層 + 絲印層 + 銅箔層 + 最終渲染輸出

與步驟 1/2/3 全相容。執行前請先依序執行前 3 步驟。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import math
import os

# ══════════════════════════════════════════════════════════════════════════════
# 全域參數（與步驟 1/2/3 一致）
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
# 輔助工具
# ══════════════════════════════════════════════════════════════════════════════

def _cube(name, x, y, z, sx, sy, sz, material=None):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, y, z))
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (sx, sy, sz)
    bpy.ops.object.transform_apply(scale=True)
    if material:
        obj.data.materials.append(material)
    return obj


def _cylinder(name, x, y, z_bottom, z_top, radius, vertices=64, material=None):
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


def _get_or_create_material(name, color, metallic, roughness, ior=1.5,
                             transmission=0.0, alpha=1.0):
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
    return mat


def _boolean_difference(target, cutter, apply=True):
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


def _get_pcb_body():
    """尋找場景中的 PCB 基板主體。"""
    if "PCB_Base_Body" in bpy.data.objects:
        return bpy.data.objects["PCB_Base_Body"]
    # 嘗試其他可能的命名
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and 'PCB' in obj.name and 'Body' in obj.name:
            return obj
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：全板阻焊層（Solder Mask）
# ══════════════════════════════════════════════════════════════════════════════

def create_solder_mask_layer():
    """
    在 PCB 頂面和底面建立完整的阻焊層。

    阻焊層（綠油）是 PCB 最上層的保護塗層，覆蓋整板，
    但在以下位置需要"開窗"（去除阻焊，露出銅面）：
        1. 所有焊盤（SMD pads）
        2. 過孔焊環
        3. 金手指區域
        4. 天線區域
        5. 安裝孔周圍焊盤
        6. 測試點

    實現方法：
        建立兩片完整覆蓋 PCB 的薄板（頂面 + 底面），
        然後用布爾運算在需要開窗的位置挖掉阻焊層。

    注意：由於完整開窗需要與前面步驟的所有元件配合，
    這裡採用「基於命名規則自動偵測需要開窗的物件」的策略。
    """
    print("\n" + "=" * 50)
    print("  建立全板阻焊層（Solder Mask）")
    print("=" * 50)

    # --- 材質 ---
    mask_mat = _get_or_create_material(
        "PCB_SolderMask", (0.08, 0.20, 0.08), 0.0, 0.45
    )

    # --- 建立頂面阻焊層 ---
    # 一片與 PCB 同大小的薄板，厚度 SOLDERMASK_THICKNESS
    # 用圓角矩形近似（這裡用長方體 + 後續布爾處理）
    mask_top_z = BOARD_TOP_Z + CU_THICKNESS + SOLDERMASK_THICKNESS / 2.0 + ZF_OFFSET

    mask_top = _cube(
        name="SolderMask_Top",
        x=0, y=0, z=mask_top_z,
        sx=PCB_L - 0.1,  # 略小於板面（避免邊緣溢出）- 先做完整覆蓋
        sy=PCB_W - 0.1,
        sz=SOLDERMASK_THICKNESS,
        material=mask_mat
    )
    print(f"  [MASK] 頂面阻焊層: {PCB_L}×{PCB_W}×{SOLDERMASK_THICKNESS}mm")

    # --- 建立底面阻焊層 ---
    mask_bottom_z = BOARD_BOTTOM_Z - CU_THICKNESS - SOLDERMASK_THICKNESS / 2.0 - ZF_OFFSET

    mask_bottom = _cube(
        name="SolderMask_Bottom",
        x=0, y=0, z=mask_bottom_z,
        sx=PCB_L - 0.1,
        sy=PCB_W - 0.1,
        sz=SOLDERMASK_THICKNESS,
        material=mask_mat
    )
    print(f"  [MASK] 底面阻焊層: {PCB_L}×{PCB_W}×{SOLDERMASK_THICKNESS}mm")

    # --- 自動偵測需要開窗的物件並執行布爾差集 ---
    # 搜尋包含以下關鍵字的物件名稱：
    #   "Pad_"  → 焊盤
    #   "Via_Outer_"  → 過孔外環
    #   "GoldFinger_" → 金手指
    #   "Foot_"  → 鷗翼引腳腳部
    #   "TermBottom_" → 電感端子底部
    #   "Antenna" → 天線相關

    opening_keywords = [
        "Pad_", "Via_Outer_", "GoldFinger_", "Foot_",
        "TermBottom_", "FeedPad", "IFA_",
        "PTH_Copper_", "Pad_Top_", "Pad_Bottom_",
    ]

    opening_objects = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for kw in opening_keywords:
            if kw in obj.name:
                opening_objects.append(obj)
                break

    # 去重
    opening_objects = list({obj.name: obj for obj in opening_objects}.values())

    print(f"  [MASK] 偵測到 {len(opening_objects)} 個需要開窗的物件")

    # 對頂面阻焊層做開窗（僅處理位於頂面的元件）
    top_openings_done = 0
    for obj in opening_objects:
        # 檢查物件是否在頂面（Z > 0）
        z_loc = obj.location.z
        if z_loc > 0 and z_loc < 20:  # 合理的元件高度範圍
            # 建立一個略大的切割體（防止邊界重合導致的布爾失敗）
            cutter = _cube(
                name=f"MaskOpening_Top_{obj.name}",
                x=obj.location.x,
                y=obj.location.y,
                z=mask_top_z,
                sx=obj.dimensions.x + 0.05,
                sy=obj.dimensions.y + 0.05,
                sz=SOLDERMASK_THICKNESS + 0.1,
                material=None
            )
            _boolean_difference(mask_top, cutter, apply=True)
            top_openings_done += 1

    # 對底面阻焊層做開窗
    bottom_openings_done = 0
    for obj in opening_objects:
        z_loc = obj.location.z
        if z_loc < 0 and z_loc > -20:
            cutter = _cube(
                name=f"MaskOpening_Bot_{obj.name}",
                x=obj.location.x,
                y=obj.location.y,
                z=mask_bottom_z,
                sx=obj.dimensions.x + 0.05,
                sy=obj.dimensions.y + 0.05,
                sz=SOLDERMASK_THICKNESS + 0.1,
                material=None
            )
            _boolean_difference(mask_bottom, cutter, apply=True)
            bottom_openings_done += 1

    print(f"  [MASK] 頂面開窗: {top_openings_done} 處, 底面開窗: {bottom_openings_done} 處")
    print(f"[MASK] ✅ 全板阻焊層建立完成\n")

    return {
        'top': mask_top,
        'bottom': mask_bottom,
        'openings': len(opening_objects),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：絲印層（Silkscreen — 文字與標記）
# ══════════════════════════════════════════════════════════════════════════════

def create_silkscreen_layer():
    """
    在 PCB 頂面建立絲印層（白色文字和標記）。

    絲印層包含：
        1. PCB 版本號和型號
        2. 元件邊框標記（Component Outlines）
        3. Pin 1 指示器
        4. 極性標記
        5. 公司標識 / 認證標記（UL, CE, RoHS）
        6. 板號和日期碼

    在 Blender 中，我們用 Text 物件（轉換為 Mesh）來產生絲印文字，
    因為 Blender Text 可以擠出立體厚度。
    """
    print("\n" + "=" * 50)
    print("  建立絲印層（Silkscreen）")
    print("=" * 50)

    silk_mat = _get_or_create_material(
        "PCB_Silkscreen", (0.95, 0.95, 0.95), 0.0, 0.55
    )

    silk_z = BOARD_TOP_Z + CU_THICKNESS + SOLDERMASK_THICKNESS + ZF_OFFSET
    silk_thickness = SILKSCREEN_THICKNESS

    silk_objects = []

    # --- 2.1 輔助函數：建立絲印文字 ---
    def create_silk_text(text, x, y, size=2.0, rotation=0.0, name_prefix="Silk"):
        """
        建立絲印文字並轉換為網格。

        步驟：
            1. 建立 Blender Text 物件
            2. 擠出厚度
            3. 轉換為 Mesh
            4. 指定材質
        """
        # 建立文字物件
        bpy.ops.object.text_add(location=(x, y, silk_z + silk_thickness / 2.0))
        text_obj = bpy.context.active_object
        text_obj.name = f"{name_prefix}_{text.replace(' ', '_')[:20]}"

        # 設定文字內容和字體
        text_data = text_obj.data
        text_data.body = text
        text_data.size = size
        text_data.extrude = silk_thickness
        text_data.align_x = 'CENTER'
        text_data.align_y = 'CENTER'

        # 字體設定（使用 Blender 內建字體或系統字體）
        # Blender 的 Bfont 是內建等寬字體，適合技術標記
        text_data.font = None  # 使用內建 Bfont

        # 旋轉
        text_obj.rotation_euler = (0, 0, math.radians(rotation))

        # 轉換為 Mesh（以便指定材質和進行後續處理）
        bpy.context.view_layer.objects.active = text_obj
        bpy.ops.object.convert(target='MESH')

        text_obj.data.materials.append(silk_mat)
        silk_objects.append(text_obj)
        return text_obj

    # --- 2.2 建立絲印標記線（用於元件外框） ---
    def create_silk_outline_rect(cx, cy, sx, sy, line_width=0.15, name="SilkRect"):
        """
        用四個細長方體畫一個矩形邊框（模擬絲印元件外框線）。
        線寬約 0.15mm（標準絲印最小線寬）。
        """
        w = line_width
        rect_parts = []

        # 上邊
        top = _cube(f"{name}_Top", cx, cy + sy/2 - w/2, silk_z + silk_thickness/2,
                     sx, w, silk_thickness, silk_mat)
        rect_parts.append(top)
        # 下邊
        bot = _cube(f"{name}_Bot", cx, cy - sy/2 + w/2, silk_z + silk_thickness/2,
                     sx, w, silk_thickness, silk_mat)
        rect_parts.append(bot)
        # 左邊
        left = _cube(f"{name}_Left", cx - sx/2 + w/2, cy, silk_z + silk_thickness/2,
                      w, sy, silk_thickness, silk_mat)
        rect_parts.append(left)
        # 右邊
        right = _cube(f"{name}_Right", cx + sx/2 - w/2, cy, silk_z + silk_thickness/2,
                       w, sy, silk_thickness, silk_mat)
        rect_parts.append(right)

        silk_objects.extend(rect_parts)
        return rect_parts

    def create_silk_circle(cx, cy, radius, line_width=0.15, name="SilkCircle", segments=48):
        """
        用圓環近似絲印圓形標記。
        建立一個環形（ring），外徑 = radius + line_width/2，內徑 = radius - line_width/2。
        """
        outer_r = radius + line_width / 2.0
        inner_r = radius - line_width / 2.0

        # 建立外圓
        outer = _cylinder(f"{name}_Outer", cx, cy,
                          silk_z, silk_z + silk_thickness,
                          outer_r, segments, silk_mat)
        # 建立內圓切割體
        inner = _cylinder(f"{name}_InnerCutter", cx, cy,
                          silk_z - 0.01, silk_z + silk_thickness + 0.01,
                          inner_r, segments, None)
        _boolean_difference(outer, inner, apply=True)
        silk_objects.append(outer)
        return outer

    # --- 2.3 放置絲印內容 ---

    # PCB 型號（中央偏下）
    create_silk_text("ATOS-PRO Rev 2.1", 0, -PCB_W/2 + 8.0, size=2.5)

    # 日期碼（型號下方）
    create_silk_text("2026-06-28  FAB:JLC  L6", 0, -PCB_W/2 + 5.0, size=1.5)

    # 公司標識（左上角）
    create_silk_text("ATOS ENGINEERING", -PCB_L/2 + 20.0, PCB_W/2 - 5.0, size=1.8)

    # 認證標記（右上角）
    create_silk_text("UL  CE  RoHS", PCB_L/2 - 15.0, PCB_W/2 - 5.0, size=1.6)

    # --- BGA 晶片絲印外框（U1 位置） ---
    bga_cx, bga_cy = -30.0, 0.0
    create_silk_outline_rect(bga_cx, bga_cy, 16.0, 16.0, line_width=0.15, name="Silk_U1_BGA")
    create_silk_text("U1", bga_cx, bga_cy - 9.5, size=1.5)
    # Pin 1 標記（圓點 + 三角形角標）
    create_silk_circle(bga_cx - 7.0, bga_cy - 7.0, 0.5, line_width=0.12, name="Silk_U1_Pin1")

    # --- RF 模塊絲印外框（U2 位置） ---
    rf_cx, rf_cy = 35.0, 0.0
    create_silk_outline_rect(rf_cx, rf_cy, 22.0, 17.0, line_width=0.15, name="Silk_U2_RF")
    create_silk_text("U2", rf_cx, rf_cy - 10.0, size=1.5)
    # RF 屏蔽罩輪廓用虛線表示（用短線段）
    for i in range(4):
        seg_y = rf_cy - 8.5 + i * 2.0
        seg = _cube(f"Silk_U2_Dash_{i}",
                     rf_cx, seg_y, silk_z + silk_thickness/2,
                     3.0, 0.15, silk_thickness, silk_mat)
        silk_objects.append(seg)

    # --- SMT 排座絲印外框（J1 位置） ---
    conn_cx, conn_cy = 0.0, 25.0
    create_silk_outline_rect(conn_cx, conn_cy, 54.0, 6.5, line_width=0.15, name="Silk_J1_Header")
    create_silk_text("J1", conn_cx, conn_cy + 4.5, size=1.5)
    # 排座 Pin 1 標記
    create_silk_circle(conn_cx - 27.0, conn_cy - 3.0, 0.4, line_width=0.12, name="Silk_J1_Pin1")

    # --- 散熱片區域標記 ---
    hs_cx, hs_cy = -30.0, -25.0
    create_silk_outline_rect(hs_cx, hs_cy, 22.0, 22.0, line_width=0.15, name="Silk_HS1")
    create_silk_text("HS1", hs_cx, hs_cy + 12.5, size=1.3)

    # --- 功率電感絲印 ---
    ind_cx, ind_cy = 45.0, 25.0
    create_silk_outline_rect(ind_cx, ind_cy, 14.0, 14.0, line_width=0.15, name="Silk_L1")
    create_silk_text("L1", ind_cx, ind_cy - 8.5, size=1.5)
    # 極性標記（線圈繞向）
    silk_ring = create_silk_circle(ind_cx, ind_cy, 2.0, line_width=0.12, name="Silk_L1_Polarity")

    # --- 安裝孔標記（四角螺絲孔） ---
    hx = PCB_L / 2.0 - 5.0
    hy = PCB_W / 2.0 - 5.0
    for sign_x in [-1, 1]:
        for sign_y in [-1, 1]:
            hole_cx = hx * sign_x
            hole_cy = hy * sign_y
            create_silk_circle(hole_cx, hole_cy, 3.5, line_width=0.15,
                             name=f"Silk_MTG_{sign_x:+}_{sign_y:+}")
            # 十字交叉線（防止螺絲旋轉鬆脫標記）
            cross_len = 2.0
            for angle in [0, 90]:
                rad = math.radians(angle)
                dx = math.cos(rad) * cross_len / 2.0
                dy = math.sin(rad) * cross_len / 2.0
                cross = _cube(
                    f"Silk_MTGCross_{sign_x:+}{sign_y:+}_{angle}",
                    hole_cx + dx, hole_cy + dy,
                    silk_z + silk_thickness/2,
                    cross_len if angle == 0 else 0.15,
                    cross_len if angle == 90 else 0.15,
                    silk_thickness, silk_mat
                )
                silk_objects.append(cross)

    # --- 板邊標記（V-Cut / Panel 切割引導線） ---
    # 在板四個邊各畫一條短標記線（幫助辨識方向和板邊）
    edge_mark_length = 5.0
    edge_mark_offset = 3.0
    for sign_x, sign_y in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        if sign_y == 0:  # 左右邊
            ex = PCB_L/2 * sign_x - edge_mark_offset * sign_x
            ey = 0
            esx, esy = 0.15, edge_mark_length
        else:  # 上下邊
            ex = 0
            ey = PCB_W/2 * sign_y - edge_mark_offset * sign_y
            esx, esy = edge_mark_length, 0.15
        mark = _cube(f"Silk_EdgeMark_{sign_x:+}{sign_y:+}",
                      ex, ey, silk_z + silk_thickness/2,
                      esx, esy, silk_thickness, silk_mat)
        silk_objects.append(mark)

    # --- 金手指編號標記 ---
    # 在金手指區下方標註 "PCIe x1"
    create_silk_text("PCIe x1", 0, -PCB_W/2 + 3.0, size=1.3)

    # --- 天線區域警告標記 ---
    create_silk_text("ANTENNA AREA", rf_cx + 12.0, rf_cy, size=1.2)
    create_silk_text("DO NOT COVER", rf_cx + 12.0, rf_cy - 1.8, size=1.0)

    print(f"[SILK] ✅ 絲印層建立完成，共 {len(silk_objects)} 個物件\n")

    return silk_objects


# ══════════════════════════════════════════════════════════════════════════════
# 第 3 部分：底面銅箔接地層
# ══════════════════════════════════════════════════════════════════════════════

def create_bottom_copper_layer():
    """
    在 PCB 底面建立整面銅箔接地層。

    底面通常是完整的 GND 平面（Ground Plane），
    只在過孔和安裝孔處有開孔（Anti-pad／隔離環）。

    這層銅箔：
        - 厚度 CU_THICKNESS（0.035mm）
        - 材質：銅色金屬
        - 在安裝孔處有隔離環（直徑比孔徑大，防止短路）
    """
    print("\n" + "=" * 50)
    print("  建立底面銅箔層（GND Plane）")
    print("=" * 50)

    copper_mat = _get_or_create_material(
        "PCB_Copper", (0.85, 0.55, 0.35), 1.0, 0.15, ior=1.18
    )

    # 底面銅箔 Z 座標（在 PCB 底面下方）
    cu_z = BOARD_BOTTOM_Z - CU_THICKNESS / 2.0 - ZF_OFFSET

    bottom_cu = _cube(
        name="CopperLayer_Bottom_GND",
        x=0, y=0, z=cu_z,
        sx=PCB_L - 0.2,  # 略小於板邊（銅箔距板邊 0.1mm）
        sy=PCB_W - 0.2,
        sz=CU_THICKNESS,
        material=copper_mat
    )
    print(f"  [CU] 底面 GND Plane: {PCB_L-0.2}×{PCB_W-0.2}×{CU_THICKNESS}mm")

    # --- 在安裝孔處建立隔離環（Anti-pad） ---
    # Anti-pad 直徑 = 孔徑 + 0.5mm（隔離間距）
    hx = PCB_L / 2.0 - 5.0
    hy = PCB_W / 2.0 - 5.0
    anti_pad_diameter = 3.2 + 0.5  # 3.7mm（比孔徑 3.2mm 大 0.5mm）

    anti_pad_count = 0
    for sign_x in [-1, 1]:
        for sign_y in [-1, 1]:
            ap_x = hx * sign_x
            ap_y = hy * sign_y

            cutter = _cylinder(
                name=f"AntiPad_{sign_x:+}{sign_y:+}",
                x=ap_x, y=ap_y,
                z_bottom=cu_z - CU_THICKNESS,
                z_top=cu_z + CU_THICKNESS,
                radius=anti_pad_diameter / 2.0,
                vertices=48,
                material=None
            )
            _boolean_difference(bottom_cu, cutter, apply=True)
            anti_pad_count += 1

    print(f"  [CU] 隔離環 (Anti-pad): {anti_pad_count} 個 Ø{anti_pad_diameter}mm")

    # --- 在過孔位置建立隔離環 ---
    # 自動偵測所有 Via 物件並在銅箔層開隔離環
    via_anti_pad_diameter = 0.5 + 0.2  # 過孔外徑 + 0.2mm 隔離

    via_count = 0
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and 'Via_Outer_' in obj.name:
            cutter = _cylinder(
                name=f"AntiPadVia_{obj.name}",
                x=obj.location.x,
                y=obj.location.y,
                z_bottom=cu_z - CU_THICKNESS,
                z_top=cu_z + CU_THICKNESS,
                radius=via_anti_pad_diameter / 2.0,
                vertices=32,
                material=None
            )
            _boolean_difference(bottom_cu, cutter, apply=True)
            via_count += 1

    print(f"  [CU] 過孔隔離環: {via_count} 個 Ø{via_anti_pad_diameter}mm")
    print(f"[CU] ✅ 底面銅箔 GND Plane 建立完成\n")

    return bottom_cu


# ══════════════════════════════════════════════════════════════════════════════
# 第 4 部分：PCB 邊緣斜面（Edge Bevel）
# ══════════════════════════════════════════════════════════════════════════════

def create_edge_bevel():
    """
    對 PCB 基板的邊緣（側面）添加微小的斜切倒角。

    PCB 邊緣通常有 0.2-0.5mm 的輕微倒角，
    這是 CNC 銑刀（Router Bit）的加工痕跡。
    我們透過對頂面和底面的邊緣各做一個微小的 Bevel 來模擬。
    """
    print("\n" + "=" * 50)
    print("  建立 PCB 邊緣斜面")
    print("=" * 50)

    pcb = _get_pcb_body()
    if pcb is None:
        print("[EDGE] ⚠ 找不到 PCB 基板，跳過邊緣斜面")
        return None

    bpy.context.view_layer.objects.active = pcb
    pcb.select_set(True)

    # 使用 Bevel modifier 對所有銳邊做微小倒角
    mod = pcb.modifiers.new(name="EdgeBevel", type='BEVEL')
    mod.width = 0.15          # 0.15mm 邊緣斜面
    mod.segments = 2          # 2 段 = 平滑過渡
    mod.limit_method = 'ANGLE'
    mod.angle_limit = 0.5236  # 30° — 只對接近 90° 的邊倒角
    bpy.ops.object.modifier_apply(modifier=mod.name)

    print(f"[EDGE] ✅ PCB 邊緣倒角: 0.15mm × 2 segments\n")
    return pcb


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：場景最終設定 + 渲染輸出
# ══════════════════════════════════════════════════════════════════════════════

def finalize_scene_and_render(output_dir=None):
    """
    最終化場景設定，包含：
        - 調整燈光（使各層材質細節可見）
        - 設定世界背景（World / HDRI 模擬）
        - 設定多個渲染視角（Camera angles）
        - 設定輸出參數
        - 可選執行渲染

    參數：
        output_dir: 輸出目錄（如果為 None 則僅設定不渲染）
    """
    print("\n" + "=" * 50)
    print("  最終場景設定 + 渲染配置")
    print("=" * 50)

    scene = bpy.context.scene

    # --- 5.1 世界背景（柔光環境） ---
    # 建立一個程序化的世界背景（暖灰漸層）
    world = bpy.data.worlds.get("PCB_World")
    if not world:
        world = bpy.data.worlds.new(name="PCB_World")
    scene.world = world
    world.use_nodes = True
    wn = world.node_tree.nodes
    wl = world.node_tree.links
    wn.clear()

    # 背景色節點
    bg = wn.new(type='ShaderNodeBackground')
    bg.location = (200, 0)
    bg.inputs['Strength'].default_value = 0.8
    bg.inputs['Color'].default_value = (0.85, 0.87, 0.90, 1.0)  # 淺灰藍（無塵室燈光）

    # 輕微漸層（模擬頭頂光源）
    gradient = wn.new(type='ShaderNodeTexGradient')
    gradient.location = (-300, 0)
    gradient.gradient_type = 'SPHERICAL'

    # 顏色映射
    color_ramp = wn.new(type='ShaderNodeValToRGB')
    color_ramp.location = (-100, 0)
    color_ramp.color_ramp.elements[0].color = (0.90, 0.92, 0.95, 1.0)  # 頂部亮
    color_ramp.color_ramp.elements[1].color = (0.60, 0.62, 0.65, 1.0)  # 底部暗
    wl.new(gradient.outputs['Fac'], color_ramp.inputs['Fac'])
    wl.new(color_ramp.outputs['Color'], bg.inputs['Color'])

    # 輸出
    wout = wn.new(type='ShaderNodeOutputWorld')
    wout.location = (500, 0)
    wl.new(bg.outputs['Background'], wout.inputs['Surface'])

    print("[FINAL] 世界背景: 暖灰漸層環境光")

    # --- 5.2 渲染取樣與降噪 ---
    scene.cycles.samples = 512               # 最終渲染用較高取樣數
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = 'OPENIMAGEDENOISE'
    scene.cycles.max_bounces = 12            # 多一些反彈（FR-4 半透明需要）
    scene.cycles.diffuse_bounces = 6
    scene.cycles.glossy_bounces = 6
    scene.cycles.transmission_bounces = 16
    scene.cycles.transparent_max_bounces = 8

    # 渲染 tile 大小（GPU 友善）
    scene.cycles.tile_size = 256

    print(f"[FINAL] Cycles: {scene.cycles.samples} samples, {scene.cycles.max_bounces} bounces")

    # --- 5.3 輸出設定 ---
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '16'     # 16-bit 色深
    scene.render.image_settings.compression = 15       # PNG 壓縮級別（0-100%, 15=輕度壓縮）
    scene.render.film_transparent = True               # 透明背景（可疊加）

    # Filmic 色彩管理
    scene.view_settings.view_transform = 'Filmic'
    scene.view_settings.look = 'Medium High Contrast'
    scene.view_settings.exposure = 0.2
    scene.view_settings.gamma = 1.0

    print(f"[FINAL] 輸出: {scene.render.resolution_x}×{scene.render.resolution_y}, "
          f"{scene.render.image_settings.color_depth}-bit PNG")

    # --- 5.4 建立多個渲染視角 ---
    cameras = {}

    # 主相機：等角視圖（Isometric-like）
    cam_main = bpy.data.objects.get("Camera_Main")
    if cam_main is None:
        bpy.ops.object.camera_add(location=(100, -70, 60))
        cam_main = bpy.context.active_object
        cam_main.name = "Camera_Main"

        look_at = bpy.data.objects.new("Camera_Target", None)
        bpy.context.collection.objects.link(look_at)
        look_at.location = (0, 0, 15)

        constraint = cam_main.constraints.new(type='TRACK_TO')
        constraint.target = look_at
        constraint.track_axis = 'TRACK_NEGATIVE_Z'
        constraint.up_axis = 'UP_Y'

    cameras['Main_Isometric'] = cam_main
    scene.camera = cam_main

    # 俯視圖（Top-down，展示 PCB 佈局）
    bpy.ops.object.camera_add(location=(0, 0, 120))
    cam_top = bpy.context.active_object
    cam_top.name = "Camera_TopDown"
    cam_top.rotation_euler = (0, 0, 0)
    cameras['TopDown'] = cam_top

    # 金手指特寫（底面邊緣視角）
    bpy.ops.object.camera_add(location=(0, -PCB_W/2 - 30, 10))
    cam_finger = bpy.context.active_object
    cam_finger.name = "Camera_GoldFingers"
    look_at2 = bpy.data.objects.new("CamTarget_Fingers", None)
    bpy.context.collection.objects.link(look_at2)
    look_at2.location = (0, -PCB_W/2, 0)
    constraint2 = cam_finger.constraints.new(type='TRACK_TO')
    constraint2.target = look_at2
    constraint2.track_axis = 'TRACK_NEGATIVE_Z'
    constraint2.up_axis = 'UP_Y'
    cameras['GoldFingers'] = cam_finger

    # BGA 晶片特寫（低角度側視）
    bpy.ops.object.camera_add(location=(-50, -40, 8))
    cam_bga = bpy.context.active_object
    cam_bga.name = "Camera_BGA_CloseUp"
    look_at3 = bpy.data.objects.new("CamTarget_BGA", None)
    bpy.context.collection.objects.link(look_at3)
    look_at3.location = (-30, 0, 1)
    constraint3 = cam_bga.constraints.new(type='TRACK_TO')
    constraint3.target = look_at3
    constraint3.track_axis = 'TRACK_NEGATIVE_Z'
    constraint3.up_axis = 'UP_Y'
    cameras['BGA_CloseUp'] = cam_bga

    print(f"[FINAL] 渲染視角: {len(cameras)} 個 ({', '.join(cameras.keys())})")

    # --- 5.5 可選渲染輸出 ---
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

        for cam_name, cam_obj in cameras.items():
            scene.camera = cam_obj
            filepath = os.path.join(output_dir, f"pcb_render_{cam_name}.png")
            scene.render.filepath = filepath
            print(f"[RENDER] 渲染中: {cam_name} → {filepath}")
            bpy.ops.render.render(write_still=True)

        print(f"\n[RENDER] ✅ 所有視角渲染完成，輸出目錄: {output_dir}")
    else:
        print("[FINAL] 未指定輸出目錄，跳過渲染")
        print("[FINAL] 提示：呼叫 finalize_scene_and_render('/path/to/output') 來渲染")

    print(f"[FINAL] ✅ 場景最終化完成\n")

    return cameras


# ══════════════════════════════════════════════════════════════════════════════
# 第 6 部分：場景總覽與物件管理
# ══════════════════════════════════════════════════════════════════════════════

def organize_collections():
    """
    將所有物件組織到 Blender Collection 中，便於管理。

    建立以下集合：
        - PCB_Base: 基板、銅箔、阻焊、絲印
        - PCB_Components: BGA、RF 模塊、連接器、散熱片、電感
        - PCB_Traces: 走線、過孔、金手指
        - Lighting: 燈光
        - Cameras: 相機
    """
    print("\n" + "=" * 50)
    print("  整理場景集合（Collections）")
    print("=" * 50)

    # 清除默認集合中的物件（移到對應的新集合）

    def ensure_collection(name):
        """確保集合存在，不存在就建立。"""
        coll = bpy.data.collections.get(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(coll)
        return coll

    # 定義分類規則
    coll_rules = {
        'PCB_Base': ['PCB_Base_Body', 'SolderMask_', 'CopperLayer_', 'Silk_',
                      'SilkText_', 'SilkRect_', 'SilkCircle_'],
        'PCB_Traces': ['DiffPair_', 'GoldFinger_', 'Via_', 'IFA_',
                        'AntennaClearance', 'PTH_'],
        'PCB_Components': ['BGA_', 'RF_', 'J1_', 'HS1_', 'L1_',
                            'Pad_', 'Ball_', 'Foot_', 'Shoulder_',
                            'TermTop_', 'TermBottom_', 'TermVert_',
                            'ShieldCan', 'MountHole_', 'FeedPad'],
        'PCB_Mounting': ['MountHole_Cutter_', 'Pad_Top_', 'Pad_Bottom_',
                          'MountHole_', 'PTH_Copper_'],
    }

    collections = {}
    for coll_name in coll_rules:
        collections[coll_name] = ensure_collection(coll_name)

    # 遍歷所有物件並分類
    # 注意：Blender 的 collection link/unlink 需要小心處理
    moved = 0
    for obj in bpy.data.objects:
        if obj.name in ('Camera_Main', 'Camera_TopDown', 'Camera_GoldFingers',
                         'Camera_BGA_CloseUp', 'Camera_Target', 'CamTarget_Fingers',
                         'CamTarget_BGA'):
            continue  # 相機留在默認集合或手動管理
        if obj.name.startswith('Light_'):
            continue  # 燈光留在默認集合

        # 判斷歸屬
        assigned = False
        for coll_name, keywords in coll_rules.items():
            for kw in keywords:
                if kw in obj.name:
                    # 將物件連結到目標集合
                    coll = collections[coll_name]
                    if obj.name not in coll.objects:
                        coll.objects.link(obj)
                        moved += 1
                    assigned = True
                    break
            if assigned:
                break

    print(f"[ORG] 已整理 {moved} 個物件到 {len(collections)} 個集合")

    # 列出各集合物件數
    for coll_name, coll in collections.items():
        obj_count = len(coll.objects)
        print(f"  - {coll_name}: {obj_count} 物件")

    print(f"[ORG] ✅ 場景整理完成\n")
    return collections


# ══════════════════════════════════════════════════════════════════════════════
# 主入口：循序執行步驟 4 的所有內容
# ══════════════════════════════════════════════════════════════════════════════

def run_step4(output_dir=None):
    """
    執行步驟 4 的全部內容。

    參數：
        output_dir: 渲染輸出目錄。如果為 None 則不渲染，
                    只建立模型和設定場景。
    """
    print("=" * 60)
    print("  ATOS PRO — PCB 超逼真建模工具 v4.0")
    print("  步驟 4：阻焊層 + 絲印層 + 銅箔層 + 渲染輸出")
    print("=" * 60)
    print()

    # 檢查 PCB 基板
    pcb = _get_pcb_body()
    if pcb is None:
        print("[WARN] ⚠ 未檢測到 PCB 基板！")
        print("[WARN] 請先執行步驟 1 建立基板，否則層疊將無效。")
        print("[WARN] 將繼續建立層疊，但可能無法正確對齊。")
    else:
        print(f"[INFO] PCB 基板已存在: {pcb.name}")

    # 1. 建立阻焊層
    mask = create_solder_mask_layer()

    # 2. 建立絲印層
    silk = create_silkscreen_layer()

    # 3. 建立底面銅箔接地層
    bottom_cu = create_bottom_copper_layer()

    # 4. PCB 邊緣斜面
    bevel = create_edge_bevel()

    # 5. 場景最終設定
    cameras = finalize_scene_and_render(output_dir=output_dir)

    # 6. 整理集合
    collections = organize_collections()

    # --- 最終統計 ---
    total_objects = len(bpy.data.objects)
    total_vertices = sum(
        len(obj.data.vertices)
        for obj in bpy.data.objects
        if obj.type == 'MESH'
    )
    total_faces = sum(
        len(obj.data.polygons)
        for obj in bpy.data.objects
        if obj.type == 'MESH'
    )

    print("\n" + "=" * 60)
    print(f"  ✅ ATOS PRO PCB 建模全部完成！")
    print(f"  ─────────────────────────────")
    print(f"  總物件數: {total_objects}")
    print(f"  總頂點數: {total_vertices:,}")
    print(f"  總面數:   {total_faces:,}")
    print(f"  ─────────────────────────────")
    print(f"  層疊結構:")
    print(f"    頂面絲印    {SILKSCREEN_THICKNESS}mm")
    print(f"    頂面阻焊    {SOLDERMASK_THICKNESS}mm")
    print(f"    頂面銅箔    {CU_THICKNESS}mm")
    print(f"    FR-4 基板  {PCB_H}mm")
    print(f"    底面銅箔    {CU_THICKNESS}mm (GND Plane)")
    print(f"    底面阻焊    {SOLDERMASK_THICKNESS}mm")
    print(f"  ─────────────────────────────")
    if output_dir:
        print(f"  渲染輸出: {output_dir}")
    print("=" * 60)
    print()
    print("📌 PCB 建模四步驟全部完成。")
    print("   使用 Blender GUI 開啟 .blend 檔案即可檢視和互動渲染。")


# ══════════════════════════════════════════════════════════════════════════════
# 獨立執行
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # 檢查命令列參數
    output = None
    if len(sys.argv) > 1:
        output = sys.argv[1]

    if output:
        print(f"渲染輸出目錄: {output}")
    else:
        print("未指定輸出目錄，僅建模不渲染。")
        print("用法: blender --background --python step4.py -- /path/to/output")

    run_step4(output_dir=output)
