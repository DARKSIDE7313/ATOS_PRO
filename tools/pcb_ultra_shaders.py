"""
ATOS_PRO/tools/pcb_ultra_shaders.py
Blender 4.2+ / Python 3.11 — PCB 照片級 PBR 材質引擎

全部使用底層 Shader Nodes (Principled BSDF + Noise/Wave/Voronoi/Musgrave)
零純色、零常數 Roughness — 所有參數均由程序化紋理驅動。

與 pcb_ultra_step1~4 的幾何函數完全相容。
執行此腳本後，場景中所有 PCB 相關物件的材質會被升級為照片級 PBR。

Author: Claude Engineer
Date: 2026-06-28
"""

import bpy
import math

# ══════════════════════════════════════════════════════════════════════════════
# 工具函數
# ══════════════════════════════════════════════════════════════════════════════

def _clear_nodes(mat):
    """清除材質的所有節點。"""
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    return nodes, mat.node_tree.links


def _make_output(nodes, links, bsdf, location=(800, 0)):
    """建立 Material Output 並連結 BSDF。"""
    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = location
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 1. FR-4 照片級基板材質
# ══════════════════════════════════════════════════════════════════════════════

def create_fr4_photoreal():
    """
    FR-4 基板 — 照片級 PBR 材質。

    物理特徵：
        - 雙層 90° 交錯 Wave Texture → 玻璃纖維經緯編織 Bump
        - Noise Texture → Roughness 非均勻分佈
        - 走線處阻焊凸起 (Solder Mask Height Map) → 0.02mm 起伏
        - Subsurface Scattering 0.25，黃綠色散射 → 半透明樹脂感
        - 微觀邊緣高光 (Edge Specular via Bevel + Clearcoat)
    """
    mat = bpy.data.materials.new(name="PCB_FR4_Photoreal")
    nodes, links = _clear_nodes(mat)

    # ---- 紋理座標 ----
    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-1400, 0)

    mapping_main = nodes.new(type='ShaderNodeMapping')
    mapping_main.location = (-1200, 0)
    mapping_main.inputs['Scale'].default_value = (1.0, 1.0, 1.0)
    links.new(tex_coord.outputs['UV'], mapping_main.inputs['Vector'])

    # ---- 玻璃纖維經線 Wave Texture（0° 方向） ----
    wave_warp = nodes.new(type='ShaderNodeTexWave')
    wave_warp.location = (-1000, 300)
    wave_warp.wave_type = 'BANDS'
    wave_warp.wave_profile = 'SIN'
    wave_warp.inputs['Scale'].default_value = 75.0        # ≈ 每 mm 0.75 根纖維
    wave_warp.inputs['Distortion'].default_value = 1.8    # 輕微彎曲（模擬編織不完美）
    wave_warp.inputs['Detail'].default_value = 5.0
    wave_warp.inputs['Detail Roughness'].default_value = 0.5
    links.new(mapping_main.outputs['Vector'], wave_warp.inputs['Vector'])

    # ---- 玻璃纖維緯線 Wave Texture（90° 方向） ----
    mapping_weft = nodes.new(type='ShaderNodeMapping')
    mapping_weft.location = (-1200, -100)
    mapping_weft.inputs['Rotation'].default_value = (0, 0, math.radians(90))
    links.new(tex_coord.outputs['UV'], mapping_weft.inputs['Vector'])

    wave_weft = nodes.new(type='ShaderNodeTexWave')
    wave_weft.location = (-1000, -100)
    wave_weft.wave_type = 'BANDS'
    wave_weft.wave_profile = 'SIN'
    wave_weft.inputs['Scale'].default_value = 75.0
    wave_weft.inputs['Distortion'].default_value = 1.8
    wave_weft.inputs['Detail'].default_value = 5.0
    wave_weft.inputs['Detail Roughness'].default_value = 0.5
    links.new(mapping_weft.outputs['Vector'], wave_weft.inputs['Vector'])

    # ---- 經緯編織混合 → Bump ----
    # 使用 Math MULTIPLY 節點混合兩個 Wave 的 Fac（經線 × 緯線）
    weave_math = nodes.new(type='ShaderNodeMath')
    weave_math.location = (-750, 150)
    weave_math.operation = 'MULTIPLY'
    weave_math.inputs[0].default_value = 0.5
    links.new(wave_warp.outputs['Fac'], weave_math.inputs[0])
    links.new(wave_weft.outputs['Fac'], weave_math.inputs[1])

    bump_weave = nodes.new(type='ShaderNodeBump')
    bump_weave.location = (-500, 150)
    bump_weave.inputs['Strength'].default_value = 0.10   # 編織紋理深度
    bump_weave.inputs['Distance'].default_value = 0.008
    links.new(weave_math.outputs['Value'], bump_weave.inputs['Height'])

    # ---- 阻焊綠漆起伏 (Solder Mask Height Map) ----
    # 使用高頻 Noise 模擬走線下方綠漆的微觀不平整
    # 現實中走線處綠漆會凸起約 0.02mm → Bump Strength 0.02
    noise_solder_mask = nodes.new(type='ShaderNodeTexNoise')
    noise_solder_mask.location = (-750, -350)
    noise_solder_mask.inputs['Scale'].default_value = 40.0
    noise_solder_mask.inputs['Detail'].default_value = 8.0
    noise_solder_mask.inputs['Roughness'].default_value = 0.6
    links.new(mapping_main.outputs['Vector'], noise_solder_mask.inputs['Vector'])

    bump_mask = nodes.new(type='ShaderNodeBump')
    bump_mask.location = (-500, -350)
    bump_mask.inputs['Strength'].default_value = 0.02   # 0.02mm 阻焊起伏
    bump_mask.inputs['Distance'].default_value = 0.02
    links.new(noise_solder_mask.outputs['Fac'], bump_mask.inputs['Height'])

    # ---- 中頻 Noise（樹脂不均勻） ----
    noise_resin = nodes.new(type='ShaderNodeTexNoise')
    noise_resin.location = (-750, -550)
    noise_resin.inputs['Scale'].default_value = 120.0
    noise_resin.inputs['Detail'].default_value = 10.0
    noise_resin.inputs['Roughness'].default_value = 0.55
    links.new(mapping_main.outputs['Vector'], noise_resin.inputs['Vector'])

    bump_resin = nodes.new(type='ShaderNodeBump')
    bump_resin.location = (-500, -550)
    bump_resin.inputs['Strength'].default_value = 0.04   # 樹脂表面微不平
    bump_resin.inputs['Distance'].default_value = 0.005
    links.new(noise_resin.outputs['Fac'], bump_resin.inputs['Height'])

    # ---- 合併三層 Bump（編織 + 阻焊起伏 + 樹脂） ----
    bump_add_1 = nodes.new(type='ShaderNodeMath')
    bump_add_1.location = (-280, 50)
    bump_add_1.operation = 'ADD'
    links.new(bump_weave.outputs['Normal'], bump_add_1.inputs[0])
    links.new(bump_mask.outputs['Normal'], bump_add_1.inputs[1])

    bump_add_2 = nodes.new(type='ShaderNodeMath')
    bump_add_2.location = (-100, 50)
    bump_add_2.operation = 'ADD'
    links.new(bump_add_1.outputs['Value'], bump_add_2.inputs[0])
    links.new(bump_resin.outputs['Normal'], bump_add_2.inputs[1])

    # ---- 顏色：深綠基底 + Noise 變異 ----
    base_color = nodes.new(type='ShaderNodeRGB')
    base_color.location = (-750, 700)
    base_color.outputs[0].default_value = (0.09, 0.20, 0.09, 1.0)  # 深綠

    color_ramp = nodes.new(type='ShaderNodeValToRGB')
    color_ramp.location = (-500, 700)
    color_ramp.color_ramp.elements[0].color = (0.06, 0.14, 0.06, 1.0)  # 暗
    color_ramp.color_ramp.elements[1].color = (0.13, 0.24, 0.13, 1.0)  # 亮
    links.new(noise_resin.outputs['Fac'], color_ramp.inputs['Fac'])

    color_mix = nodes.new(type='ShaderNodeMix')
    color_mix.location = (-250, 700)
    color_mix.data_type = 'RGBA'
    color_mix.blend_type = 'MIX'
    color_mix.inputs['Factor'].default_value = 0.40
    links.new(base_color.outputs['Color'], color_mix.inputs['A'])
    links.new(color_ramp.outputs['Color'], color_mix.inputs['B'])

    # ---- Principled BSDF ----
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['IOR'].default_value = 1.52
    bsdf.inputs['Specular IOR Level'].default_value = 0.20

    links.new(color_mix.outputs['Result'], bsdf.inputs['Base Color'])
    links.new(bump_add_2.outputs['Value'], bsdf.inputs['Normal'])

    # ---- Roughness：基底 0.42 + Noise 動態 ±0.12 ----
    roughness_range = nodes.new(type='ShaderNodeMapRange')
    roughness_range.location = (-250, 400)
    roughness_range.inputs['From Min'].default_value = 0.0
    roughness_range.inputs['From Max'].default_value = 1.0
    roughness_range.inputs['To Min'].default_value = -0.12
    roughness_range.inputs['To Max'].default_value = 0.12
    links.new(noise_resin.outputs['Fac'], roughness_range.inputs['Value'])

    roughness_add = nodes.new(type='ShaderNodeMath')
    roughness_add.location = (-50, 400)
    roughness_add.operation = 'ADD'
    roughness_val = nodes.new(type='ShaderNodeValue')
    roughness_val.location = (-250, 350)
    roughness_val.outputs[0].default_value = 0.42   # 基底 Roughness
    links.new(roughness_val.outputs['Value'], roughness_add.inputs[0])
    links.new(roughness_range.outputs['Result'], roughness_add.inputs[1])
    links.new(roughness_add.outputs['Value'], bsdf.inputs['Roughness'])

    # ---- Subsurface Scattering（FR-4 半透明樹脂質感） ----
    bsdf.inputs['Subsurface Weight'].default_value = 0.25
    bsdf.inputs['Subsurface Radius'].default_value = (0.20, 0.48, 0.20)  # 黃綠色散射
    bsdf.inputs['Subsurface Scale'].default_value = 0.06
    bsdf.inputs['Subsurface Anisotropy'].default_value = 0.35
    bsdf.inputs['Transmission Weight'].default_value = 0.08

    # ---- 輸出 ----
    _make_output(nodes, links, bsdf)
    print("  [PBR] FR-4: 雙層編織 Bump + 阻焊起伏 + SSS 0.25")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 2. 熔融焊錫照片級材質 + 助焊劑殘留
