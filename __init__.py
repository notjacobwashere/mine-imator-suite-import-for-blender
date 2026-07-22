"""Mine-imator to Blender/MCprep Static Scene Bridge."""

from __future__ import annotations

bl_info = {
    "name": "Mine-imator MCprep Bridge",
    "author": "Mine-imator MCprep Bridge contributors",
    "version": (0, 3, 0),
    "blender": (5, 2, 0),
    "location": "File > Import; 3D View > Sidebar > MI Bridge",
    "description": "Import frame-zero Mine-imator scenes with editable Minecraft geometry",
    "category": "Import-Export",
}

import traceback
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from . import core, render_export, suite_environment
from .blender_importer import CATEGORY_NAMES, ImportOptions, SceneImporter, preflight, write_report


def _default_mineimator() -> str:
    found = core.find_mineimator_install()
    return str(found) if found else ""


def _default_asset_pack() -> str:
    found = core.find_mineimator_install()
    if not found:
        return ""
    try:
        version = core.read_asset_version(found)
    except core.BridgeError:
        return ""
    candidate = found / "Data" / "Minecraft" / f"{version}.zip"
    return str(candidate) if candidate.is_file() else ""


def _default_mineways() -> str:
    found = core.find_mineways()
    return str(found) if found else ""


def _environment_update(_self, context) -> None:
    if context and context.scene:
        suite_environment.apply_environment(context.scene)


def _biome_update(_self, context) -> None:
    if context and context.scene:
        suite_environment.apply_biome_preset(context.scene)
        suite_environment.apply_environment(context.scene)


def _suite_toggle_update(self, _context) -> None:
    if self.mineimator_suite:
        self.import_environment = True


COLOR_DEFAULTS = {
    "sky_color": (0.471, 0.655, 1.0, 1.0),
    "sky_clouds_color": (1.0, 1.0, 1.0, 1.0),
    "sunlight_color": (1.0, 0.969, 0.894, 1.0),
    "ambient_color": (0.4, 0.439, 0.549, 1.0),
    "night_color": (0.055, 0.055, 0.094, 1.0),
    "grass_color": (0.569, 0.741, 0.349, 1.0),
    "foliage_color": (0.467, 0.671, 0.184, 1.0),
    "water_color": (0.243, 0.459, 0.882, 1.0),
    "fog_color": (0.471, 0.655, 1.0, 1.0),
}


