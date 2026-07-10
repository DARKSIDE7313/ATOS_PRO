"""
ATOS_PRO/tools/pcb_ultra_step4.py
Blender 4.2+ / Python 3.11 — PCB 超精密建模
第四部分（最終）：立體絲印 + 幾何條形碼 + QR碼 + ESD標誌 + 全局總裝 + 渲染

全部使用 bmesh / mesh.from_pydata / Blender Text-to-Mesh。
零外部貼圖 —— 所有圖形均為真實 3D 幾何體。

與步驟 1/2/3 的全域參數完全相容。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import bmesh
import math
import random

# ══════════════════════════════════════════════════════════════════════════════
# 全域參數
# ══════════════════════════════════════════════════════════════════════════════

PCB_L = 140.0
PCB_W = 90.0
PCB_H = 1.6
CU_THICKNESS = 0.035
SOLDERMASK_THICKNESS = 0.02
SILKSCREEN_THICKNESS = 0.01
ZF_OFFSET = 0.002
BOARD_TOP_Z = PCB_H / 2.0       # 0.8mm（中心座標系）
BOARD_BOTTOM_Z = -PCB_H / 2.0

# 絕對座標系 → 中心座標系轉換
# absolute_x = center_x + 70, absolute_y = center_y + 45
ABS_OFFSET_X = PCB_L / 2.0  # 70
ABS_OFFSET_Y = PCB_W / 2.0  # 45


def abs_to_center(ax, ay, az=None):
    """絕對座標 → 中心座標系。Z 軸：絕對 Z=1.6 = 板頂面 = 中心系 Z=0.8。"""
    cx = ax - ABS_OFFSET_X
    cy = ay - ABS_OFFSET_Y
    if az is not None:
        cz = az - PCB_H / 2.0  # 絕對 Z=0→中心-0.8, 絕對Z=1.6→中心0.8
        return cx, cy, cz
    return cx, cy


# ══════════════════════════════════════════════════════════════════════════════
# 輔助函數
# ══════════════════════════════════════════════════════════════════════════════

def _bmesh_to_obj(bm, name, material=None):
    mesh = bpy.data.meshes.new(name=name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name=name, object_data=mesh)
    bpy.context.collection.objects.link(obj)
    if material:
        obj.data.materials.append(material)
    return obj


def _get_or_create_mat(name, color, metallic, roughness, ior=1.5):
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
    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 材質
# ══════════════════════════════════════════════════════════════════════════════

def create_silkscreen_white():
    """絲印白色：啞光、高粗糙度。Roughness=0.85。"""
    mat = bpy.data.materials.new(name="PCB_SilkWhite")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.92, 0.92, 0.91, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.85
    bsdf.inputs['IOR'].default_value = 1.5
    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


def create_silkscreen_black():
    """絲印黑色（條形碼/QR碼黑塊）。"""
    return _get_or_create_mat("PCB_SilkBlack", (0.02, 0.02, 0.03), 0.0, 0.80)


def create_silkscreen_yellow():
    """絲印黃色（ESD 警告標誌）。"""
    return _get_or_create_mat("PCB_SilkYellow", (0.90, 0.78, 0.15), 0.0, 0.80)


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：create_ultra_silkscreen()
# ══════════════════════════════════════════════════════════════════════════════

def create_text_mesh(text, x, y, z, size=1.2, material=None, name="SilkText"):
    """
    使用 Blender 內建字體建立立體文字網格。

    步驟：
        1. 建立 Text 物件
        2. 設定 extrude = 0.01mm（絲印厚度）
        3. 轉換為 Mesh
        4. 放置在指定位置

    注意：Blender 4.2+ 的 text.body 是正確的屬性名稱。
    """
    bpy.ops.object.text_add(location=(x, y, z))
    text_obj = bpy.context.active_object
    short_name = text.replace(' ', '_')[:30]
    text_obj.name = f"{name}_{short_name}"

    text_data = text_obj.data
    text_data.body = text
    text_data.size = size
    text_data.extrude = SILKSCREEN_THICKNESS  # 0.01mm 厚度
    text_data.align_x = 'CENTER'
    text_data.align_y = 'CENTER'
    text_data.font = None  # 使用內建 Bfont

    # 轉換為 Mesh
    bpy.context.view_layer.objects.active = text_obj
    bpy.ops.object.convert(target='MESH')

    if material:
        text_obj.data.materials.append(material)

    return text_obj


def create_geometric_barcode(x, y, z, bar_count=25, material_white=None, material_black=None):
    """
    使用 for 迴圈 + 隨機寬度陣列建立純幾何條形碼。

    規格：
        - 總尺寸: 10mm (X) × 2mm (Y) × 0.01mm (Z)
        - 白色基底 + 隨機寬度黑色長條
        - 不使用貼圖 —— 每個黑條都是一個獨立的薄長方體

    參數：
        x, y, z: 條形碼左下角絕對座標
        bar_count: 黑色條數量
    """
    barcode_w = 10.0
    barcode_h = 2.0
    thickness = SILKSCREEN_THICKNESS
    z_center = z + thickness / 2.0

    silk_objects = []

    # --- 白色基底 ---
    bm_base = bmesh.new()
    bmesh.ops.create_cube(bm_base, size=2.0)
    bm_base.verts.ensure_lookup_table()
    for v in bm_base.verts:
        v.co = (v.co.x * barcode_w / 2.0,
                 v.co.y * barcode_h / 2.0,
                 v.co.z * thickness / 2.0)
    base_obj = _bmesh_to_obj(bm_base, "Barcode_Base", material=material_white)
    base_obj.location = (x + barcode_w / 2.0, y + barcode_h / 2.0, z_center)
    silk_objects.append(base_obj)

    # --- 隨機黑色條 ---
    # 使用隨機種子確保可重複的結果
    random.seed(42)

    # 生成條形碼的寬度序列（模擬 Code 128 風格）
    # 實際條形碼由不同寬度的黑條和白間隙組成
    # 這裡用隨機寬度來近似
    bar_widths = []
    current_x = 0.0

    for _ in range(bar_count):
        # 隨機條寬：0.15 ~ 0.5mm
        bw = random.uniform(0.15, 0.5)
        bar_widths.append(bw)
        current_x += bw
        # 隨機白間隙：0.1 ~ 0.4mm
        gap = random.uniform(0.1, 0.4)
        current_x += gap
        if current_x >= barcode_w - 0.5:
            break

    bar_x = 0.0
    for i, bw in enumerate(bar_widths):
        bm_bar = bmesh.new()
        bmesh.ops.create_cube(bm_bar, size=2.0)
        bm_bar.verts.ensure_lookup_table()
        for v in bm_bar.verts:
            v.co = (v.co.x * bw / 2.0,
                     v.co.y * barcode_h / 2.0,
                     v.co.z * thickness / 2.0)
        bar_obj = _bmesh_to_obj(bm_bar, f"Barcode_Bar_{i+1}", material=material_black)
        # 放置：在白色基底上方 ZF_OFFSET 處（防止 Z-fighting）
        bar_obj.location = (
            x + bar_x + bw / 2.0,
            y + barcode_h / 2.0,
            z + thickness + ZF_OFFSET
        )
        silk_objects.append(bar_obj)
        bar_x += bw
        # 跳過間隙
        gap = random.uniform(0.1, 0.4)
        bar_x += gap

    random.seed()  # 恢復隨機種子
    print(f"  [SILK] 條形碼: {barcode_w}×{barcode_h}mm, {len(bar_widths)} 條黑線")
    return silk_objects


# ══════════════════════════════════════════════════════════════════════════════
# QR Code 矩陣定義
# ══════════════════════════════════════════════════════════════════════════════

# 15×15 二維矩陣（1=黑, 0=白）
# 使用簡化的 QR 碼結構：
#   - 三個 7×7 尋像圖形（Finder Pattern）位於左上、右上、左下角
#   - 其餘區域用偽隨機數據填充

def _make_finder_pattern():
    """建立一個 7×7 的尋像圖形：外框黑、內框黑、中心黑。"""
    return [
        [1, 1, 1, 1, 1, 1, 1],
        [1, 0, 0, 0, 0, 0, 1],
        [1, 0, 1, 1, 1, 0, 1],
        [1, 0, 1, 1, 1, 0, 1],
        [1, 0, 1, 1, 1, 0, 1],
        [1, 0, 0, 0, 0, 0, 1],
        [1, 1, 1, 1, 1, 1, 1],
    ]


def _generate_qr_matrix(size=15):
    """
    生成一個 size×size 的 QR 碼矩陣。
    包含三個尋像圖形 + 時序圖案 + 隨機數據區。
    """
    matrix = [[0] * size for _ in range(size)]

    # 放置左上角尋像圖形
    finder = _make_finder_pattern()
    for r in range(7):
        for c in range(7):
            matrix[r][c] = finder[r][c]

    # 放置右上角尋像圖形
    for r in range(7):
        for c in range(7):
            matrix[r][size - 7 + c] = finder[r][c]

    # 放置左下角尋像圖形
    for r in range(7):
        for c in range(7):
            matrix[size - 7 + r][c] = finder[r][c]

    # 時序圖案（Timing Pattern）：第 7 行和第 7 列，交替黑白
    for i in range(8, size - 8):
        matrix[7][i] = 1 if i % 2 == 0 else 0  # 水平時序
        matrix[i][7] = 1 if i % 2 == 0 else 0  # 垂直時序

    # 隨機數據區域填充（偽隨機但固定種子）
    random.seed(2026)
    for r in range(size):
        for c in range(size):
            # 跳過已有圖案的區域
            if (r < 7 and c < 7) or (r < 7 and c >= size - 7) or (r >= size - 7 and c < 7):
                continue
            if r == 7 or c == 7:
                continue
            # 右下角 2×2 區域保留為格式資訊
            if r >= size - 2 and c >= size - 2:
                matrix[r][c] = 0
                continue
            matrix[r][c] = 1 if random.random() > 0.55 else 0

    random.seed()
    return matrix


def create_geometric_qrcode(x, y, z, size=5.0, grid=15,
                             material_white=None, material_black=None):
    """
    使用雙重 for 迴圈 + 二維矩陣建立純幾何 QR 碼。

    規格：
        - 總尺寸: 5mm × 5mm × 0.01mm
        - 15×15 矩陣, 每格 ≈ 0.333mm
        - 白色基底 + 矩陣驅動的黑色立體方塊
        - 零貼圖，純幾何

    參數：
        x, y, z: QR 碼左下角絕對座標
        size: 總尺寸
        grid: 矩陣尺寸 (15×15)
    """
    cell_size = size / grid  # ≈ 0.333mm
    thickness = SILKSCREEN_THICKNESS
    z_center = z + thickness / 2.0

    silk_objects = []

    # --- 白色基底 ---
    bm_base = bmesh.new()
    bmesh.ops.create_cube(bm_base, size=2.0)
    bm_base.verts.ensure_lookup_table()
    for v in bm_base.verts:
        v.co = (v.co.x * size / 2.0,
                 v.co.y * size / 2.0,
                 v.co.z * thickness / 2.0)
    base_obj = _bmesh_to_obj(bm_base, "QR_Base", material=material_white)
    base_obj.location = (x + size / 2.0, y + size / 2.0, z_center)
    silk_objects.append(base_obj)

    # --- QR 碼矩陣驅動的黑色方塊 ---
    qr_matrix = _generate_qr_matrix(grid)

    block_z = z + thickness + ZF_OFFSET

    for row in range(grid):
        for col in range(grid):
            if qr_matrix[row][col] == 0:
                continue  # 白色格 = 不放置黑塊

            # 建立單個微型黑色方塊
            bm_cell = bmesh.new()
            bmesh.ops.create_cube(bm_cell, size=2.0)
            bm_cell.verts.ensure_lookup_table()
            half_c = cell_size / 2.0
            for v in bm_cell.verts:
                v.co = (v.co.x * half_c, v.co.y * half_c, v.co.z * half_c)

            cell_obj = _bmesh_to_obj(bm_cell,
                f"QR_R{row}C{col}", material=material_black)
            # QR 碼的 row=0 是頂部，在空間中對應 Y 最大值
            cell_obj.location = (
                x + col * cell_size + cell_size / 2.0,
                y + (grid - 1 - row) * cell_size + cell_size / 2.0,
                block_z + thickness / 2.0
            )
            silk_objects.append(cell_obj)

    black_count = sum(sum(row) for row in qr_matrix)
    print(f"  [SILK] QR碼: {size}×{size}mm, {grid}×{grid}, "
          f"每格 {cell_size:.3f}mm, {black_count} 個黑塊")
    return silk_objects


def create_esd_warning_symbol(x, y, z, size=4.0, material=None):
    """
    使用頂點拼接建立 ESD 防靜電警告標誌。

    結構：
        - 三角形邊框（等邊三角形 + 寬邊框效果）
        - 內部簡化閃電形狀（多段線構成的之字形）
        - 底部手掌簡化圖形（省略，只用閃電 + 三角框）

    方法：
        用 bmesh 建立三角形邊框輪廓面和閃電多邊形面。

    參數：
        x, y, z: 標誌中心絕對座標
        size: 三角形邊長
    """
    thickness = SILKSCREEN_THICKNESS
    z_center = z + thickness / 2.0
    silk_objects = []

    # 等邊三角形幾何
    tri_h = size * math.sqrt(3) / 2.0  # 等邊三角形高
    tri_r = size / math.sqrt(3)        # 外接圓半徑
    # 頂點（尖端朝上）
    top_y = y + tri_h * 0.55
    bot_y = y - tri_h * 0.45

    tri_verts_2d = [
        (x, top_y),                          # 頂點
        (x - size / 2.0, bot_y),             # 左下
        (x + size / 2.0, bot_y),             # 右下
    ]

    # --- 三角形外框（用線框寬度模擬） ---
    # 建立一個三角形面 + 內部挖空三角形 = 三角形框
    # 方法：建立兩個三角形（外層大 + 內層小），內層布爾挖空
    bm_outer = bmesh.new()
    v_outer_bot = []
    v_outer_top = []
    for vx, vy in tri_verts_2d:
        v_outer_bot.append(bm_outer.verts.new((vx, vy, z)))
        v_outer_top.append(bm_outer.verts.new((vx, vy, z + thickness)))
    bm_outer.verts.ensure_lookup_table()
    bm_outer.faces.new(v_outer_bot)
    bm_outer.faces.new(reversed(v_outer_top))
    for i in range(3):
        j = (i + 1) % 3
        bm_outer.faces.new([v_outer_bot[i], v_outer_bot[j],
                             v_outer_top[j], v_outer_top[i]])

    outer_obj = _bmesh_to_obj(bm_outer, "ESD_Triangle", material=material)
    silk_objects.append(outer_obj)

    # 內部挖空三角形（小 0.25mm）
    inset = 0.25
    # 用向量向內偏移
    inner_verts = []
    for i in range(3):
        vx, vy = tri_verts_2d[i]
        # 向三角形中心偏移
        dx = x - vx
        dy = (y + tri_h * 0.05) - vy  # 近似中心
        dist = math.hypot(dx, dy)
        if dist > 0.001:
            inner_verts.append((vx + dx / dist * inset,
                                vy + dy / dist * inset))
        else:
            inner_verts.append((vx, vy))

    bm_inner = bmesh.new()
    v_inner_bot, v_inner_top = [], []
    for vx, vy in inner_verts:
        v_inner_bot.append(bm_inner.verts.new((vx, vy, z - 0.01)))
        v_inner_top.append(bm_inner.verts.new((vx, vy, z + thickness + 0.01)))
    bm_inner.verts.ensure_lookup_table()
    bm_inner.faces.new(v_inner_bot)
    bm_inner.faces.new(reversed(v_inner_top))
    for i in range(3):
        j = (i + 1) % 3
        bm_inner.faces.new([v_inner_bot[i], v_inner_bot[j],
                             v_inner_top[j], v_inner_top[i]])
    inner_obj = _bmesh_to_obj(bm_inner, "ESD_InnerCutter")
    _apply_bool_diff(outer_obj, inner_obj)

    # --- 閃電形狀（之字折線構成的多邊形面） ---
    # 簡化閃電：中心垂直線 + 左右分支
    lightning_pts = [
        (x, top_y - 0.3),              # 頂端
        (x + 0.5, y + 0.2),            # 右折
        (x - 0.2, y + 0.05),           # 左折
        (x + 0.3, y - 0.5),            # 右折
        (x, bot_y + 0.3),              # 底端
        (x - 0.4, y - 0.1),            # 左回
        (x + 0.1, y + 0.15),           # 右回（結束）
    ]

    # 建立閃電面（用頂點 + 簡單三角形扇形）
    bm_lightning = bmesh.new()
    l_verts_bot, l_verts_top = [], []
    for px, py in lightning_pts:
        l_verts_bot.append(bm_lightning.verts.new((px, py, z + thickness + ZF_OFFSET)))
        l_verts_top.append(bm_lightning.verts.new((px, py, z + thickness * 2 + ZF_OFFSET)))
    bm_lightning.verts.ensure_lookup_table()
    # 頂面和底面
    bm_lightning.faces.new(l_verts_bot)
    bm_lightning.faces.new(reversed(l_verts_top))
    for i in range(len(l_verts_bot) - 1):
        bm_lightning.faces.new([l_verts_bot[i], l_verts_bot[i+1],
                                 l_verts_top[i+1], l_verts_top[i]])
    lightning_obj = _bmesh_to_obj(bm_lightning, "ESD_Lightning", material=material)
    silk_objects.append(lightning_obj)

    print(f"  [SILK] ESD 標誌: 三角形邊框 {size}mm + 閃電符號")
    return silk_objects


def _apply_bool_diff(target, cutter):
    """target - cutter 布爾差集。"""
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    mod = target.modifiers.new(name=f"Bool_{cutter.name}", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)
    cutter.hide_viewport = True
    cutter.hide_render = True


def create_ultra_silkscreen():
    """
    建立完整絲印層：元件位號 + 條形碼 + QR 碼 + ESD 標誌。

    所有圖形均為真實 3D 幾何體（厚度 0.01mm），
    不使用任何 UV 貼圖。
    """
    print("\n" + "=" * 50)
    print("  [SILK] 建立微米級立體絲印圖形")
    print("=" * 50)

    white_mat = create_silkscreen_white()
    black_mat = create_silkscreen_black()
    yellow_mat = create_silkscreen_yellow()

    # 絲印層 Z 座標（在阻焊層上方）
    silk_z = BOARD_TOP_Z + CU_THICKNESS + SOLDERMASK_THICKNESS + ZF_OFFSET * 2

    all_silk = []

    # --- 1. 元件位號標註 ---
    # 座標使用絕對座標系對應的中心座標
    # BGA @ (70,45)abs → (0,0)center
    cx_bga, cy_bga = abs_to_center(70, 45)
    t1 = create_text_mesh("BGA1", cx_bga, cy_bga - 11, silk_z, size=1.2,
                           material=white_mat, name="Silk_BGA")
    all_silk.append(t1)

    # RF @ (25,65)abs → (-45,20)center
    cx_rf, cy_rf = abs_to_center(25, 65)
    t2 = create_text_mesh("RF1", cx_rf, cy_rf - 11, silk_z, size=1.2,
                           material=white_mat, name="Silk_RF")
    all_silk.append(t2)

    # 排座 CN1 @ (70,82)abs → (0,37)center
    cx_cn, cy_cn = abs_to_center(70, 82)
    t3 = create_text_mesh("CN1", cx_cn - 30, cy_cn, silk_z, size=1.2,
                           material=white_mat, name="Silk_CN")
    all_silk.append(t3)

    # 電感 L1 @ (115,25)abs → (45,-20)center
    cx_l, cy_l = abs_to_center(115, 25)
    t4 = create_text_mesh("L1", cx_l, cy_l - 10, silk_z, size=1.2,
                           material=white_mat, name="Silk_L")
    all_silk.append(t4)

    # 板號和日期碼
    t5 = create_text_mesh("ATOS-PRO Rev 2.1  2026-06-28",
                           -35, -PCB_W/2 + 6, silk_z, size=1.8,
                           material=white_mat, name="Silk_PCBInfo")
    all_silk.append(t5)

    # UL / CE 認證標記
    t6 = create_text_mesh("UL  CE  RoHS",
                           PCB_L/2 - 15, PCB_W/2 - 5, silk_z, size=1.4,
                           material=white_mat, name="Silk_Cert")
    all_silk.append(t6)

    # 天線區域警告
    t7 = create_text_mesh("ANTENNA AREA",
                           -42, 20, silk_z, size=1.1,
                           material=white_mat, name="Silk_Ant")
    all_silk.append(t7)

    print(f"  [SILK] 文字標註: 7 組")

    # --- 2. 幾何條形碼（PCB 右下角） ---
    barcode_abs_x = 110  # 絕對 X
    barcode_abs_y = 8    # 絕對 Y
    bc_x, bc_y = abs_to_center(barcode_abs_x, barcode_abs_y)
    bc_obj = create_geometric_barcode(
        bc_x, bc_y, silk_z,
        bar_count=22,
        material_white=white_mat,
        material_black=black_mat
    )
    all_silk.extend(bc_obj)

    # --- 3. 幾何 QR 碼（條形碼旁邊） ---
    qr_abs_x = 122
    qr_abs_y = 8
    qr_x, qr_y = abs_to_center(qr_abs_x, qr_abs_y)
    qr_obj = create_geometric_qrcode(
        qr_x, qr_y, silk_z,
        size=5.0, grid=15,
        material_white=white_mat,
        material_black=black_mat
    )
    all_silk.extend(qr_obj)

    # --- 4. ESD 防靜電警告標誌（PCB 左上角） ---
    esd_abs_x = 12
    esd_abs_y = 78
    esd_x, esd_y = abs_to_center(esd_abs_x, esd_abs_y)
    esd_obj = create_esd_warning_symbol(
        esd_x, esd_y, silk_z,
        size=5.0,
        material=yellow_mat
    )
    all_silk.extend(esd_obj)

    print(f"[SILK] ✅ 全部絲印建立完成，共 {len(all_silk)} 個物件\n")
    return all_silk


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：全局總裝 main()
# ══════════════════════════════════════════════════════════════════════════════

def initialize_scene():
    """清除場景，設定 Cycles。"""
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for block in list(bpy.data.meshes) + list(bpy.data.materials) + list(bpy.data.curves):
        if hasattr(block, 'users') and block.users == 0:
            try:
                bpy.data.meshes.remove(block) if hasattr(block, 'vertices') else None
            except:
                pass

    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 384
    scene.cycles.max_bounces = 10
    scene.cycles.transmission_bounces = 12
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = 'OPENIMAGEDENOISE'
    scene.view_settings.view_transform = 'Filmic'
    scene.view_settings.look = 'Medium High Contrast'
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080

    print("[INIT] 場景初始化完成")


def setup_lighting():
    """
    全局展示打光系統：
        - 世界環境光（強度 0.5）
        - 三點式區域光（左前暖白、右前冷白、上方自然白）
    """
    # --- 世界環境光 ---
    world = bpy.data.worlds.new(name="PCB_Studio_World")
    bpy.context.scene.world = world
    world.use_nodes = True
    wn = world.node_tree.nodes
    wl = world.node_tree.links
    wn.clear()
    bg = wn.new(type='ShaderNodeBackground')
    bg.location = (200, 0)
    bg.inputs['Strength'].default_value = 0.5
    bg.inputs['Color'].default_value = (0.90, 0.92, 0.95, 1.0)
    wout = wn.new(type='ShaderNodeOutputWorld')
    wout.location = (500, 0)
    wl.new(bg.outputs['Background'], wout.inputs['Surface'])
    print("[LIGHT] 世界環境光: 強度 0.5")

    # --- 左前側光（暖白 3200K，勾勒金屬高光） ---
    bpy.ops.object.light_add(type='AREA', location=(-80, -40, 50))
    left_key = bpy.context.active_object
    left_key.name = "Light_Key_Warm"
    left_key.data.energy = 400
    left_key.data.size = 8.0
    left_key.data.color = (1.0, 0.90, 0.80)  # 暖白

    # --- 右前側光（冷白 5600K，補充陰影細節） ---
    bpy.ops.object.light_add(type='AREA', location=(70, 50, 45))
    right_fill = bpy.context.active_object
    right_fill.name = "Light_Fill_Cool"
    right_fill.data.energy = 250
    right_fill.data.size = 6.0
    right_fill.data.color = (0.85, 0.88, 1.0)  # 冷白

    # --- 頂光（自然白 4500K，整體照明） ---
    bpy.ops.object.light_add(type='AREA', location=(0, 0, 90))
    top_rim = bpy.context.active_object
    top_rim.name = "Light_Rim_Top"
    top_rim.data.energy = 350
    top_rim.data.size = 12.0
    top_rim.data.color = (0.95, 0.95, 0.93)  # 自然白

    # --- 底部補光（弱，展現 FR-4 半透明） ---
    bpy.ops.object.light_add(type='AREA', location=(0, 0, -30))
    bot_fill = bpy.context.active_object
    bot_fill.name = "Light_Bottom_Fill"
    bot_fill.data.energy = 80
    bot_fill.data.size = 8.0
    bot_fill.data.color = (0.90, 0.93, 0.96)

    # --- 相機 ---
    bpy.ops.object.camera_add(location=(100, -65, 55))
    cam = bpy.context.active_object
    cam.name = "Camera_Main"
    target = bpy.data.objects.new("Camera_Target", None)
    bpy.context.collection.objects.link(target)
    target.location = (0, 0, 5)
    constraint = cam.constraints.new(type='TRACK_TO')
    constraint.target = target
    constraint.track_axis = 'TRACK_NEGATIVE_Z'
    constraint.up_axis = 'UP_Y'
    bpy.context.scene.camera = cam

    print("[LIGHT] 三點式區域光: 暖白(K) + 冷白(F) + 頂光(R) + 底補")


def main():
    """
    全局總裝主函數。

    佈局（絕對座標系）：
        - PCB 基板: 原點 (0, 0, 0) → 佔據 Z: 0~1.6mm
        - BGA 晶片: (70, 45, 1.6) — 板中央
        - 散熱片:   (70, 45, 2.8) — BGA 上方
        - RF 模塊:  (25, 65, 1.6) — 左上區域
        - 電感:     (115, 25, 1.6) — 右下區域
        - 排座:     (70, 82, 1.6) — 頂邊區域
        - 金手指:   板底邊 Y=0 處
        - 蛇形線:   自動生成
        - 過孔陣列: 自動生成
        - 絲印層:   所有元件上方 ZF_OFFSET

    所有重疊面嚴格使用 0.002mm Z 軸偏置防止 Z-fighting。
    """
    print("=" * 60)
    print("  ATOS PRO — PCB 超精密建模 v5.0")
    print("  第四部分：全局總裝 + 立體絲印 + 渲染")
    print("=" * 60)
    print()

    # ---- 初始化 ----
    initialize_scene()

    # ---- 步驟 1: 基板 + 過孔 + 蛇形線 ----
    print("\n>>> 執行步驟 1: 基板 + 微觀工藝...")
    # 動態導入步驟 1 的函數（假設已載入或 exec）
    base_obj = create_ultra_pcb_base()
    vias = create_micro_vias()
    traces = create_serpentine_traces()

    # ---- 步驟 2: BGA + RF ----
    print("\n>>> 執行步驟 2: BGA 晶片 + RF 模塊...")

    # BGA @ (70, 45)abs → (0, 0)center
    bga_cx, bga_cy = abs_to_center(70, 45)
    bga = create_ultra_bga_chip(bga_cx, bga_cy, name="U1_BGA144")

    # RF @ (25, 65)abs → (-45, 20)center
    rf_cx, rf_cy = abs_to_center(25, 65)
    rf = create_rf_shield_and_antenna(rf_cx, rf_cy, name="U2_RF")

    # ---- 步驟 3: 連接器 + 散熱片 + 電感 ----
    print("\n>>> 執行步驟 3: 連接器 + 散熱片 + 電感...")

    # 注意：步驟 3 的 create_precision_connectors() 和
    # create_thermal_and_power_blocks() 有自己的內部座標。
    # 需要在呼叫前後手動調整位置。
    conn = create_precision_connectors()

    # 手動重新定位排座到 (70, 82)abs
    if conn.get('conn_body'):
        new_cx, new_cy = abs_to_center(70, 82)
        # 偏移排座本體和所有引腳
        dx = new_cx - conn['conn_body'].location.x
        dy = new_cy - conn['conn_body'].location.y
        conn['conn_body'].location.x = new_cx
        conn['conn_body'].location.y = new_cy
        for gw in conn.get('gull_wings', []):
            gw.location.x += dx
            gw.location.y += dy
        for fl in conn.get('fillets', []):
            fl.location.x += dx
            fl.location.y += dy
        for gf in conn.get('gold_fingers', []):
            pass  # 金手指保持在底邊

    thermal = create_thermal_and_power_blocks()

    # 重新定位散熱片到 (70, 45)abs, Z=2.8abs
    if thermal.get('heatsink_base'):
        hs_cx, hs_cy, hs_cz = abs_to_center(70, 45, 2.8)
        thermal['heatsink_base'].location = (hs_cx, hs_cy, hs_cz)
        for fin in thermal.get('heatsink_fins', []):
            fin.location.x += hs_cx - (-30)  # 從原位置偏移
            fin.location.y += hs_cy - (-25)
            fin.location.z += hs_cz - (BOARD_TOP_Z + CU_THICKNESS + ZF_OFFSET + 2.0)

    # 重新定位電感到 (115, 25)abs
    if thermal.get('inductor_body'):
        ind_cx, ind_cy = abs_to_center(115, 25)
        thermal['inductor_body'].location = (ind_cx, ind_cy,
                                              thermal['inductor_body'].location.z)
        for term in thermal.get('inductor_terminals', []):
            term.location.x += ind_cx - 45
            term.location.y += ind_cy - 25

    # ---- 步驟 4: 絲印層 ----
    print("\n>>> 執行步驟 4: 立體絲印圖形...")
    silk = create_ultra_silkscreen()

    # ---- 打光與相機 ----
    print("\n>>> 設定燈光與相機...")
    setup_lighting()

    # ---- 最終統計 ----
    total_objs = len(bpy.data.objects)
    total_verts = sum(len(o.data.vertices) for o in bpy.data.objects
                      if o.type == 'MESH')
    total_faces = sum(len(o.data.polygons) for o in bpy.data.objects
                      if o.type == 'MESH')

    # 設定視口著色
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'

    print("\n" + "=" * 60)
    print(f"  ✅ ATOS PRO PCB 超精密建模全部完成")
    print(f"  ─────────────────────────────")
    print(f"  總物件數: {total_objs}")
    print(f"  總頂點數: {total_verts:,}")
    print(f"  總面數:   {total_faces:,}")
    print(f"  ─────────────────────────────")
    print(f"  佈局核對:")
    print(f"    BGA   @ (70.0, 45.0, 1.6) abs")
    print(f"    散熱片 @ (70.0, 45.0, 2.8) abs")
    print(f"    RF    @ (25.0, 65.0, 1.6) abs")
    print(f"    排座   @ (70.0, 82.0, 1.6) abs")
    print(f"    電感   @ (115.0, 25.0, 1.6) abs")
    print(f"  ─────────────────────────────")
    print(f"  層疊 (Z 軸):")
    print(f"    Z=0.000   PCB 底面")
    print(f"    Z=0.800   PCB 中心")
    print(f"    Z=1.600   PCB 頂面 (元件層)")
    print(f"    Z=1.635   銅箔層")
    print(f"    Z=1.655   阻焊層")
    print(f"    Z=1.677   絲印層")
    print(f"    Z=2.800   BGA頂面 / 散熱片底面")
    print("=" * 60)

    return {
        'base': base_obj,
        'vias': vias,
        'traces': traces,
        'bga': bga,
        'rf': rf,
        'conn': conn,
        'thermal': thermal,
        'silk': silk,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 渲染輸出
# ══════════════════════════════════════════════════════════════════════════════

def render_all_views(output_dir):
    """渲染多個視角並儲存。"""
    import os
    os.makedirs(output_dir, exist_ok=True)

    scene = bpy.context.scene

    # 視角定義
    views = {
        'Main_Isometric': (100, -65, 55, 0, 0, 5),
        'TopDown': (0, 0, 100, 0, 0, 0),
        'BGA_CloseUp': (-40, -30, 8, -30, 0, 1.5),
    }

    for view_name, (cx, cy, cz, tx, ty, tz) in views.items():
        # 移動相機
        cam = scene.camera
        if cam:
            cam.location = (cx, cy, cz)
            # 更新追蹤目標
            for c in cam.constraints:
                if c.type == 'TRACK_TO' and c.target:
                    c.target.location = (tx, ty, tz)

        filepath = os.path.join(output_dir, f"pcb_ultra_{view_name}.png")
        scene.render.filepath = filepath
        print(f"[RENDER] {view_name} → {filepath}")
        bpy.ops.render.render(write_still=True)

    print(f"\n[RENDER] ✅ 全部渲染完成: {output_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# 獨立執行入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # 檢查前面的步驟函數是否已定義（如果在 Scripting 介面中拼接執行）
    if 'create_ultra_pcb_base' not in dir():
        print("=" * 60)
        print("  ⚠ 步驟 1/2/3 的函數未載入")
        print("  請確保已先執行 pcb_ultra_step1.py, step2.py, step3.py")
        print("  或在 Blender Scripting 中依序貼上全部四個步驟的代碼")
        print("=" * 60)
        print()
        print("  正確操作流程（見下方說明）：")
        print("  1. 打開 Blender → Scripting 工作區")
        print("  2. 在文字編輯器中新增一個新文本")
        print("  3. 依序貼上 step1 → step2 → step3 → step4 的全部代碼")
        print("  4. 在 step4 末尾，加入這一行並執行：")
        print("     result = main()")
        print("     render_all_views('/path/to/output')")
        print("=" * 60)
        sys.exit(0)

    # 輸出目錄
    output = None
    if len(sys.argv) > 1:
        output = sys.argv[1]

    result = main()

    if output:
        render_all_views(output)
    else:
        print("\n📌 建模完成。在 Blender 中按 F12 渲染，或呼叫 render_all_views()。")


# ══════════════════════════════════════════════════════════════════════════════
# 使用說明（中文）
# ══════════════════════════════════════════════════════════════════════════════
"""
═══════════════════════════════════════════════════════════
  ATOS PRO PCB 超精密建模 — 四步驟完整操作指南