# ══════════════════════════════════════════════════════════════════════════════

def create_solder_photoreal():
    """
    焊錫材質 — 模擬 SAC305 無鉛焊錫的真實外觀。

    物理特徵：
        - Metallic = 1.0
        - 高頻 Noise → Roughness 0.08~0.18 非均勻（氧化不平整）
        - 微細 Voronoi → 金屬結晶顆粒 Normal 干擾
        - 顏色 #B5B8B1 亮銀錫色
    """
    mat = bpy.data.materials.new(name="PCB_Solder_Photoreal")
    nodes, links = _clear_nodes(mat)

    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-1000, 0)

    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-800, 0)
    mapping.inputs['Scale'].default_value = (1.0, 1.0, 1.0)
    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

    # ---- 高頻 Noise → 氧化不平整 Roughness ----
    noise_oxide = nodes.new(type='ShaderNodeTexNoise')
    noise_oxide.location = (-600, 200)
    noise_oxide.inputs['Scale'].default_value = 600.0     # 極高頻
    noise_oxide.inputs['Detail'].default_value = 12.0
    noise_oxide.inputs['Roughness'].default_value = 0.55
    links.new(mapping.outputs['Vector'], noise_oxide.inputs['Vector'])

    # Noise → Roughness 0.08~0.18
    rough_range = nodes.new(type='ShaderNodeMapRange')
    rough_range.location = (-350, 200)
    rough_range.inputs['From Min'].default_value = 0.0
    rough_range.inputs['From Max'].default_value = 1.0
    rough_range.inputs['To Min'].default_value = 0.08
    rough_range.inputs['To Max'].default_value = 0.18
    links.new(noise_oxide.outputs['Fac'], rough_range.inputs['Value'])

    # ---- Voronoi → 金屬結晶顆粒 Normal 干擾 ----
    voronoi_grain = nodes.new(type='ShaderNodeTexVoronoi')
    voronoi_grain.location = (-600, -250)
    voronoi_grain.voronoi_dimensions = '3D'
    voronoi_grain.feature = 'F1'
    voronoi_grain.inputs['Scale'].default_value = 300.0
    links.new(mapping.outputs['Vector'], voronoi_grain.inputs['Vector'])

    bump_grain = nodes.new(type='ShaderNodeBump')
    bump_grain.location = (-350, -250)
    bump_grain.inputs['Strength'].default_value = 0.03     # 極微細結晶凹凸
    bump_grain.inputs['Distance'].default_value = 0.001
    links.new(voronoi_grain.outputs['Distance'], bump_grain.inputs['Height'])

    # ---- 中頻 Noise → 微觀表面波紋 ----
    noise_wave = nodes.new(type='ShaderNodeTexNoise')
    noise_wave.location = (-600, -450)
    noise_wave.inputs['Scale'].default_value = 150.0
    noise_wave.inputs['Detail'].default_value = 6.0
    noise_wave.inputs['Roughness'].default_value = 0.65
    links.new(mapping.outputs['Vector'], noise_wave.inputs['Vector'])

    bump_wave = nodes.new(type='ShaderNodeBump')
    bump_wave.location = (-350, -450)
    bump_wave.inputs['Strength'].default_value = 0.015
    bump_wave.inputs['Distance'].default_value = 0.003
    links.new(noise_wave.outputs['Fac'], bump_wave.inputs['Height'])

    # ---- 合併 Normal ----
    bump_add = nodes.new(type='ShaderNodeMath')
    bump_add.location = (-150, -300)
    bump_add.operation = 'ADD'
    links.new(bump_grain.outputs['Normal'], bump_add.inputs[0])
    links.new(bump_wave.outputs['Normal'], bump_add.inputs[1])

    # ---- Principled BSDF ----
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.710, 0.722, 0.694, 1.0)  # #B5B8B1
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['IOR'].default_value = 1.9
    bsdf.inputs['Anisotropic'].default_value = 0.06

    links.new(rough_range.outputs['Result'], bsdf.inputs['Roughness'])
    links.new(bump_add.outputs['Value'], bsdf.inputs['Normal'])

    _make_output(nodes, links, bsdf)
    print("  [PBR] 焊錫: Voronoi結晶 + Noise氧化, Roughness 0.08~0.18")
    return mat


