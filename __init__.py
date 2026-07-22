"""Mine-imator to Blender/MCprep Static Scene Bridge."""

from __future__ import annotations

bl_info = {
    "name": "Mine-imator MCprep Bridge",
    "author": "Mine-imator MCprep Bridge contributors",
    "version": (0, 1, 2),
    "blender": (5, 2, 0),
    "location": "File > Import; 3D View > Sidebar > MI Bridge",
    "description": "Import frame-zero Mine-imator scenes with editable Minecraft geometry",
    "category": "Import-Export",
}

import traceback
from pathlib import Path

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from . import core
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
    return ImportOptions(
        categories=categories,
        mineimator_path=bpy.path.abspath(settings.mineimator_path) if settings.mineimator_path else "",
        asset_pack_path=bpy.path.abspath(settings.asset_pack_path) if settings.asset_pack_path else "",
        mineways_path=bpy.path.abspath(settings.mineways_path) if settings.mineways_path else "",
        use_mcprep=settings.use_mcprep,
        honor_item_keyframe_changes=settings.honor_item_keyframe_changes,
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


def _draw_settings(layout: bpy.types.UILayout, settings: MIBRIDGE_PG_settings) -> None:
    layout.prop(settings, "project_path")
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
        categories.prop(settings, prop)
    layout.prop(settings, "use_mcprep")
    layout.prop(settings, "honor_item_keyframe_changes")
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


def _menu_import(self, _context):
    self.layout.operator(MIBRIDGE_OT_import_file.bl_idname, text="Mine-imator Project (.miproject)")


CLASSES = (
    MIBRIDGE_PG_settings,
    MIBRIDGE_OT_preflight,
    MIBRIDGE_OT_import_scene,
    MIBRIDGE_OT_import_file,
    MIBRIDGE_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mi_bridge = PointerProperty(type=MIBRIDGE_PG_settings)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    del bpy.types.Scene.mi_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