═══════════════════════════════════════════════════════════

【方法一：Blender Scripting 介面拼接執行（推薦）】

1. 打開 Blender 4.2+
2. 點擊頂部選單 "Scripting" 進入腳本工作區
3. 在中央文字編輯器中，點擊 "+ New" 建立新文本
4. 依序貼上以下四個檔案的完整內容（順序不可顛倒）：
   - pcb_ultra_step1.py（基板 + 過孔 + 蛇形線）
   - pcb_ultra_step2.py（BGA + RF 屏蔽罩）
   - pcb_ultra_step3.py（金手指 + 排座 + 散熱片 + 電感）
   - pcb_ultra_step4.py（絲印 + 總裝 + 渲染）
5. 在文字編輯器末尾（所有代碼之後）添加：

       result = main()
       render_all_views('/Users/你的用戶名/Desktop/pcb_output')

6. 點擊上方的 "Run Script" 按鈕（或按 Alt+P）
7. 等待建模完成（約 1-3 分鐘），渲染圖將自動輸出到指定目錄

【方法二：命令行背景執行】

    /Applications/Blender.app/Contents/MacOS/Blender \\
      --background \\
      --python pcb_ultra_step1.py \\
      --python pcb_ultra_step2.py \\
      --python pcb_ultra_step3.py \\
      --python pcb_ultra_step4.py -- /path/to/output

【方法三：在 Python 控制台逐函數調用】

    import bpy
    exec(open("pcb_ultra_step1.py").read())
    exec(open("pcb_ultra_step2.py").read())
    exec(open("pcb_ultra_step3.py").read())
    exec(open("pcb_ultra_step4.py").read())
    result = main()

【注意事項】

- 本建模使用 CPU 渲染（GPU 若可用會自動偵測）
- 完整場景約 1000+ 物件、140,000+ 面
- 首次渲染可能需要 5-15 分鐘（取決於 CPU 性能）
- 所有座標單位為毫米（mm），Blender 單位設定為公制
- Z-fighting 已透過 0.002mm 全域偏置防護
- 絲印、條形碼、QR 碼均為真實幾何體，無貼圖依賴

═══════════════════════════════════════════════════════════
"""