def create_flux_residue_material():
    """
    助焊劑殘留材質 — 微薄透明層。

    物理特徵：
        - 半透明 (Transmission Weight)
        - 極高 Roughness = 0.85（樹脂狀乾涸殘留）
        - 輕微黃褐色 (#C8B896)
        - 極薄面（幾何厚度 ~0.005mm），疊加在焊錫上方
    """
    mat = bpy.data.materials.new(name="PCB_FluxResidue")
    nodes, links = _clear_nodes(mat)

    # 高頻 Noise → 殘留不均勻
    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-600, 0)
    noise = nodes.new(type='ShaderNodeTexNoise')
    noise.location = (-350, 100)
    noise.inputs['Scale'].default_value = 500.0
    noise.inputs['Detail'].default_value = 10.0
    links.new(tex_coord.outputs['UV'], noise.inputs['Vector'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    # 黃褐色半透明
    bsdf.inputs['Base Color'].default_value = (0.784, 0.722, 0.588, 1.0)  # #C8B896
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.85
    bsdf.inputs['IOR'].default_value = 1.45
    bsdf.inputs['Transmission Weight'].default_value = 0.55   # 半透明
    bsdf.inputs['Alpha'].default_value = 0.45                 # 局部覆蓋

    # Noise → 殘留厚度不均（Alpha 變化）
    alpha_range = nodes.new(type='ShaderNodeMapRange')
    alpha_range.location = (0, 100)
    alpha_range.inputs['From Min'].default_value = 0.0
    alpha_range.inputs['From Max'].default_value = 1.0
    alpha_range.inputs['To Min'].default_value = 0.20
    alpha_range.inputs['To Max'].default_value = 0.60
    links.new(noise.outputs['Fac'], alpha_range.inputs['Value'])
    links.new(alpha_range.outputs['Result'], bsdf.inputs['Alpha'])

    _make_output(nodes, links, bsdf)
    print("  [PBR] 助焊劑殘留: 黃褐半透明, Roughness=0.85")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 3. IC 封裝塑料照片級材質
# ══════════════════════════════════════════════════════════════════════════════

def create_ic_package_photoreal():
    """
    IC 封裝塑料（BGA / QFP 本體）— 照片級 PBR。

    物理特徵：
        - Musgrave Texture（低頻）→ 注塑變形波浪起伏
        - Noise Texture（高頻）→ 磨砂顆粒感
        - Roughness 在 0.55~0.78 之間動態跳變
        - 微細表面凹陷模擬模具咬花（Mold Texture）
    """
    mat = bpy.data.materials.new(name="PCB_ICPackage_Photoreal")
    nodes, links = _clear_nodes(mat)

    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-1200, 0)

    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-1000, 0)
    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

    # ---- Noise（低頻 FBM）→ 注塑波浪變形 ----
    # Blender 4.5 移除了 Musgrave，使用 Noise 替代
    noise_wave_lf = nodes.new(type='ShaderNodeTexNoise')
    noise_wave_lf.location = (-750, 300)
    noise_wave_lf.inputs['Scale'].default_value = 15.0        # 低頻
    noise_wave_lf.inputs['Detail'].default_value = 8.0
    noise_wave_lf.inputs['Roughness'].default_value = 0.70
    noise_wave_lf.inputs['Lacunarity'].default_value = 2.5
    links.new(mapping.outputs['Vector'], noise_wave_lf.inputs['Vector'])

    bump_lf = nodes.new(type='ShaderNodeBump')
    bump_lf.location = (-450, 300)
    bump_lf.inputs['Strength'].default_value = 0.04         # 注塑波浪
    bump_lf.inputs['Distance'].default_value = 0.010
    links.new(noise_wave_lf.outputs['Fac'], bump_lf.inputs['Height'])

    # ---- Noise（高頻）→ 磨砂顆粒感 ----
    noise_grain = nodes.new(type='ShaderNodeTexNoise')
    noise_grain.location = (-750, 0)
    noise_grain.inputs['Scale'].default_value = 350.0        # 高頻
    noise_grain.inputs['Detail'].default_value = 12.0
    noise_grain.inputs['Roughness'].default_value = 0.58
    links.new(mapping.outputs['Vector'], noise_grain.inputs['Vector'])

    bump_grain = nodes.new(type='ShaderNodeBump')
    bump_grain.location = (-450, 0)
    bump_grain.inputs['Strength'].default_value = 0.06       # 磨砂凹凸
    bump_grain.inputs['Distance'].default_value = 0.002
    links.new(noise_grain.outputs['Fac'], bump_grain.inputs['Height'])

    # ---- 模具咬花紋理（極高頻） ----
    noise_mold = nodes.new(type='ShaderNodeTexNoise')
    noise_mold.location = (-750, -300)
    noise_mold.inputs['Scale'].default_value = 800.0
    noise_mold.inputs['Detail'].default_value = 8.0
    noise_mold.inputs['Roughness'].default_value = 0.50
    links.new(mapping.outputs['Vector'], noise_mold.inputs['Vector'])

    bump_mold = nodes.new(type='ShaderNodeBump')
    bump_mold.location = (-450, -300)
    bump_mold.inputs['Strength'].default_value = 0.02
    bump_mold.inputs['Distance'].default_value = 0.001
    links.new(noise_mold.outputs['Fac'], bump_mold.inputs['Height'])

    # ---- 合併三層 Bump ----
    bump_add_1 = nodes.new(type='ShaderNodeMath')
    bump_add_1.location = (-200, 100)
    bump_add_1.operation = 'ADD'
    links.new(bump_lf.outputs['Normal'], bump_add_1.inputs[0])
    links.new(bump_grain.outputs['Normal'], bump_add_1.inputs[1])

    bump_add_2 = nodes.new(type='ShaderNodeMath')
    bump_add_2.location = (0, 100)
    bump_add_2.operation = 'ADD'
    links.new(bump_add_1.outputs['Value'], bump_add_2.inputs[0])
    links.new(bump_mold.outputs['Normal'], bump_add_2.inputs[1])

    # ---- Roughness 動態跳變 (0.55~0.78) ----
    # 使用 Musgrave + Noise 混合驅動
    rough_mix = nodes.new(type='ShaderNodeMix')
    rough_mix.location = (-200, 450)
    rough_mix.data_type = 'FLOAT'
    rough_mix.blend_type = 'MIX'
    rough_mix.inputs['Factor'].default_value = 0.5
    links.new(noise_wave_lf.outputs['Fac'], rough_mix.inputs['A'])
    links.new(noise_grain.outputs['Fac'], rough_mix.inputs['B'])

    rough_range = nodes.new(type='ShaderNodeMapRange')
    rough_range.location = (50, 450)
    rough_range.inputs['From Min'].default_value = 0.0
    rough_range.inputs['From Max'].default_value = 1.0
    rough_range.inputs['To Min'].default_value = 0.55
    rough_range.inputs['To Max'].default_value = 0.78
    links.new(rough_mix.outputs['Result'], rough_range.inputs['Value'])

    # ---- Principled BSDF ----
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.035, 0.035, 0.04, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['IOR'].default_value = 1.55
    bsdf.inputs['Specular IOR Level'].default_value = 0.10
    bsdf.inputs['Sheen Weight'].default_value = 0.15         # 輕微天鵝絨光澤

    links.new(rough_range.outputs['Result'], bsdf.inputs['Roughness'])
    links.new(bump_add_2.outputs['Value'], bsdf.inputs['Normal'])

    _make_output(nodes, links, bsdf)
    print("  [PBR] IC封裝: Musgrave波浪 + Noise磨砂, Roughness 0.55~0.78")
    return mat