class MIBRIDGE_PG_environment(PropertyGroup):
    active: BoolProperty(default=False, options={"HIDDEN"})
    suite_id: StringProperty(options={"HIDDEN"})
    collection_name: StringProperty(options={"HIDDEN"})
    world_name: StringProperty(options={"HIDDEN"})
    source_json: StringProperty(options={"HIDDEN"})
    source_sky_time: FloatProperty(options={"HIDDEN"})

    time_hours: FloatProperty(name="Time", description="Minecraft time of day in hours", min=0.0, max=24.0, default=9.0, update=_environment_update)
    sky_rotation: FloatProperty(name="Rotation", description="Horizontal sky rotation in degrees", default=0.0, update=_environment_update)
    sunlight_angle: FloatProperty(name="Angle", description="Sun light angular size in degrees", min=0.0, default=0.526, update=_environment_update)
    sunlight_strength: FloatProperty(name="Strength", description="Sunlight strength percent", min=0.0, default=100.0, subtype="PERCENTAGE", update=_environment_update)

    sky_mode: EnumProperty(name="Sky", items=(("MINECRAFT", "Minecraft", "Use the Minecraft sky"), ("CUSTOM", "Custom", "Use a custom background image")), default="MINECRAFT", update=_environment_update)
    background_image: StringProperty(name="Background image", subtype="FILE_PATH", update=_environment_update)
    background_type: EnumProperty(name="Image type", items=(("IMAGE", "Image", "Screen-like image mapped as a sky"), ("SPHERE", "Sphere", "Spherical environment"), ("BOX", "Box", "Box environment")), default="IMAGE", update=_environment_update)
    background_stretch: BoolProperty(name="Stretch", default=True, update=_environment_update)
    background_box_mapped: BoolProperty(name="Box mapped", default=False, update=_environment_update)
    background_rotation: FloatProperty(name="Background rotation", default=0.0, update=_environment_update)
    sun_texture: StringProperty(name="Sun texture", subtype="FILE_PATH", update=_environment_update)
    sun_angle: FloatProperty(name="Sun angle", default=0.0, update=_environment_update)
    sun_size: FloatProperty(name="Sun size", min=0.0, default=100.0, subtype="PERCENTAGE", update=_environment_update)
    moon_texture: StringProperty(name="Moon texture", subtype="FILE_PATH", update=_environment_update)
    moon_phase: EnumProperty(name="Moon phase", items=tuple((str(index), label, "") for index, label in enumerate(("Full moon", "Waning gibbous", "Third quarter", "Waning crescent", "New moon", "Waxing crescent", "First quarter", "Waxing gibbous"))), default="0", update=_environment_update)
    moon_angle: FloatProperty(name="Moon angle", default=0.0, update=_environment_update)
    moon_size: FloatProperty(name="Moon size", min=0.0, default=100.0, subtype="PERCENTAGE", update=_environment_update)

    clouds_show: BoolProperty(name="Clouds", default=True, update=_environment_update)
    clouds_mode: EnumProperty(name="Appearance", items=(("NORMAL", "Normal", "Blocky Minecraft clouds"), ("FADED", "Faded", "Translucent faded clouds"), ("FLAT", "Flat", "Flat cloud layer")), default="NORMAL", update=_environment_update)
    clouds_texture: StringProperty(name="Texture", subtype="FILE_PATH", update=_environment_update)
    clouds_speed: FloatProperty(name="Speed", default=100.0, subtype="PERCENTAGE")
    clouds_offset: FloatProperty(name="Offset", default=0.0, update=_environment_update)
    clouds_height: FloatProperty(name="Height", default=1024.0, update=_environment_update)
    clouds_size: FloatProperty(name="Size", min=16.0, default=1536.0, update=_environment_update)
    clouds_thickness: FloatProperty(name="Thickness", min=0.0, default=64.0, update=_environment_update)

    ground_show: BoolProperty(name="Ground", default=True, update=_environment_update)
    ground_texture: StringProperty(name="Ground texture", subtype="FILE_PATH", update=_environment_update)
    biome: EnumProperty(name="Biome", items=suite_environment.biome_items, update=_biome_update)
    sky_color: FloatVectorProperty(name="Sky", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["sky_color"], update=_environment_update)
    sky_clouds_color: FloatVectorProperty(name="Clouds", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["sky_clouds_color"], update=_environment_update)
    sunlight_color: FloatVectorProperty(name="Sunlight", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["sunlight_color"], update=_environment_update)
    ambient_color: FloatVectorProperty(name="Ambient", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["ambient_color"], update=_environment_update)
    night_color: FloatVectorProperty(name="Night", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["night_color"], update=_environment_update)
    grass_color: FloatVectorProperty(name="Grass", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["grass_color"], update=_environment_update)
    foliage_color: FloatVectorProperty(name="Foliage", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    water_color: FloatVectorProperty(name="Water", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["water_color"], update=_environment_update)
    leaves_oak_color: FloatVectorProperty(name="Oak leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    leaves_spruce_color: FloatVectorProperty(name="Spruce leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.384, 0.659, 0.341, 1.0), update=_environment_update)
    leaves_birch_color: FloatVectorProperty(name="Birch leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.384, 0.659, 0.341, 1.0), update=_environment_update)
    leaves_jungle_color: FloatVectorProperty(name="Jungle leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    leaves_acacia_color: FloatVectorProperty(name="Acacia leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    leaves_dark_oak_color: FloatVectorProperty(name="Dark oak leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    leaves_mangrove_color: FloatVectorProperty(name="Mangrove leaves", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["foliage_color"], update=_environment_update)
    twilight: BoolProperty(name="Twilight", default=True, update=_environment_update)

    fog_show: BoolProperty(name="Fog", default=True, update=_environment_update)
    fog_sky: BoolProperty(name="Sky fog", default=True, update=_environment_update)
    fog_color_custom: BoolProperty(name="Custom fog color", default=False, update=_environment_update)
    fog_color: FloatVectorProperty(name="Fog color", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["fog_color"], update=_environment_update)
    fog_object_color_custom: BoolProperty(name="Custom object color", default=False, update=_environment_update)
    fog_object_color: FloatVectorProperty(name="Object fog color", subtype="COLOR", size=4, min=0.0, max=1.0, default=COLOR_DEFAULTS["fog_color"], update=_environment_update)
    fog_distance: FloatProperty(name="Distance", min=10.0, default=10000.0, update=_environment_update)
    fog_size: FloatProperty(name="Fade size", min=10.0, default=2000.0, update=_environment_update)
    fog_height: FloatProperty(name="Height", min=10.0, default=1250.0, update=_environment_update)

    wind: BoolProperty(name="Wind", default=True)
    wind_speed: FloatProperty(name="Speed", min=0.0, default=10.0, subtype="PERCENTAGE")
    wind_strength: FloatProperty(name="Strength", min=0.0, default=0.5)
    wind_direction: FloatProperty(name="Direction", default=45.0)
    wind_directional_speed: FloatProperty(name="Directional speed", min=0.0, default=20.0, subtype="PERCENTAGE")
    wind_directional_strength: FloatProperty(name="Directional strength", min=0.0, default=1.5)
    texture_animation_speed: FloatProperty(name="Texture animation speed", min=0.0, default=0.25)


class MIBRIDGE_PG_settings(PropertyGroup):
    project_path: StringProperty(
        name="Project",
        description="A Mine-imator project directory or .miproject file",
        subtype="FILE_PATH",
    )
    mineimator_path: StringProperty(
        name="Mine-imator",
        description="Mine-imator installation directory (automatically detected when empty)",
        subtype="DIR_PATH",
        default=_default_mineimator(),
    )
    asset_pack_path: StringProperty(
        name="Minecraft Assets",
        description="Mine-imator Minecraft asset ZIP (automatically detected when empty)",
        subtype="FILE_PATH",
        default=_default_asset_pack(),
    )
    mineways_path: StringProperty(
        name="Mineways",
        description="Mineways executable used for world scenery export",
        subtype="FILE_PATH",
        default=_default_mineways(),
    )
    mineimator_suite: BoolProperty(
        name="Mine-imator Suite",
        description="Create the full editable Mine-imator sky, lighting, clouds, fog, biome, and ground environment",
        default=True,
        update=_suite_toggle_update,
    )
    import_characters: BoolProperty(name="Characters and entities", default=True)
    import_items: BoolProperty(name="Items", default=True)
    import_blocks: BoolProperty(name="Blocks and special blocks", default=True)
    import_models: BoolProperty(name="Custom models", default=True)
    import_scenery: BoolProperty(name="World scenery", default=True)
    import_primitives: BoolProperty(name="Primitive shapes and text", default=True)
    import_lights: BoolProperty(name="Lights", default=True)
    import_camera: BoolProperty(name="Camera", default=True)
    import_environment: BoolProperty(name="Background/environment", default=True)
    import_helpers: BoolProperty(name="Paths and helper objects", default=True)
    use_mcprep: BoolProperty(
        name="Run MCprep material preparation",
        description="Run MCprep's material preparation after geometry creation when MCprep is enabled",
        default=True,
    )
    honor_item_keyframe_changes: BoolProperty(
        name="Use frame-0 item swaps",
        description=(
            "Override each template item with its frame-0 ITEM/ITEM_NAME value; "
            "leave disabled for projects whose saved compatibility hint is stale"
        ),
        default=False,
    )
    remove_startup_cube: BoolProperty(
        name="Remove untouched Blender startup cube",
        description="Remove only Blender's pristine default Cube when its original Camera and Light are still present",
        default=True,
    )


def _options(settings: MIBRIDGE_PG_settings) -> ImportOptions:
    categories = {
        key
        for key, enabled in (
            ("characters", settings.import_characters),
            ("items", settings.import_items),
            ("blocks", settings.import_blocks),
            ("models", settings.import_models),
            ("scenery", settings.import_scenery),
            ("primitives", settings.import_primitives),
            ("lights", settings.import_lights),
            ("camera", settings.import_camera),
            ("environment", settings.import_environment),
            ("helpers", settings.import_helpers),
        )
        if enabled
    }
    if settings.mineimator_suite:
        categories.add("environment")
    return ImportOptions(
        categories=categories,
        mineimator_path=bpy.path.abspath(settings.mineimator_path) if settings.mineimator_path else "",
        asset_pack_path=bpy.path.abspath(settings.asset_pack_path) if settings.asset_pack_path else "",
        mineways_path=bpy.path.abspath(settings.mineways_path) if settings.mineways_path else "",
        use_mcprep=settings.use_mcprep,
        honor_item_keyframe_changes=settings.honor_item_keyframe_changes,
        remove_startup_cube=settings.remove_startup_cube,
        mineimator_suite=settings.mineimator_suite,
    )


def _popup(context: bpy.types.Context, title: str, lines: list[str], icon: str = "INFO") -> None:
    def draw(self, _context):
        for line in lines[:12]:
            self.layout.label(text=line[:180])
    context.window_manager.popup_menu(draw, title=title, icon=icon)


class MIBRIDGE_OT_preflight(Operator):
    bl_idname = "mibridge.preflight"
    bl_label = "Preflight Mine-imator Project"
    bl_description = "Validate the project, assets, and optional tools without modifying the scene"

    def execute(self, context):
        settings = context.scene.mi_bridge
        try:
            report = preflight(bpy.path.abspath(settings.project_path), _options(settings))
            write_report(report)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            _popup(context, "Mine-imator preflight failed", [str(exc)], "ERROR")
            return {"CANCELLED"}
        summary = [
            f"Found {sum(report.imported.values())} visible root timelines",
            f"Missing/setup items: {len(report.missing)}",
            "Full details: Text Editor > Mine-imator Bridge Report",
        ]
        _popup(context, "Mine-imator preflight complete", summary, "CHECKMARK")
        self.report({"INFO"}, summary[0])
        return {"FINISHED"}


class MIBRIDGE_OT_import_scene(Operator):
    bl_idname = "mibridge.import_scene"
    bl_label = "Import Mine-imator Scene"
    bl_description = "Import only the frame-zero static state into a new collection"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.mi_bridge
        try:
            project = core.load_project(bpy.path.abspath(settings.project_path))
            report = SceneImporter(context, project, _options(settings)).import_scene()
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            _popup(context, "Mine-imator import failed", [str(exc), "See Blender's system console for details."], "ERROR")
            return {"CANCELLED"}
        summary = [
            f"Created: {report.collection_name}",
            f"Accounted objects: {sum(report.imported.values())}",
            f"Placeholders: {len(report.placeholders)}; missing: {len(report.missing)}",
            "Full details: Text Editor > Mine-imator Bridge Report",
        ]
        _popup(context, "Mine-imator import complete", summary, "CHECKMARK")
        self.report({"INFO"}, summary[0])
        return {"FINISHED"}


class MIBRIDGE_OT_import_file(Operator, ImportHelper):
    bl_idname = "import_scene.mineimator_project"
    bl_label = "Import Mine-imator Project"
    bl_description = "Choose a .miproject file, or paste a Mine-imator project directory path"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".miproject"
    filter_glob: StringProperty(default="*.miproject", options={"HIDDEN"})

    def execute(self, context):
        context.scene.mi_bridge.project_path = self.filepath
        return bpy.ops.mibridge.import_scene()


class MIBRIDGE_OT_reload_environment(Operator):
    bl_idname = "mibridge.reload_environment"
    bl_label = "Reload Imported Values"
    bl_description = "Restore the active Mine-imator Suite environment to the values saved in the imported project"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not suite_environment.reload_environment(context.scene):
            self.report({"ERROR"}, "No active Mine-imator Suite environment")
            return {"CANCELLED"}
        self.report({"INFO"}, "Reloaded Mine-imator environment values")
        return {"FINISHED"}


def _draw_settings(layout: bpy.types.UILayout, settings: MIBRIDGE_PG_settings) -> None:
    layout.prop(settings, "project_path")
    suite = layout.box()
    suite.prop(settings, "mineimator_suite", icon="WORLD")
    if settings.mineimator_suite:
        suite.label(text="Full sky, ground and live environment controls", icon="CHECKMARK")
    tools = layout.box()
    tools.label(text="Tools and assets")
    tools.prop(settings, "mineimator_path")
    tools.prop(settings, "asset_pack_path")
    tools.prop(settings, "mineways_path")
    categories = layout.box()
    categories.label(text="Include")
    for prop in (
        "import_characters", "import_items", "import_blocks", "import_models",
        "import_scenery", "import_primitives", "import_lights", "import_camera",
        "import_environment", "import_helpers",
    ):
        row = categories.row()
        row.enabled = not (prop == "import_environment" and settings.mineimator_suite)
        row.prop(settings, prop)
    layout.prop(settings, "use_mcprep")
    layout.prop(settings, "honor_item_keyframe_changes")
    layout.prop(settings, "remove_startup_cube")
    row = layout.row(align=True)
    row.operator("mibridge.preflight", icon="CHECKMARK")
    row.operator("mibridge.import_scene", icon="IMPORT")


class MIBRIDGE_PT_panel(Panel):
    bl_label = "Mine-imator Bridge"
    bl_idname = "MIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"

    def draw(self, context):
        _draw_settings(self.layout, context.scene.mi_bridge)


class MIBRIDGE_PT_environment(Panel):
    bl_label = "MI Environment"
    bl_idname = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"

    @classmethod
    def poll(cls, context):
        return hasattr(context.scene, "mi_environment") and context.scene.mi_environment.active

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.label(text=f"Active: {env.suite_id}", icon="WORLD")
        row = layout.row(align=True)
        row.prop(env, "time_hours", slider=True)
        row.prop(env, "sky_rotation")
        layout.operator("mibridge.reload_environment", icon="FILE_REFRESH")


class MIBRIDGE_PT_environment_sunlight(Panel):
    bl_label = "Sunlight"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        env = context.scene.mi_environment
        self.layout.prop(env, "sunlight_angle")
        self.layout.prop(env, "sunlight_strength")
        self.layout.prop(env, "sunlight_color")


class MIBRIDGE_PT_environment_sky(Panel):
    bl_label = "Sky background"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.prop(env, "sky_mode", expand=True)
        if env.sky_mode == "CUSTOM":
            layout.prop(env, "background_image")
            layout.prop(env, "background_type")
            layout.prop(env, "background_stretch")
            layout.prop(env, "background_rotation")
            if env.background_type == "BOX":
                layout.prop(env, "background_box_mapped")
        layout.separator()
        layout.prop(env, "sun_texture")
        row = layout.row(align=True)
        row.prop(env, "sun_angle")
        row.prop(env, "sun_size")
        layout.prop(env, "moon_texture")
        layout.prop(env, "moon_phase")
        row = layout.row(align=True)
        row.prop(env, "moon_angle")
        row.prop(env, "moon_size")


class MIBRIDGE_PT_environment_clouds(Panel):
    bl_label = "Clouds"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.mi_environment, "clouds_show", text="")

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.enabled = env.clouds_show
        layout.prop(env, "clouds_mode", expand=True)
        layout.prop(env, "clouds_texture")
        layout.prop(env, "clouds_speed")
        layout.label(text="Speed is preserved; environment remains static", icon="INFO")
        for prop in ("clouds_offset", "clouds_height", "clouds_size", "clouds_thickness"):
            layout.prop(env, prop)


class MIBRIDGE_PT_environment_ground(Panel):
    bl_label = "Ground and biome"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"

    def draw_header(self, context):
        self.layout.prop(context.scene.mi_environment, "ground_show", text="")

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.prop(env, "ground_texture")
        layout.prop(env, "biome")
        if env.biome == "custom":
            box = layout.box()
            box.label(text="Biome colors")
            for prop in ("grass_color", "foliage_color", "water_color"):
                box.prop(env, prop)
            leaves = layout.box()
            leaves.label(text="Leaf colors")
            for prop in ("leaves_oak_color", "leaves_spruce_color", "leaves_birch_color", "leaves_jungle_color", "leaves_acacia_color", "leaves_dark_oak_color", "leaves_mangrove_color"):
                leaves.prop(env, prop)


class MIBRIDGE_PT_environment_colors(Panel):
    bl_label = "Scene colors"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        env = context.scene.mi_environment
        for prop in ("sky_color", "sky_clouds_color", "sunlight_color", "ambient_color", "night_color"):
            self.layout.prop(env, prop)
        self.layout.prop(env, "twilight")


class MIBRIDGE_PT_environment_fog(Panel):
    bl_label = "Fog"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.mi_environment, "fog_show", text="")

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.enabled = env.fog_show
        layout.prop(env, "fog_sky")
        layout.prop(env, "fog_color_custom")
        if env.fog_color_custom:
            layout.prop(env, "fog_color")
        layout.prop(env, "fog_object_color_custom")
        if env.fog_object_color_custom:
            layout.prop(env, "fog_object_color")
        for prop in ("fog_distance", "fog_size", "fog_height"):
            layout.prop(env, prop)


class MIBRIDGE_PT_environment_wind(Panel):
    bl_label = "Wind and texture motion"
    bl_parent_id = "MIBRIDGE_PT_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.mi_environment, "wind", text="")

    def draw(self, context):
        env = context.scene.mi_environment
        layout = self.layout
        layout.label(text="Stored static values — no animation", icon="INFO")
        column = layout.column()
        column.enabled = env.wind
        for prop in ("wind_speed", "wind_strength", "wind_direction", "wind_directional_speed", "wind_directional_strength"):
            column.prop(env, prop)
        layout.prop(env, "texture_animation_speed")


def _menu_import(self, _context):
    self.layout.operator(MIBRIDGE_OT_import_file.bl_idname, text="Mine-imator Project (.miproject)")


CLASSES = (
    MIBRIDGE_PG_environment,
    MIBRIDGE_PG_settings,
    MIBRIDGE_OT_preflight,
    MIBRIDGE_OT_import_scene,
    MIBRIDGE_OT_import_file,
    MIBRIDGE_OT_reload_environment,
    MIBRIDGE_PT_panel,
    MIBRIDGE_PT_environment,
    MIBRIDGE_PT_environment_sunlight,
    MIBRIDGE_PT_environment_sky,
    MIBRIDGE_PT_environment_clouds,
    MIBRIDGE_PT_environment_ground,
    MIBRIDGE_PT_environment_colors,
    MIBRIDGE_PT_environment_fog,
    MIBRIDGE_PT_environment_wind,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mi_bridge = PointerProperty(type=MIBRIDGE_PG_settings)
    bpy.types.Scene.mi_environment = PointerProperty(type=MIBRIDGE_PG_environment)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    render_export.register()


def unregister():
    render_export.unregister()
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    del bpy.types.Scene.mi_bridge
    del bpy.types.Scene.mi_environment
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
