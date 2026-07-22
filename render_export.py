"""Compact still-image export integrated with the MI Bridge add-on."""

from __future__ import annotations

import os
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup


SIZE_PRESETS = {
    "AVATAR": (512, 512),
    "HD": (1280, 720),
    "FHD": (1920, 1080),
    "QHD": (2560, 1440),
    "UHD": (3840, 2160),
    "HD_CINEMA": (1680, 720),
    "FHD_CINEMA": (2560, 1080),
    "QHD_CINEMA": (3440, 1440),
    "UHD_CINEMA": (5120, 2160),
}


def camera_poll(_self, obj):
    return obj is not None and obj.type == "CAMERA"


class MIBRIDGE_PG_render_export(PropertyGroup):
    image_size: EnumProperty(
        name="Image size",
        items=(
            ("AVATAR", "Avatar (512x512)", "Square avatar image"),
            ("HD", "HD 720p (1280x720)", "16:9 HD image"),
            ("FHD", "FHD 1080p (1920x1080)", "16:9 Full HD image"),
            ("QHD", "QHD 1440p (2560x1440)", "16:9 QHD image"),
            ("UHD", "UHD 4K (3840x2160)", "16:9 UHD image"),
            ("HD_CINEMA", "HD 720p Cinematic (1680x720)", "Cinematic HD image"),
            ("FHD_CINEMA", "FHD 1080p Cinematic (2560x1080)", "Cinematic Full HD image"),
            ("QHD_CINEMA", "QHD 1440p Cinematic (3440x1440)", "Cinematic QHD image"),
            ("UHD_CINEMA", "UHD 4K Cinematic (5120x2160)", "Cinematic UHD image"),
            ("CUSTOM", "Custom", "Choose a custom width and height"),
        ),
        default="UHD",
    )
    custom_width: IntProperty(name="Width", default=1920, min=1, max=65536)
    custom_height: IntProperty(name="Height", default=1080, min=1, max=65536)
    remove_background: BoolProperty(
        name="Remove background",
        description="Save an RGBA PNG with a transparent World background",
        default=False,
    )
    include_hidden_objects: BoolProperty(
        name="Include hidden objects",
        description="Temporarily render objects and collections whose render visibility is disabled",
        default=False,
    )
    camera: PointerProperty(name="Camera", type=bpy.types.Object, poll=camera_poll)


def _draw_settings(layout, settings, *, show_save: bool) -> None:
    layout.prop(settings, "image_size")
    if settings.image_size == "CUSTOM":
        row = layout.row(align=True)
        row.prop(settings, "custom_width")
        row.prop(settings, "custom_height")
    layout.prop(settings, "camera")
    layout.prop(settings, "remove_background")
    layout.prop(settings, "include_hidden_objects")
    if show_save:
        layout.operator(MIBRIDGE_OT_render_export_save.bl_idname, text="Render and Save PNG", icon="RENDER_STILL")


class MIBRIDGE_OT_render_export_save(Operator):
    bl_idname = "mibridge.render_export_save"
    bl_label = "Render and Save PNG"
    bl_description = "Choose a PNG file, render the current frame, and save it"

    filepath: StringProperty(name="File Path", subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.png", options={"HIDDEN"})

    def invoke(self, context, _event):
        settings = context.scene.mi_render_export
        if settings.camera is None:
            settings.camera = context.scene.camera
        if settings.camera is None:
            self.report({"ERROR"}, "Choose a camera before saving")
            return {"CANCELLED"}

        blend_path = Path(bpy.data.filepath) if bpy.data.filepath else None
        base_name = bpy.path.clean_name(blend_path.stem) if blend_path else "render"
        self.filepath = os.path.join(bpy.path.abspath("//"), f"{base_name}.png")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        scene = context.scene
        settings = scene.mi_render_export
        camera = settings.camera
        if camera is None or camera.type != "CAMERA":
            self.report({"ERROR"}, "The selected camera is no longer available")
            return {"CANCELLED"}

        filepath = bpy.path.ensure_ext(self.filepath, ".png")
        if settings.image_size == "CUSTOM":
            width, height = settings.custom_width, settings.custom_height
        else:
            width, height = SIZE_PRESETS[settings.image_size]

        render = scene.render
        original = {
            "camera": scene.camera,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "resolution_percentage": render.resolution_percentage,
            "film_transparent": render.film_transparent,
            "filepath": render.filepath,
            "file_format": render.image_settings.file_format,
            "color_mode": render.image_settings.color_mode,
        }
        hidden_objects = []
        hidden_collections = []
        if settings.include_hidden_objects:
            hidden_objects = [(obj, obj.hide_render) for obj in bpy.data.objects if obj.hide_render]
            hidden_collections = [
                (collection, collection.hide_render)
                for collection in bpy.data.collections
                if collection.hide_render
            ]

        try:
            scene.camera = camera
            render.resolution_x = width
            render.resolution_y = height
            render.resolution_percentage = 100
            render.film_transparent = settings.remove_background
            render.filepath = filepath
            render.image_settings.file_format = "PNG"
            render.image_settings.color_mode = "RGBA" if settings.remove_background else "RGB"
            for obj, _state in hidden_objects:
                obj.hide_render = False
            for collection, _state in hidden_collections:
                collection.hide_render = False
            bpy.ops.render.render(write_still=True)
        except Exception as exc:
            self.report({"ERROR"}, f"Render failed: {exc}")
            return {"CANCELLED"}
        finally:
            scene.camera = original["camera"]
            render.resolution_x = original["resolution_x"]
            render.resolution_y = original["resolution_y"]
            render.resolution_percentage = original["resolution_percentage"]
            render.film_transparent = original["film_transparent"]
            render.filepath = original["filepath"]
            render.image_settings.file_format = original["file_format"]
            render.image_settings.color_mode = original["color_mode"]
            for obj, state in hidden_objects:
                obj.hide_render = state
            for collection, state in hidden_collections:
                collection.hide_render = state

        self.report({"INFO"}, f"Saved render to {filepath}")
        return {"FINISHED"}


class MIBRIDGE_OT_render_export_dialog(Operator):
    bl_idname = "mibridge.render_export_dialog"
    bl_label = "Export Image"
    bl_description = "Open the MI Bridge still-image export menu"

    def invoke(self, context, _event):
        settings = context.scene.mi_render_export
        if settings.camera is None:
            settings.camera = context.scene.camera
        return context.window_manager.invoke_popup(self, width=360)

    def execute(self, _context):
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.label(text="Export Image", icon="IMAGE_DATA")
        layout.separator(factor=0.35)
        _draw_settings(layout, context.scene.mi_render_export, show_save=True)


class MIBRIDGE_PT_render_export(Panel):
    bl_label = "Render Export"
    bl_idname = "MIBRIDGE_PT_render_export"
    bl_parent_id = "MIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MI Bridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.mi_render_export
        _draw_settings(self.layout, settings, show_save=True)


def draw_file_menu(self, _context):
    self.layout.separator()
    self.layout.operator(MIBRIDGE_OT_render_export_dialog.bl_idname, text="Export Image...", icon="RENDER_STILL")


CLASSES = (
    MIBRIDGE_PG_render_export,
    MIBRIDGE_OT_render_export_save,
    MIBRIDGE_OT_render_export_dialog,
    MIBRIDGE_PT_render_export,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mi_render_export = PointerProperty(type=MIBRIDGE_PG_render_export)
    bpy.types.TOPBAR_MT_file.append(draw_file_menu)


def unregister():
    bpy.types.TOPBAR_MT_file.remove(draw_file_menu)
    del bpy.types.Scene.mi_render_export
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