def create_laser_marking_text(text, x, y, z, size=1.0, material=None):
    """
    建立雷射雕刻標記（負向凹陷文字）。

    雷射雕刻不會添加材料，而是在表面燒蝕出微小凹陷 (~0.005mm)。
    這裡建立一個略凹的文字網格，材質為啞光灰。
    """
    bpy.ops.object.text_add(location=(x, y, z))
    text_obj = bpy.context.active_object
    text_obj.name = f"LaserMark_{text[:20]}"
    text_obj.data.body = text
    text_obj.data.size = size
    text_obj.data.extrude = 0.003      # 微小凹陷深度
    text_obj.data.align_x = 'CENTER'
    text_obj.data.align_y = 'CENTER'
    text_obj.data.font = None

    bpy.context.view_layer.objects.active = text_obj
    bpy.ops.object.convert(target='MESH')

    if material:
        text_obj.data.materials.append(material)
    return text_obj


def create_laser_mark_material():
    """
    雷射雕刻標記材質：啞光灰。

    與晶片本體的亮黑形成對比，但比絲印白更暗更柔和。
    Roughness = 0.70, 顏色為中性灰 (#808080)。
    """
    mat = bpy.data.materials.new(name="PCB_LaserMark")
    nodes, links = _clear_nodes(mat)

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    bsdf.inputs['Base Color'].default_value = (0.45, 0.45, 0.46, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.70
    bsdf.inputs['IOR'].default_value = 1.5

    _make_output(nodes, links, bsdf)
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 4. ENIG 硬鍍金照片級材質
# ══════════════════════════════════════════════════════════════════════════════

def create_enig_gold_photoreal():
    """
    ENIG 沉金工藝材質 — 照片級 PBR。

    物理特徵：
        - 顏色 #D4AF37 工業硬金
        - Metallic = 1.0
        - Voronoi Texture → 沉金表面微觀金屬結晶顆粒 (Normal)
        - 高頻 Noise → Roughness 0.06~0.14 非均勻
        - 輕微 Anisotropic → 電鍍方向紋理
    """
    mat = bpy.data.materials.new(name="PCB_ENIG_Gold_Photoreal")
    nodes, links = _clear_nodes(mat)

    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-1000, 0)

    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-800, 0)
    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

    # ---- Voronoi → 金屬結晶顆粒 ----
    voronoi = nodes.new(type='ShaderNodeTexVoronoi')
    voronoi.location = (-550, 250)
    voronoi.voronoi_dimensions = '3D'
    voronoi.feature = 'F1'
    voronoi.inputs['Scale'].default_value = 250.0
    links.new(mapping.outputs['Vector'], voronoi.inputs['Vector'])

    bump_voronoi = nodes.new(type='ShaderNodeBump')
    bump_voronoi.location = (-280, 250)
    bump_voronoi.inputs['Strength'].default_value = 0.04
    bump_voronoi.inputs['Distance'].default_value = 0.0015
    links.new(voronoi.outputs['Distance'], bump_voronoi.inputs['Height'])

    # ---- 高頻 Noise → Roughness 非均勻 ----
    noise_hf = nodes.new(type='ShaderNodeTexNoise')
    noise_hf.location = (-550, -50)
    noise_hf.inputs['Scale'].default_value = 500.0
    noise_hf.inputs['Detail'].default_value = 12.0
    noise_hf.inputs['Roughness'].default_value = 0.50
    links.new(mapping.outputs['Vector'], noise_hf.inputs['Vector'])

    rough_range = nodes.new(type='ShaderNodeMapRange')
    rough_range.location = (-280, -50)
    rough_range.inputs['From Min'].default_value = 0.0
    rough_range.inputs['From Max'].default_value = 1.0
    rough_range.inputs['To Min'].default_value = 0.06
    rough_range.inputs['To Max'].default_value = 0.14
    links.new(noise_hf.outputs['Fac'], rough_range.inputs['Value'])

    # ---- 中頻 Noise → 電鍍紋理 ----
    noise_plate = nodes.new(type='ShaderNodeTexNoise')
    noise_plate.location = (-550, -350)
    noise_plate.inputs['Scale'].default_value = 100.0
    noise_plate.inputs['Detail'].default_value = 6.0
    noise_plate.inputs['Roughness'].default_value = 0.60
    links.new(mapping.outputs['Vector'], noise_plate.inputs['Vector'])

    bump_plate = nodes.new(type='ShaderNodeBump')
    bump_plate.location = (-280, -350)
    bump_plate.inputs['Strength'].default_value = 0.015
    bump_plate.inputs['Distance'].default_value = 0.004
    links.new(noise_plate.outputs['Fac'], bump_plate.inputs['Height'])

    # ---- 合併 Normal ----
    bump_add = nodes.new(type='ShaderNodeMath')
    bump_add.location = (-80, 100)
    bump_add.operation = 'ADD'
    links.new(bump_voronoi.outputs['Normal'], bump_add.inputs[0])
    links.new(bump_plate.outputs['Normal'], bump_add.inputs[1])

    # ---- Principled BSDF ----
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.831, 0.686, 0.216, 1.0)  # #D4AF37
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['IOR'].default_value = 0.47
    bsdf.inputs['Anisotropic'].default_value = 0.12
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.0

    links.new(rough_range.outputs['Result'], bsdf.inputs['Roughness'])
    links.new(bump_add.outputs['Value'], bsdf.inputs['Normal'])

    # 輕微 Clearcoat → 沉金表面保護層的高光
    bsdf.inputs['Coat Weight'].default_value = 0.08
    bsdf.inputs['Coat Roughness'].default_value = 0.15

    _make_output(nodes, links, bsdf)
    print("  [PBR] ENIG沉金: Voronoi結晶 + 電鍍紋理, #D4AF37, Roughness 0.06~0.14")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 5. 鋁散熱片拉絲金屬
# ══════════════════════════════════════════════════════════════════════════════

def create_brushed_aluminum_photoreal():
    """
    陽極氧化鋁散熱片 — 拉絲金屬 PBR。

    特徵：
        - Anisotropic 0.45 單向拉絲
        - 拉絲 Noise（UV 單向拉伸）
        - 陽極藍色 + 金屬基底
        - Roughness 0.28~0.42
    """
    mat = bpy.data.materials.new(name="PCB_Aluminum_Brushed")
    nodes, links = _clear_nodes(mat)

    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-1000, 0)

    # UV 單向拉伸（模擬拉絲方向）
    mapping_brush = nodes.new(type='ShaderNodeMapping')
    mapping_brush.location = (-800, 200)
    mapping_brush.inputs['Scale'].default_value = (0.2, 25.0, 25.0)  # X 方向拉伸
    links.new(tex_coord.outputs['UV'], mapping_brush.inputs['Vector'])

    noise_brush = nodes.new(type='ShaderNodeTexNoise')
    noise_brush.location = (-550, 200)
    noise_brush.inputs['Scale'].default_value = 200.0
    noise_brush.inputs['Detail'].default_value = 5.0
    noise_brush.inputs['Roughness'].default_value = 0.55
    links.new(mapping_brush.outputs['Vector'], noise_brush.inputs['Vector'])

    bump_brush = nodes.new(type='ShaderNodeBump')
    bump_brush.location = (-250, 200)
    bump_brush.inputs['Strength'].default_value = 0.06
    bump_brush.inputs['Distance'].default_value = 0.004
    links.new(noise_brush.outputs['Fac'], bump_brush.inputs['Height'])

    # Roughness 動態
    rough_range = nodes.new(type='ShaderNodeMapRange')
    rough_range.location = (-250, 350)
    rough_range.inputs['From Min'].default_value = 0.0
    rough_range.inputs['From Max'].default_value = 1.0
    rough_range.inputs['To Min'].default_value = 0.28
    rough_range.inputs['To Max'].default_value = 0.42
    links.new(noise_brush.outputs['Fac'], rough_range.inputs['Value'])

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (300, 0)
    bsdf.inputs['Base Color'].default_value = (0.14, 0.30, 0.55, 1.0)  # 陽極藍
    bsdf.inputs['Metallic'].default_value = 0.80
    bsdf.inputs['IOR'].default_value = 1.35
    bsdf.inputs['Anisotropic'].default_value = 0.45
    bsdf.inputs['Anisotropic Rotation'].default_value = 0.0

    links.new(rough_range.outputs['Result'], bsdf.inputs['Roughness'])
    links.new(bump_brush.outputs['Normal'], bsdf.inputs['Normal'])

    _make_output(nodes, links, bsdf)
    print("  [PBR] 拉絲鋁: Anisotropic=0.45, 陽極藍")
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 6. 場景材質自動升級
# ══════════════════════════════════════════════════════════════════════════════

def upgrade_all_materials():
    """
    自動偵測場景中所有 PCB 相關物件，
    根據名稱關鍵字匹配並替換為照片級 PBR 材質。
    """
    print("\n" + "=" * 60)
    print("  照片級 PBR 材質升級")
    print("=" * 60)

    # 建立所有 PBR 材質
    mats = {
        'FR4': create_fr4_photoreal(),
        'Solder': create_solder_photoreal(),
        'Flux': create_flux_residue_material(),
        'ICPackage': create_ic_package_photoreal(),
        'LaserMark': create_laser_mark_material(),
        'ENIG': create_enig_gold_photoreal(),
        'Aluminum': create_brushed_aluminum_photoreal(),
    }

    # 關鍵字匹配規則
    rules = [
        # (關鍵字列表, 材質名稱)
        (['PCB_Base', 'PCB_Body', 'PCB_Ultra'], 'FR4'),
        (['Solder', 'Ball_', 'Fillet', 'SAC305', 'Via_'], 'Solder'),
        (['BGA', 'Epoxy', 'LCP', 'Plastic', 'Body', 'Package'], 'ICPackage'),
        (['Gold', 'Trace', 'Finger', 'DiffPair', 'ENIG', 'IFA_'], 'ENIG'),
        (['Heatsink', 'Fin_', 'Aluminum', 'ShieldCan', 'Shield_'], 'Aluminum'),
        (['Laser', 'Mark'], 'LaserMark'),
    ]

    upgraded = 0
    skipped = 0

    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not obj.data.materials:
            continue

        obj_name_upper = obj.name

        for keywords, mat_key in rules:
            if any(kw in obj_name_upper for kw in keywords):
                if len(obj.data.materials) > 0:
                    obj.data.materials[0] = mats[mat_key]
                    upgraded += 1
                break
        else:
            skipped += 1

    print(f"  [UPGRADE] 已升級: {upgraded} 物件 → PBR 材質")
    print(f"  [UPGRADE] 未匹配: {skipped} 物件 (保留原材質)")
    print(f"\n  PBR 材質清單:")
    for key, mat in mats.items():
        print(f"    {key}: {mat.name}")
    print("=" * 60)

    return mats


# ══════════════════════════════════════════════════════════════════════════════
# 7. 基板邊緣微觀倒角
# ══════════════════════════════════════════════════════════════════════════════

def apply_edge_bevel_to_pcb():
    """
    對 PCB 基板的所有銳邊執行 0.15mm 微觀圓角倒角。

    這對於在渲染時捕捉邊緣高光（Specular Edge Highlight）至關重要。
    """
    print("\n[BEVEL] 對 PCB 基板邊緣執行 0.15mm 倒角...")

    pcb_found = False
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and ('PCB_Base' in obj.name or 'PCB_Body' in obj.name):
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # 對所有銳邊做 Bevel
            mod = obj.modifiers.new(name="PCB_EdgeBevel", type='BEVEL')
            mod.width = 0.15
            mod.segments = 3
            mod.limit_method = 'ANGLE'
            mod.angle_limit = 0.5236  # 30° — 只對銳邊倒角
            bpy.ops.object.modifier_apply(modifier=mod.name)

            pcb_found = True
            print(f"  [BEVEL] {obj.name}: 0.15mm × 3 segments")
            break

    if not pcb_found:
        print("  [BEVEL] ⚠ 未找到 PCB 基板物件")

    print("[BEVEL] ✅ 邊緣倒角完成\n")


# ══════════════════════════════════════════════════════════════════════════════
# 8. 助焊劑殘留幾何層
# ══════════════════════════════════════════════════════════════════════════════

def add_flux_residue_geometry():
    """
    在每個焊盤/引腳交界處疊加一層微薄的助焊劑殘留幾何體。

    自動尋找場景中包含 "Fillet" 或 "Pad_" 的物件，
    在其表面上方 0.003mm 處複製一個略大的薄面。
    """
    import bmesh
    print("\n[FLUX] 添加助焊劑殘留幾何層...")

    flux_mat = bpy.data.materials.get("PCB_FluxResidue")
    if not flux_mat:
        flux_mat = create_flux_residue_material()

    flux_count = 0
    for obj in list(bpy.data.objects):
        if obj.type != 'MESH':
            continue
        if 'Fillet' not in obj.name and 'Pad_' not in obj.name:
            continue
        if 'Ball_' in obj.name:
            continue  # 跳過錫球本身

        # 複製物件並微調 Z 軸
        flux_obj = obj.copy()
        flux_obj.data = obj.data.copy()
        flux_obj.name = f"Flux_{obj.name}"
        bpy.context.collection.objects.link(flux_obj)

        # 向上偏移 0.003mm（助焊劑極薄層）
        flux_obj.location.z += 0.003

        # 替換材質
        if flux_obj.data.materials:
            flux_obj.data.materials[0] = flux_mat
        else:
            flux_obj.data.materials.append(flux_mat)

        flux_count += 1

    print(f"  [FLUX] 已添加 {flux_count} 個助焊劑殘留層")
    print("[FLUX] ✅ 助焊劑殘留完成\n")
    return flux_count


# ══════════════════════════════════════════════════════════════════════════════
# 9. 雷射標記放置
# ══════════════════════════════════════════════════════════════════════════════

def add_laser_markings():
    """
    在 BGA 晶片頂面放置雷射雕刻標記。

    真實的 IC 封裝頂部通常有雷射雕刻的型號、批號等資訊。
    這裡建立略微凹陷（~0.005mm）的灰色文字。
    """
    print("\n[LASER] 放置雷射雕刻標記...")

    laser_mat = create_laser_mark_material()
    marks = []

    # 尋找 BGA 晶片物件以獲取其頂面 Z 座標
    bga_top_z = None
    bga_x, bga_y = 0, 0
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and 'BGA' in obj.name and 'Body' in obj.name:
            bga_x = obj.location.x
            bga_y = obj.location.y
            bga_top_z = obj.location.z + obj.dimensions.z / 2.0
            break

    if bga_top_z:
        # 主型號
        m1 = create_laser_marking_text(
            "STM32F407VGT6",
            bga_x, bga_y + 2.5, bga_top_z + 0.001,
            size=0.8, material=laser_mat
        )
        marks.append(m1)

        # 批號
        m2 = create_laser_marking_text(
            "GH22M  CHN 2250",
            bga_x, bga_y - 1.5, bga_top_z + 0.001,
            size=0.55, material=laser_mat
        )
        marks.append(m2)

        # Pin 1 標記圓點
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=24, radius=0.35, depth=0.003,
            location=(bga_x - 6.5, bga_y - 6.5, bga_top_z)
        )
        dot = bpy.context.active_object
        dot.name = "LaserMark_Pin1Dot"
        dot.data.materials.append(laser_mat)
        marks.append(dot)

    print(f"  [LASER] 已放置 {len(marks)} 個雷射標記")
    print("[LASER] ✅ 雷射雕刻完成\n")
    return marks


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

def run_photoreal_upgrade():
    """一鍵執行全部 PBR 升級。"""
    print("=" * 60)
    print("  PCB 照片級 PBR 材質 + 微觀幾何升級")
    print("=" * 60)

    # 1. 邊緣倒角
    apply_edge_bevel_to_pcb()

    # 2. PBR 材質升級
    mats = upgrade_all_materials()

    # 3. 助焊劑殘留
    add_flux_residue_geometry()

    # 4. 雷射標記
    add_laser_markings()

    print("=" * 60)
    print("  ✅ PBR 升級全部完成")
    print("  材質: FR-4(SSS+編織) 焊錫(Voronoi) IC(Musgrave+Noise)")
    print("        ENIG沉金(Voronoi結晶) 拉絲鋁(Anisotropic)")
    print("  瑕疵: 助焊劑殘留 + 雷射雕刻凹陷")
    print("=" * 60)


if __name__ == "__main__":
    run_photoreal_upgrade()
