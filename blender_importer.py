"""Blender-side scene construction for the Mine-imator bridge."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import math
import os
import subprocess
import tempfile

import bpy
import bmesh
from mathutils import Matrix, Vector

from . import core


CATEGORY_NAMES = {
    "characters": "Characters & Entities",
    "items": "Items",
    "blocks": "Blocks & Special Blocks",
    "models": "Custom Models",
    "scenery": "World Scenery",
    "primitives": "Shapes & Text",
    "lights": "Lights",
    "camera": "Camera",
    "environment": "Environment",
    "helpers": "Paths & Helpers",
}

TYPE_CATEGORY = {
    "char": "characters",
    "entity": "characters",
    "item": "items",
    "block": "blocks",
    "spblock": "blocks",
    "model": "models",
    "scenery": "scenery",
    "cube": "primitives",
    "cone": "primitives",
    "cylinder": "primitives",
    "sphere": "primitives",
    "surface": "primitives",
    "text": "primitives",
    "pointlight": "lights",
    "spotlight": "lights",
    "spot_light": "lights",
    "camera": "camera",
    "background": "environment",
    "path": "helpers",
    "path_point": "helpers",
    "folder": "helpers",
    "particle_spawner": "helpers",
    "particle": "helpers",
}


@dataclass
class ImportOptions:
    categories: set[str] = field(default_factory=lambda: set(CATEGORY_NAMES))
    mineimator_path: str = ""
    asset_pack_path: str = ""
    mineways_path: str = ""
    use_mcprep: bool = True
    honor_item_keyframe_changes: bool = False


@dataclass
class ImportReport:
    project_path: str = ""
    collection_name: str = ""
    imported: Counter = field(default_factory=Counter)
    created: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    fallbacks: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            "Mine-imator MCprep Bridge report",
            f"Bridge version: {core.ADDON_VERSION}",
            f"Source: {self.project_path}",
            f"Collection: {self.collection_name or '(preflight)'}",
            "Animation: intentionally not imported (frame 0 only)",
            "",
            "Accounted root timelines:",
        ]
        if self.imported:
            lines.extend(f"  {key}: {value}" for key, value in sorted(self.imported.items()))
        else:
            lines.append("  none")
        if self.created:
            lines += ["", "Created datablocks:"]
            lines.extend(f"  {key}: {value}" for key, value in sorted(self.created.items()))
        for heading, values in (
            ("Fallbacks", self.fallbacks),
            ("Missing resources", self.missing),
            ("Placeholders", self.placeholders),
            ("Notes", self.notes),
        ):
            lines += ["", f"{heading} ({len(values)}):"]
            lines.extend(f"  - {value}" for value in values) if values else lines.append("  none")
        return "\n".join(lines)


def _hex_color(value: Any, alpha: float = 1.0) -> tuple[float, float, float, float]:
    raw = str(value or "#FFFFFF").lstrip("#")
    if len(raw) == 8:
        alpha = int(raw[6:8], 16) / 255.0
        raw = raw[:6]
    try:
        return tuple(int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4)) + (alpha,)
    except (ValueError, IndexError):
        return (1.0, 1.0, 1.0, alpha)


def _link_object(obj: bpy.types.Object, collection: bpy.types.Collection) -> None:
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def _new_empty(name: str, collection: bpy.types.Collection, display: str = "PLAIN_AXES") -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = display
    obj.empty_display_size = 0.25
    collection.objects.link(obj)
    return obj


def _metadata(obj: bpy.types.Object, project: core.ProjectIndex, timeline: dict[str, Any], template: dict[str, Any] | None = None, resource: Path | str | None = None) -> None:
    obj["mi_bridge_version"] = core.ADDON_VERSION
    obj["mi_source_project"] = str(project.path)
    obj["mi_id"] = str(timeline.get("id", ""))
    obj["mi_type"] = str(timeline.get("type", ""))
    obj["mi_template"] = str(timeline.get("temp", "null"))
    if template:
        obj["mi_template_type"] = str(template.get("type", ""))
    if resource:
        obj["mi_resource_path"] = str(resource)


def _apply_transform(obj: bpy.types.Object, state: dict[str, Any], include_bend: bool = False) -> None:
    location = core.mi_position(state)
    rotation = _engine_rotation_matrix(state)
    if include_bend:
        rotation = rotation @ _engine_rotation_matrix({
            "ROT_X": state.get("BEND_ANGLE_X", 0.0),
            "ROT_Y": state.get("BEND_ANGLE_Y", 0.0),
            "ROT_Z": state.get("BEND_ANGLE_Z", 0.0),
        })
    obj.matrix_basis = _transform_matrix(location, rotation, core.mi_scale(state))
    visible = bool(state.get("VISIBLE", True)) and float(state.get("ALPHA", 1.0)) > 0.0
    obj.hide_viewport = not visible
    obj.hide_render = not visible


def _engine_rotation_matrix(state: dict[str, Any]) -> Matrix:
    """Reproduce GameMaker's Z-X-Y Mine-imator rotation composition.

    Matrix::Rotation composes the engine rotation as Y @ X @ Z after accounting
    for Mine-imator's column-major storage.  matrix_build then transposes it.
    Reflecting engine Y into Blender Y produces Z @ X @ -Y.  Treating these
    values as an ordinary XYZ Euler breaks mixed-axis limb poses.
    """
    rx = math.radians(float(state.get("ROT_X", 0.0)))
    ry = math.radians(float(state.get("ROT_Y", 0.0)))
    rz = math.radians(float(state.get("ROT_Z", 0.0)))
    return (
        Matrix.Rotation(rz, 4, "Z")
        @ Matrix.Rotation(rx, 4, "X")
        @ Matrix.Rotation(-ry, 4, "Y")
    )


def _mimodel_rotation_matrix(value: Any) -> Matrix:
    values = list(value) if isinstance(value, (list, tuple)) else [0.0, 0.0, 0.0]
    values = (values + [0.0, 0.0, 0.0])[:3]
    # .mimodel stores Y-up X/Y/Z; value_get_point3D swaps JSON Y/Z into the
    # engine's Z-up axes before matrix_build is called.
    return _engine_rotation_matrix({"ROT_X": values[0], "ROT_Y": values[2], "ROT_Z": values[1]})


def _transform_matrix(location: Any, rotation: Matrix, scale: Any) -> Matrix:
    scale_values = list(scale)
    return (
        Matrix.Translation(Vector(location))
        @ rotation
        @ Matrix.Diagonal((float(scale_values[0]), float(scale_values[1]), float(scale_values[2]), 1.0))
    )


def _new_material(name: str, image_path: Path | None = None, color: tuple[float, float, float, float] = (1, 1, 1, 1)) -> bpy.types.Material:
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    material.diffuse_color = color
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = nodes.get("Principled BSDF")
    if principled:
        principled.inputs["Base Color"].default_value = color
        principled.inputs["Alpha"].default_value = color[3]
        principled.inputs["Roughness"].default_value = 0.8
    if image_path and image_path.is_file() and principled:
        try:
            image = bpy.data.images.load(str(image_path), check_existing=True)
            image.colorspace_settings.name = "sRGB"
            texture = nodes.new("ShaderNodeTexImage")
            texture.image = image
            texture.interpolation = "Closest"
            links.new(texture.outputs["Color"], principled.inputs["Base Color"])
            links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
            material.diffuse_color = (1, 1, 1, color[3])
        except Exception:
            pass
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = False
    material["mi_pixel_art"] = True
    return material


def _placeholder(name: str, message: str, collection: bpy.types.Collection, project: core.ProjectIndex, timeline: dict[str, Any], report: ImportReport) -> bpy.types.Object:
    size = 0.5
    vertices = [
        (-size, -size, -size), (size, -size, -size), (size, size, -size), (-size, size, -size),
        (-size, -size, size), (size, -size, size), (size, size, size), (-size, size, size),
    ]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)]
    mesh = bpy.data.meshes.new(f"{name} Placeholder Mesh")
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(f"MISSING - {name}", mesh)
    collection.objects.link(obj)
    obj.data.materials.append(_new_material("MI Missing Resource", color=(1.0, 0.0, 1.0, 1.0)))
    obj["mi_placeholder_reason"] = message
    obj.show_name = True
    _apply_transform(obj, core.frame0_state(timeline))
    _metadata(obj, project, timeline)
    report.placeholders.append(f"{name}: {message}")
    report.created["placeholder"] += 1
    return obj


def _box_geometry(shape: dict[str, Any], texture_size: tuple[float, float]) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[tuple[float, float]]]:
    start = list(shape.get("from", [-8, -8, -8]))
    end = list(shape.get("to", [8, 8, 8]))
    while len(start) < 3: start.append(0)
    while len(end) < 3: end.append(0)
    inflate = float(shape.get("inflate", 0.0))
    # Shape position is its transform origin, not part of the vertex geometry.
    # Baking it here makes the position rotate around the parent part origin and
    # scatters multi-shape custom models when a shape has its own rotation.
    lo = [min(float(start[i]), float(end[i])) - inflate for i in range(3)]
    hi = [max(float(start[i]), float(end[i])) + inflate for i in range(3)]
    mi_vertices = [
        (lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
        (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]), (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2]),
    ]
    vertices = [core.mi_vector(vertex) for vertex in mi_vertices]
    faces = [(0, 3, 2, 1), (5, 6, 7, 4), (4, 7, 3, 0), (1, 2, 6, 5), (3, 7, 6, 2), (4, 0, 1, 5)]
    u0, v0 = [float(x) for x in (list(shape.get("uv", [0, 0])) + [0, 0])[:2]]
    # UV dimensions deliberately exclude inflation and shape scale, matching
    # Mine-imator's to_noscale/from_noscale calculation.
    width = abs(float(end[0]) - float(start[0]))
    height = abs(float(end[1]) - float(start[1]))
    depth = abs(float(end[2]) - float(start[2]))
    tex_w, tex_h = texture_size
    points = {
        "south": [(u0, v0), (u0 + width, v0), (u0 + width, v0 + height), (u0, v0 + height)],
        "east": [(u0 + width, v0), (u0 + width + depth, v0), (u0 + width + depth, v0 + height), (u0 + width, v0 + height)],
        "west": [(u0 - depth, v0), (u0, v0), (u0, v0 + height), (u0 - depth, v0 + height)],
        "north": [(u0 + width + depth, v0), (u0 + width + depth + width, v0), (u0 + width + depth + width, v0 + height), (u0 + width + depth, v0 + height)],
        "up": [(u0, v0 - depth), (u0 + width, v0 - depth), (u0 + width, v0), (u0, v0)],
        # Mine-imator flips the down face vertically.
        "down": [(u0 + width, v0), (u0 + width * 2, v0), (u0 + width * 2, v0 - depth), (u0 + width, v0 - depth)],
    }
    mirror = bool(shape.get("texture_mirror", False))
    if mirror:
        points["east"], points["west"] = points["west"], points["east"]
        for key in points:
            p1, p2, p3, p4 = points[key]
            points[key] = [p2, p1, p4, p3]
    # Order each face's UVs to match the loop ordering in `faces` above.
    loop_points = [
        [points["north"][2], points["north"][1], points["north"][0], points["north"][3]],
        [points["south"][2], points["south"][1], points["south"][0], points["south"][3]],
        [points["west"][2], points["west"][1], points["west"][0], points["west"][3]],
        [points["east"][2], points["east"][1], points["east"][0], points["east"][3]],
        [points["up"][0], points["up"][3], points["up"][2], points["up"][1]],
        [points["down"][3], points["down"][0], points["down"][1], points["down"][2]],
    ]
    uv = [(x / tex_w, 1.0 - y / tex_h) for face_points in loop_points for x, y in face_points]
    return vertices, faces, uv


def _plane_geometry(shape: dict[str, Any], texture_size: tuple[float, float]) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[tuple[float, float]]]:
    start = list(shape.get("from", [-8, 0, -8]))
    end = list(shape.get("to", [8, 0, 8]))
    while len(start) < 3: start.append(0)
    while len(end) < 3: end.append(0)
    x0, x1 = sorted((float(start[0]), float(end[0])))
    y = float(start[1])
    z0, z1 = sorted((float(start[2]), float(end[2])))
    vertices = [core.mi_vector(value) for value in ((x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1))]
    faces = [(0, 1, 2, 3)]
    if not shape.get("hide_back", False):
        faces.append((3, 2, 1, 0))
    u0, v0 = [float(x) for x in (list(shape.get("uv", [0, 0])) + [0, 0])[:2]]
    w = abs(x1 - x0)
    h = abs(z1 - z0)
    tw, th = texture_size
    quad = [(u0 / tw, 1 - (v0 + h) / th), ((u0 + w) / tw, 1 - (v0 + h) / th), ((u0 + w) / tw, 1 - v0 / th), (u0 / tw, 1 - v0 / th)]
    return vertices, faces, quad * len(faces)


def _resolve_texture(name: Any, model_dir: Path | None, project: core.ProjectIndex, assets: core.AssetStore | None) -> Path | None:
    if core._is_null(name):
        return None
    raw = str(name)
    direct = Path(raw.replace("/", os.sep))
    candidates = []
    if direct.is_absolute():
        candidates.append(direct)
    if model_dir:
        candidates.extend([model_dir / direct, model_dir / direct.name])
    candidates.extend([project.project_dir / direct, project.project_dir / direct.name])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    if assets:
        asset_name = raw
        if not Path(asset_name).suffix:
            asset_name = f"textures/{asset_name}.png"
        resolved = assets.materialize(asset_name)
        if resolved:
            return resolved
    return _mcprep_texture(raw)


def _mcprep_texture(name: str) -> Path | None:
    """Last-resort lookup in MCprep's bundled default resource pack."""
    raw = str(name).replace("minecraft:", "").replace("\\", "/")
    raw = raw.removeprefix("textures/")
    if not Path(raw).suffix:
        raw += ".png"
    candidates: list[Path] = []
    scripts = Path(bpy.utils.user_resource("SCRIPTS"))
    addon_roots = [scripts / "addons" / "MCprep_addon", scripts / "addons_core" / "MCprep_addon"]
    for addon_root in addon_roots:
        pack = addon_root / "MCprep_resources" / "resourcepacks" / "mcprep_default" / "assets" / "minecraft" / "textures"
        candidates.extend([pack / raw, pack / Path(raw).name])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _create_model_shape(name: str, shape: dict[str, Any], texture_size: tuple[float, float], material: bpy.types.Material, collection: bpy.types.Collection) -> bpy.types.Object:
    shape_type = str(shape.get("type", "block")).lower()
    geometry = _plane_geometry(shape, texture_size) if shape_type == "plane" else _box_geometry(shape, texture_size)
    vertices, faces, uvs = geometry
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.materials.append(material)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(mesh.loops, uvs):
        uv_layer.data[loop.index].uv = uv
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    rotation = _mimodel_rotation_matrix(shape.get("rotation", [0, 0, 0]))
    location = core.mi_vector(shape.get("position", [0, 0, 0]))
    scale = list(shape.get("scale", [1, 1, 1]))
    while len(scale) < 3: scale.append(1)
    obj.matrix_basis = _transform_matrix(location, rotation, (float(scale[0]), float(scale[2]), float(scale[1])))
    return obj


def _build_mimodel(model: dict[str, Any], model_path: Path | None, root: bpy.types.Object, collection: bpy.types.Collection, project: core.ProjectIndex, assets: core.AssetStore | None, texture_override: Path | None, report: ImportReport) -> dict[str, list[bpy.types.Object]]:
    part_map: dict[str, list[bpy.types.Object]] = defaultdict(list)
    base_size = model.get("texture_size", [64, 64])
    if not isinstance(base_size, list) or len(base_size) < 2:
        base_size = [64, 64]
    material_cache: dict[str, bpy.types.Material] = {}

    def material_for(texture: Any, color: Any, alpha: float) -> bpy.types.Material:
        texture_path = texture_override or _resolve_texture(texture, model_path.parent if model_path else None, project, assets)
        key = f"{texture_path}|{color}|{alpha}"
        if key not in material_cache:
            material_cache[key] = _new_material(f"MI {Path(str(texture_path)).stem if texture_path else 'Color'}", texture_path, _hex_color(color, alpha))
        return material_cache[key]

    def visit(parts: list[dict[str, Any]], parent: bpy.types.Object, inherited_texture: Any, inherited_size: list[float], path: str) -> None:
        for part_index, part in enumerate(parts):
            part_name = str(part.get("name") or f"part_{part_index}")
            pivot = _new_empty(part_name, collection)
            pivot.parent = parent
            location = core.mi_vector(part.get("position", [0, 0, 0]))
            rotation = _mimodel_rotation_matrix(part.get("rotation", [0, 0, 0]))
            scale = list(part.get("scale", [1, 1, 1]))
            while len(scale) < 3: scale.append(1)
            pivot.matrix_basis = _transform_matrix(location, rotation, (scale[0], scale[2], scale[1]))
            pivot["mi_model_part"] = part_name
            pivot["mi_model_part_path"] = f"{path}/{part_name}"
            bend = part.get("bend")
            if bend:
                pivot["mi_bend_definition"] = json.dumps(bend, separators=(",", ":"))
            part_map[part_name].append(pivot)
            part_texture = part.get("texture", inherited_texture)
            part_size = part.get("texture_size", inherited_size)
            if not isinstance(part_size, list) or len(part_size) < 2:
                part_size = inherited_size
            for shape_index, shape in enumerate(part.get("shapes", [])):
                shape_name = str(shape.get("description") or f"{part_name}_shape_{shape_index + 1}")
                alpha = float(shape.get("color_alpha", 1.0))
                texture = shape.get("texture", part_texture)
                shape_size = shape.get("texture_size", part_size)
                material = material_for(texture, shape.get("color", "#FFFFFF"), alpha)
                obj = _create_model_shape(shape_name, shape, tuple(shape_size[:2]), material, collection)
                obj.parent = pivot
                obj.hide_render = alpha <= 0
                report.created[f"model_{str(shape.get('type', 'block')).lower()}"] += 1
            visit(part.get("parts", []), pivot, part_texture, part_size, f"{path}/{part_name}")

    visit(model.get("parts", []), root, model.get("texture", "default"), base_size, "")
    return part_map


def _apply_model_bend(part_obj: bpy.types.Object, state: dict[str, Any]) -> None:
    """Apply Mine-imator's static bend at the model part's configured pivot.

    Blocky and realistic projects share the same pivot/axis solution at frame
    zero; realistic interpolation is approximated by the mesh face between the
    stationary and rotated vertex sets, without adding animation modifiers.
    """
    raw = part_obj.get("mi_bend_definition")
    if not raw:
        return
    try:
        definition = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return
    angles = {
        "ROT_X": float(state.get("BEND_ANGLE_X", 0.0)),
        "ROT_Y": float(state.get("BEND_ANGLE_Y", 0.0)),
        "ROT_Z": float(state.get("BEND_ANGLE_Z", 0.0)),
    }
    allowed = definition.get("axis", ["x", "y", "z"])
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed = [str(axis).lower() for axis in allowed]

    def axis_setting(name: str, axis: str, default: float) -> float:
        value = definition.get(name, default)
        if isinstance(value, (list, tuple)):
            try:
                index = allowed.index(axis)
            except ValueError:
                return default
            value = value[index] if index < len(value) else default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def axis_inverted(axis: str) -> bool:
        value = definition.get("invert", False)
        if isinstance(value, (list, tuple)):
            try:
                index = allowed.index(axis)
            except ValueError:
                return False
            return bool(value[index]) if index < len(value) else False
        return bool(value)

    # The .mimodel JSON axes are Y-up, so JSON z controls engine Y and JSON y
    # controls engine Z.
    for key, axis in (("ROT_X", "x"), ("ROT_Y", "z"), ("ROT_Z", "y")):
        if axis not in allowed:
            angles[key] = 0.0
            continue
        angles[key] = min(
            axis_setting("direction_max", axis, 180.0),
            max(axis_setting("direction_min", axis, -180.0), angles[key]),
        )
        if axis_inverted(axis):
            angles[key] = -angles[key]
    if not any(abs(value) > 1e-8 for value in angles.values()):
        return
    rotation = _engine_rotation_matrix(angles).to_3x3()
    offset = float(definition.get("offset", 0.0)) / core.MI_UNITS_PER_BLOCK
    bend_part = str(definition.get("part", "lower")).lower()
    if bend_part in {"upper", "lower"}:
        axis_index, pivot_value = 2, offset
        positive = bend_part == "upper"
    elif bend_part in {"right", "left"}:
        axis_index, pivot_value = 0, offset
        positive = bend_part == "right"
    else:
        axis_index, pivot_value = 1, -offset
        positive = bend_part == "back"
    pivot = Vector((0.0, 0.0, 0.0))
    pivot[axis_index] = pivot_value

    def selected(co: Vector) -> bool:
        return co[axis_index] >= pivot_value if positive else co[axis_index] <= pivot_value

    for child in part_obj.children:
        if child.type == "MESH":
            child_matrix = child.matrix_basis.copy()
            child_inverse = child_matrix.inverted()
            axis = Vector((0.0, 0.0, 0.0))
            axis[axis_index] = 1.0
            local_plane_co = child_inverse @ pivot
            local_plane_no = (child_matrix.to_3x3().transposed() @ axis).normalized()
            bm = bmesh.new()
            bm.from_mesh(child.data)
            bmesh.ops.bisect_plane(
                bm,
                geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                plane_co=local_plane_co,
                plane_no=local_plane_no,
                clear_inner=False,
                clear_outer=False,
                dist=1e-6,
            )
            bm.to_mesh(child.data)
            bm.free()
            for vertex in child.data.vertices:
                part_co = child_matrix @ vertex.co
                if selected(part_co):
                    vertex.co = child_inverse @ (pivot + rotation @ (part_co - pivot))
            child.data.update()
        elif child.type == "EMPTY" and selected(child.location):
            bend_matrix = Matrix.Translation(pivot) @ rotation.to_4x4() @ Matrix.Translation(-pivot)
            child.matrix_basis = bend_matrix @ child.matrix_basis


def _model_definition(template: dict[str, Any], project: core.ProjectIndex, assets: core.AssetStore | None) -> tuple[dict[str, Any] | None, Path | None, Path | None, str | None]:
    template_type = str(template.get("type", ""))
    texture_override = None
    texture_resource = project.resource(template.get("model_tex"))
    if texture_resource:
        texture_override = core.project_resource_path(project, texture_resource)
    if template_type == "model":
        resource = project.resource(template.get("model"))
        path = core.project_resource_path(project, resource)
        if path:
            return core.load_mimodel(path), path, texture_override, None
        return None, path, texture_override, "custom .mimodel resource is missing"
    if not assets:
        return None, None, texture_override, "Mine-imator asset pack is unavailable"
    model_spec = template.get("model", {})
    if not isinstance(model_spec, dict):
        return None, None, texture_override, "template has no model definition"
    name = model_spec.get("name")
    category = "characters" if template_type in {"char", "entity"} else "special_blocks"
    record = assets.find_record(category, name)
    if not record:
        return None, None, texture_override, f"asset definition not found for {name}"
    state = model_spec.get("state", {}) if isinstance(model_spec.get("state"), dict) else {}
    filename = record.get("file")
    for state_name, choices in record.get("states", {}).items():
        wanted = str(state.get(state_name, ""))
        for choice in choices:
            if str(choice.get("value")) == wanted and choice.get("file"):
                filename = choice["file"]
    if not filename:
        return None, None, texture_override, f"asset definition for {name} has no model file"
    model_path = assets.materialize(str(filename))
    if not model_path:
        return None, None, texture_override, f"model file {filename} is absent from asset pack"
    model = core.load_mimodel(model_path)
    if not texture_override:
        for state_name, choices in record.get("states", {}).items():
            wanted = str(state.get(state_name, ""))
            for choice in choices:
                if str(choice.get("value")) != wanted:
                    continue
                texture = choice.get("shape_texture")
                if isinstance(texture, dict):
                    texture = texture.get(str(state.get("type", "wide"))) or next(iter(texture.values()), None)
                if texture:
                    texture_override = assets.materialize(f"textures/{texture}.png")
    return model, model_path, texture_override, None


def _pixel_item(
    name: str,
    image_path: Path,
    collection: bpy.types.Collection,
    report: ImportReport,
    rotation_point: Any = (8.0, 0.0, 0.5),
) -> bpy.types.Object:
    """Build Mine-imator's one-unit item slab around its saved pivot.

    Blender exposes image pixels bottom-up.  Mine-imator's item buffer maps
    that bottom row to local Z=0 and uses a fixed one-MI-unit depth, regardless
    of texture resolution.  Building only boundary side faces avoids both the
    scrambled per-pixel UVs and the thousands of hidden internal faces from
    the previous cube-per-pixel approximation.
    """
    image = bpy.data.images.load(str(image_path), check_existing=True)
    width, height = image.size
    pixels = list(image.pixels[:])
    opaque: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            if pixels[(y * width + x) * 4 + 3] > 0.01:
                opaque.add((x, y))
    if not opaque:
        raise ValueError("item texture contains no visible pixels")
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int, int]] = []
    uvs: list[tuple[float, float]] = []
    point = list(rotation_point) if isinstance(rotation_point, (list, tuple)) else [8.0, 0.0, 0.5]
    point = (point + [0.0, 0.0, 0.5])[:3]
    pivot = Vector(core.mi_vector(point))
    size_x = 1.0
    size_z = height / width if width > height else 1.0
    if height > width:
        size_x = width / height
    front_y = -1.0 / core.MI_UNITS_PER_BLOCK - pivot.y
    back_y = -pivot.y

    def add_quad(coords: list[tuple[float, float, float]], face_uvs: list[tuple[float, float]]) -> None:
        base = len(vertices)
        vertices.extend(coords)
        faces.append((base, base + 1, base + 2, base + 3))
        uvs.extend(face_uvs)

    for x, y in sorted(opaque, key=lambda value: (value[1], value[0])):
        x0 = x / width * size_x - pivot.x
        x1 = (x + 1) / width * size_x - pivot.x
        z0 = y / height * size_z - pivot.z
        z1 = (y + 1) / height * size_z - pivot.z
        yf = front_y
        yb = back_y
        u0, u1 = x / width, (x + 1) / width
        v0, v1 = y / height, (y + 1) / height
        center_u, center_v = (u0 + u1) / 2.0, (v0 + v1) / 2.0

        add_quad([(x0, yf, z0), (x1, yf, z0), (x1, yf, z1), (x0, yf, z1)], [(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
        add_quad([(x1, yb, z0), (x0, yb, z0), (x0, yb, z1), (x1, yb, z1)], [(u1, v0), (u0, v0), (u0, v1), (u1, v1)])
        if (x - 1, y) not in opaque:
            add_quad([(x0, yb, z0), (x0, yf, z0), (x0, yf, z1), (x0, yb, z1)], [(center_u, v0), (center_u, v0), (center_u, v1), (center_u, v1)])
        if (x + 1, y) not in opaque:
            add_quad([(x1, yf, z0), (x1, yb, z0), (x1, yb, z1), (x1, yf, z1)], [(center_u, v0), (center_u, v0), (center_u, v1), (center_u, v1)])
        if (x, y - 1) not in opaque:
            add_quad([(x0, yf, z0), (x0, yb, z0), (x1, yb, z0), (x1, yf, z0)], [(u0, center_v), (u0, center_v), (u1, center_v), (u1, center_v)])
        if (x, y + 1) not in opaque:
            add_quad([(x0, yb, z1), (x0, yf, z1), (x1, yf, z1), (x1, yb, z1)], [(u0, center_v), (u0, center_v), (u1, center_v), (u1, center_v)])

    mesh = bpy.data.meshes.new(f"{name} Mesh")
    mesh.from_pydata(vertices, [], faces)
    material = _new_material(f"MI Item {image_path.stem}", image_path)
    mesh.materials.append(material)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(mesh.loops, uvs):
        uv_layer.data[loop.index].uv = uv
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    report.created["item_mesh"] += 1
    return obj


class SceneImporter:
    def __init__(self, context: bpy.types.Context, project: core.ProjectIndex, options: ImportOptions):
        self.context = context
        self.project = project
        self.options = options
        self.report = ImportReport(project_path=str(project.path))
        self.assets: core.AssetStore | None = None
        self.root_collection: bpy.types.Collection | None = None
        self.collections: dict[str, bpy.types.Collection] = {}
        self.timeline_objects: dict[str, bpy.types.Object] = {}

    def prepare_assets(self) -> None:
        install = core.find_mineimator_install(self.project.path, self.options.mineimator_path)
        asset_path = Path(self.options.asset_pack_path) if self.options.asset_pack_path else None
        if asset_path and asset_path.is_file() and asset_path.suffix.lower() == ".zip":
            metadata_path = asset_path.with_suffix(".midata")
            if metadata_path.is_file():
                try:
                    base = install or asset_path.parent
                    self.assets = core.AssetStore.open_paths(base, asset_path.stem, metadata_path, asset_path)
                    self.report.notes.append(f"Mine-imator assets: {self.assets.asset_version} ({self.assets.zip_path})")
                    return
                except core.BridgeError as exc:
                    self.report.missing.append(str(exc))
        if not install:
            self.report.missing.append("Mine-imator installation not found; bundled characters and special blocks will use placeholders")
            return
        try:
            self.assets = core.AssetStore.open(install)
            self.report.notes.append(f"Mine-imator assets: {self.assets.asset_version} ({self.assets.zip_path})")
        except core.BridgeError as exc:
            self.report.missing.append(str(exc))

    def create_collections(self) -> None:
        base = f"Mine-imator - {self.project.name}"
        name = core.unique_name(base, bpy.data.collections.keys())
        self.root_collection = bpy.data.collections.new(name)
        self.context.scene.collection.children.link(self.root_collection)
        self.root_collection["mi_bridge_version"] = core.ADDON_VERSION
        self.root_collection["mi_source_project"] = str(self.project.path)
        self.root_collection["mi_created_in"] = self.project.created_in
        self.report.collection_name = name
        for key, label in CATEGORY_NAMES.items():
            if key in self.options.categories:
                child = bpy.data.collections.new(label)
                self.root_collection.children.link(child)
                self.collections[key] = child

    def import_scene(self) -> ImportReport:
        self.prepare_assets()
        self.create_collections()
        for timeline in self.project.root_timelines():
            timeline_type = str(timeline.get("type", "")).lower()
            if timeline_type == "audio":
                self.report.skipped["audio"] += 1
                continue
            category = TYPE_CATEGORY.get(timeline_type)
            if not category:
                category = "helpers"
            if category not in self.options.categories:
                self.report.skipped[category] += 1
                continue
            self.report.imported[timeline_type] += 1
            try:
                obj = self._import_root(timeline, category)
                if obj:
                    self.timeline_objects[str(timeline.get("id"))] = obj
            except Exception as exc:
                label = timeline.get("name") or timeline_type or timeline.get("id")
                self.report.fallbacks.append(f"{label}: {type(exc).__name__}: {exc}")
                obj = _placeholder(str(label), str(exc), self.collections[category], self.project, timeline, self.report)
                self.timeline_objects[str(timeline.get("id"))] = obj
        self._apply_parenting()
        self._environment()
        self._mcprep_materials()
        self._ensure_static()
        write_report(self.report)
        return self.report

    def _import_root(self, timeline: dict[str, Any], category: str) -> bpy.types.Object | None:
        timeline_type = str(timeline.get("type", "")).lower()
        template = self.project.template_for_timeline(timeline)
        collection = self.collections[category]
        if timeline_type in {"char", "entity", "model", "spblock"}:
            return self._import_model(timeline, template, collection)
        if timeline_type == "item":
            return self._import_item(timeline, template, collection)
        if timeline_type == "block":
            return self._import_block(timeline, template, collection)
        if timeline_type in {"pointlight", "spotlight", "spot_light"}:
            return self._import_light(timeline, collection)
        if timeline_type == "camera":
            return self._import_camera(timeline, collection)
        if timeline_type == "scenery":
            return self._import_scenery(timeline, template, collection)
        if timeline_type == "text":
            return self._import_text(timeline, template, collection)
        if timeline_type in {"cube", "cone", "cylinder", "sphere", "surface"}:
            return self._import_primitive(timeline, timeline_type, collection)
        if timeline_type in {"particle", "particle_spawner"}:
            return _placeholder(timeline.get("name") or "Particle Spawner", "particle simulation is outside the static bridge scope", collection, self.project, timeline, self.report)
        if timeline_type == "path":
            return self._import_path(timeline, collection)
        obj = _new_empty(timeline.get("name") or timeline_type.title() or "Helper", collection)
        _apply_transform(obj, core.frame0_state(timeline))
        _metadata(obj, self.project, timeline, template)
        self.report.created["helper"] += 1
        return obj

    def _import_model(self, timeline: dict[str, Any], template: dict[str, Any] | None, collection: bpy.types.Collection) -> bpy.types.Object:
        label = timeline.get("name") or (template or {}).get("name") or str(timeline.get("type", "Model")).title()
        if not template:
            return _placeholder(label, "timeline template is missing", collection, self.project, timeline, self.report)
        model, model_path, texture, error = _model_definition(template, self.project, self.assets)
        if not model:
            self.report.missing.append(f"{label}: {error}")
            return _placeholder(label, error or "model not found", collection, self.project, timeline, self.report)
        state = core.frame0_state(timeline)
        texture_resource = self.project.resource(state.get("TEXTURE_OBJ"))
        if texture_resource:
            instance_texture = core.project_resource_path(self.project, texture_resource)
            if instance_texture and instance_texture.is_file():
                texture = instance_texture
            else:
                self.report.missing.append(f"{label}: frame-0 texture resource is missing")
        root = _new_empty(label, collection)
        _apply_transform(root, state)
        _metadata(root, self.project, timeline, template, model_path)
        part_map = _build_mimodel(model, model_path, root, collection, self.project, self.assets, texture, self.report)
        for bodypart in self._descendants(str(timeline.get("id"))):
            if str(bodypart.get("type", "")).lower() != "bodypart":
                continue
            part_name = str(bodypart.get("model_part_name", ""))
            candidates = part_map.get(part_name, [])
            if not candidates:
                self.report.fallbacks.append(f"{label}: body part {part_name} was not present in model")
                continue
            part_obj = candidates[0]
            state = core.frame0_state(bodypart)
            default_matrix = part_obj.matrix_basis.copy()
            state_matrix = _transform_matrix(
                core.mi_position(state),
                _engine_rotation_matrix(state),
                core.mi_scale(state),
            )
            # Mine-imator multiplies each timeline transform after the model
            # part's built-in transform. Matrix composition is essential here;
            # adding Euler components gives visibly wrong arm/head poses.
            part_obj.matrix_basis = default_matrix @ state_matrix
            _apply_model_bend(part_obj, state)
            part_obj["mi_timeline_id"] = str(bodypart.get("id", ""))
            part_obj["mi_bend_frame0"] = json.dumps([state.get("BEND_ANGLE_X", 0), state.get("BEND_ANGLE_Y", 0), state.get("BEND_ANGLE_Z", 0)])
            self.timeline_objects[str(bodypart.get("id"))] = part_obj
        self.report.created["model_root"] += 1
        return root

    def _descendants(self, timeline_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        pending = list(self.project.children(timeline_id))
        while pending:
            item = pending.pop(0)
            result.append(item)
            pending.extend(self.project.children(str(item.get("id"))))
        return result

    def _import_item(self, timeline: dict[str, Any], template: dict[str, Any] | None, collection: bpy.types.Collection) -> bpy.types.Object:
        state = core.frame0_state(timeline)
        item = dict((template or {}).get("item", {}))
        if self.options.honor_item_keyframe_changes:
            changed = state.get("ITEM") or state.get("ITEM_NAME")
            if isinstance(changed, str):
                item["name"] = changed
        name = str(item.get("name") or timeline.get("name") or "Item")
        texture = _resolve_texture(item.get("tex"), None, self.project, self.assets)
        if not texture and self.assets:
            texture = self.assets.materialize(f"textures/{name}.png")
        if not texture:
            texture = _mcprep_texture(name)
        if not texture:
            self.report.missing.append(f"{name}: item texture not found")
            return _placeholder(name, "item texture not found", collection, self.project, timeline, self.report)
        obj = _pixel_item(
            timeline.get("name") or Path(name).name,
            texture,
            collection,
            self.report,
            timeline.get("rot_point", (8.0, 0.0, 0.5)),
        )
        _apply_transform(obj, state)
        _metadata(obj, self.project, timeline, template, texture)
        return obj

    def _import_block(self, timeline: dict[str, Any], template: dict[str, Any] | None, collection: bpy.types.Collection) -> bpy.types.Object:
        name = str(((template or {}).get("block") or {}).get("name") or timeline.get("name") or "Block")
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        obj = self.context.active_object
        obj.name = name
        _link_object(obj, collection)
        clean_name = name.replace("minecraft:", "").removeprefix("block/")
        texture = self.assets.materialize(f"textures/block/{clean_name}.png") if self.assets else None
        if not texture:
            texture = _mcprep_texture(f"block/{clean_name}")
        obj.data.materials.append(_new_material(f"MI Block {name}", texture))
        _apply_transform(obj, core.frame0_state(timeline))
        _metadata(obj, self.project, timeline, template, texture)
        self.report.created["block"] += 1
        return obj

    def _import_light(self, timeline: dict[str, Any], collection: bpy.types.Collection) -> bpy.types.Object:
        state = core.frame0_state(timeline)
        is_spot = str(timeline.get("type", "")).lower() in {"spotlight", "spot_light"}
        data = bpy.data.lights.new(timeline.get("name") or ("Spot Light" if is_spot else "Point Light"), "SPOT" if is_spot else "POINT")
        color = _hex_color(state.get("LIGHT_COLOR"))
        data.color = color[:3]
        strength = max(0.0, float(state.get("LIGHT_STRENGTH", 1.0)))
        light_range = max(0.01, float(state.get("LIGHT_RANGE", 16.0)) / core.MI_UNITS_PER_BLOCK)
        data.energy = strength * max(25.0, light_range * light_range * 10.0)
        data.cutoff_distance = light_range
        data.use_shadow = bool(timeline.get("shadows", True))
        data.specular_factor = float(state.get("LIGHT_SPECULAR_STRENGTH", 1.0))
        data.shadow_soft_size = max(0.0, float(state.get("LIGHT_FADE_SIZE", 0.0)) / core.MI_UNITS_PER_BLOCK)
        if is_spot:
            data.spot_size = math.radians(float(state.get("SPOT_RADIUS", state.get("LIGHT_SPOT_RADIUS", 45.0))) * 2.0)
            data.spot_blend = min(1.0, max(0.0, float(state.get("SPOT_FADE", 0.15))))
        obj = bpy.data.objects.new(data.name, data)
        collection.objects.link(obj)
        _apply_transform(obj, state)
        _metadata(obj, self.project, timeline)
        self.report.created["light"] += 1
        return obj

    def _import_camera(self, timeline: dict[str, Any], collection: bpy.types.Collection) -> bpy.types.Object:
        state = core.frame0_state(timeline)
        data = bpy.data.cameras.new(timeline.get("name") or "Mine-imator Camera")
        fov = math.radians(float(state.get("CAMERA_FOV", state.get("FOV", 70.0))))
        # Mine-imator stores a vertical FOV; Blender's generic `angle` setter
        # is horizontal under the default sensor fit.
        data.sensor_fit = "VERTICAL"
        data.lens = data.sensor_height / (2.0 * math.tan(max(0.001, fov) / 2.0))
        data.dof.use_dof = bool(state.get("DOF", False))
        obj = bpy.data.objects.new(data.name, data)
        collection.objects.link(obj)
        _apply_transform(obj, state)
        yaw = math.radians(float(state.get("ROT_Z", 0.0)))
        pitch = math.radians(float(state.get("ROT_X", 0.0)))
        direction = Vector((
            math.cos(pitch) * math.sin(yaw),
            -math.cos(pitch) * math.cos(yaw),
            -math.sin(pitch),
        ))
        if direction.length_squared > 0:
            obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        _metadata(obj, self.project, timeline)
        self.context.scene.camera = obj
        self.report.created["camera"] += 1
        return obj

    def _import_scenery(self, timeline: dict[str, Any], template: dict[str, Any] | None, collection: bpy.types.Collection) -> bpy.types.Object:
        resource = self.project.resource((template or {}).get("scenery"))
        if not resource:
            # Mine-imator creates an empty scenery slot for a new project.  It
            # has no visible geometry, so a rendered missing-resource cube is
            # misleading (and showed up as the reported random white block).
            root = _new_empty(timeline.get("name") or "Empty World Scenery", collection, "PLAIN_AXES")
            _apply_transform(root, core.frame0_state(timeline))
            _metadata(root, self.project, timeline, template)
            root["mi_scenery_note"] = "No scenery resource is assigned; nothing visible to import"
            # Keep the empty slot for source accounting and metadata, but make
            # it completely unobtrusive. A CUBE display looked like imported
            # white geometry in solid/material-preview mode.
            root.empty_display_size = 0.05
            root.show_name = False
            root.hide_viewport = True
            root.hide_render = True
            self.report.notes.append(f"{root.name}: empty scenery slot contains no visible world geometry")
            self.report.created["empty_scenery"] += 1
            return root
        mineways = core.find_mineways(self.options.mineways_path)
        if not mineways:
            bounds = core.world_bounds({"start": resource.get("world_box_start"), "end": resource.get("world_box_end")})
            details = "Mineways is not configured"
            if bounds:
                details += f"; export X/Y/Z {bounds[0]} to {bounds[1]} from {resource.get('world_regions_dir', '')}"
            return _placeholder(timeline.get("name") or resource.get("filename") or "World Scenery", details, collection, self.project, timeline, self.report)
        raw_bounds = {"start": resource.get("world_box_start"), "end": resource.get("world_box_end")}
        bounds = core.world_bounds(raw_bounds)
        region_dir = Path(str(resource.get("world_regions_dir", "")))
        world_dir = region_dir.parent
        if not bounds or not region_dir.is_dir() or not world_dir.is_dir():
            return _placeholder(timeline.get("name") or "World Scenery", "saved world path or crop is invalid", collection, self.project, timeline, self.report)
        export_dir = Path(tempfile.gettempdir()) / "mineimator_mcprep_bridge" / "mineways"
        export_dir.mkdir(parents=True, exist_ok=True)
        stem = core.safe_filename(f"{self.project.path.stem}-{timeline.get('id', 'scenery')}")
        obj_path = export_dir / f"{stem}.obj"
        script_path = export_dir / f"{stem}.mwscript"
        log_path = export_dir / f"{stem}.log"
        start, end = bounds
        script = "\n".join((
            f"Save log file: {log_path}",
            "Show informational: script",
            "Show warning: script",
            "Show error: script",
            "Set render type: Wavefront OBJ absolute indices",
            f"Minecraft world: {world_dir}",
            f"Selection location min to max: {start[0]}, {start[1]}, {start[2]} to {end[0]}, {end[1]}, {end[2]}",
            "File type: Export all textures to three large images",
            "Center model: YES",
            "Make Z the up direction instead of Y: no",
            "Use biomes: yes",
            f"Export for Rendering: {obj_path}",
            "",
        ))
        script_path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [str(mineways), "-headless", "-s", str(world_dir.parent), str(script_path)],
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not obj_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            message = (log_text or completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip().replace("\n", " ")
            raise RuntimeError(f"Mineways export failed: {message[:400]}")
        before = set(bpy.data.objects)
        bpy.ops.wm.obj_import(filepath=str(obj_path), forward_axis="NEGATIVE_Z", up_axis="Y")
        imported = [obj for obj in bpy.data.objects if obj not in before]
        if not imported:
            raise RuntimeError("Mineways exported an OBJ but Blender imported no objects")
        root = _new_empty(timeline.get("name") or resource.get("filename") or "World Scenery", collection)
        _apply_transform(root, core.frame0_state(timeline))
        _metadata(root, self.project, timeline, template, obj_path)
        root["mi_world_region_dir"] = str(region_dir)
        root["mi_world_bounds_xyz"] = json.dumps(bounds)
        # Mine-imator's world builder maps Minecraft Z->engine X and
        # Minecraft X->engine Y. Blender's OBJ importer instead produces
        # (world X, -world Z, world Y), so transpose the horizontal axes into
        # Mine-imator's (world Z, -world X, world Y) local basis.
        world_basis = Matrix((
            (0.0, -1.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ))
        for obj in imported:
            _link_object(obj, collection)
            obj.parent = root
            obj.matrix_basis = world_basis @ obj.matrix_basis
            for material in getattr(obj.data, "materials", []):
                if material and material.use_nodes:
                    for node in material.node_tree.nodes:
                        if node.type == "TEX_IMAGE":
                            node.interpolation = "Closest"
            self.report.created["scenery_mesh"] += 1
        self.report.notes.append(f"Mineways export: {obj_path}")
        return root

    def _import_text(self, timeline: dict[str, Any], template: dict[str, Any] | None, collection: bpy.types.Collection) -> bpy.types.Object:
        state = core.frame0_state(timeline)
        curve = bpy.data.curves.new(timeline.get("name") or "Mine-imator Text", type="FONT")
        curve.body = str(state.get("TEXT", (template or {}).get("text", "Text")))
        curve.align_x = "CENTER"
        obj = bpy.data.objects.new(curve.name, curve)
        collection.objects.link(obj)
        _apply_transform(obj, state)
        _metadata(obj, self.project, timeline, template)
        self.report.created["text"] += 1
        return obj

    def _import_primitive(self, timeline: dict[str, Any], kind: str, collection: bpy.types.Collection) -> bpy.types.Object:
        if kind in {"cube", "surface"}:
            bpy.ops.mesh.primitive_cube_add(size=1.0)
        elif kind == "cone":
            bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=0.5, depth=1.0)
        elif kind == "cylinder":
            bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.5, depth=1.0)
        else:
            bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.5)
        obj = self.context.active_object
        obj.name = timeline.get("name") or kind.title()
        _link_object(obj, collection)
        state = core.frame0_state(timeline)
        obj.data.materials.append(_new_material(f"MI {kind.title()}", color=_hex_color(state.get("COLOR"), float(state.get("ALPHA", 1.0)))))
        _apply_transform(obj, state)
        _metadata(obj, self.project, timeline)
        self.report.created[kind] += 1
        return obj

    def _import_path(self, timeline: dict[str, Any], collection: bpy.types.Collection) -> bpy.types.Object:
        points = [item for item in self._descendants(str(timeline.get("id"))) if str(item.get("type", "")).lower() in {"path_point", "pathpoint"}]
        root = _new_empty(timeline.get("name") or "Mine-imator Path", collection)
        _apply_transform(root, core.frame0_state(timeline))
        _metadata(root, self.project, timeline)
        if len(points) < 2:
            root["mi_path_note"] = "Path has fewer than two visible points"
            self.report.created["path"] += 1
            return root
        curve = bpy.data.curves.new(f"{root.name} Geometry", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.025
        curve.bevel_resolution = 1
        spline = curve.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for index, point in enumerate(points):
            coordinate = core.mi_position(core.frame0_state(point))
            spline.points[index].co = (*coordinate, 1.0)
            handle = _new_empty(point.get("name") or f"Path Point {index + 1}", collection, "SPHERE")
            handle.location = coordinate
            handle.parent = root
            _metadata(handle, self.project, point)
            self.timeline_objects[str(point.get("id"))] = handle
        obj = bpy.data.objects.new(curve.name, curve)
        collection.objects.link(obj)
        obj.parent = root
        curve.materials.append(_new_material("MI Path", color=(0.2, 0.8, 1.0, 1.0)))
        self.report.created["path"] += 1
        return root

    def _apply_parenting(self) -> None:
        for timeline_id, obj in list(self.timeline_objects.items()):
            timeline = self.project.timeline(timeline_id)
            if not timeline or str(timeline.get("type", "")).lower() == "bodypart":
                continue
            parent_id = str(timeline.get("parent", "root"))
            parent = self.timeline_objects.get(parent_id)
            if parent and obj.parent is None:
                # Timeline transforms are stored relative to their Mine-imator
                # parent. Keep matrix_basis unchanged so held items and nested
                # entities follow the posed body part instead of retaining the
                # temporary world transform they had before hierarchy linking.
                obj.parent = parent

    def _environment(self) -> None:
        if "environment" not in self.options.categories:
            return
        background = self.project.data.get("background", {})
        if not isinstance(background, dict):
            return
        world = self.context.scene.world or bpy.data.worlds.new("Mine-imator World")
        self.context.scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        node = nodes.get("Background")
        color = background.get("color") or background.get("sky_color") or "#87A9FF"
        if node:
            node.inputs["Color"].default_value = _hex_color(color)
            node.inputs["Strength"].default_value = float(background.get("brightness", 0.8))
        if background.get("fog_show"):
            distance = max(1.0, float(background.get("fog_distance", 10000.0)) / core.MI_UNITS_PER_BLOCK)
            size = max(1.0, float(background.get("fog_size", 2000.0)) / core.MI_UNITS_PER_BLOCK)
            height = max(10.0, float(background.get("fog_height", 1250.0)) / core.MI_UNITS_PER_BLOCK)
            extent = distance + size
            bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, height / 2.0))
            fog = self.context.active_object
            fog.name = "Mine-imator Fog Volume"
            fog.dimensions = (extent * 2.0, extent * 2.0, height)
            _link_object(fog, self.collections["environment"])
            material = bpy.data.materials.new("Mine-imator Fog")
            material.use_nodes = True
            fog_nodes = material.node_tree.nodes
            fog_nodes.clear()
            output = fog_nodes.new("ShaderNodeOutputMaterial")
            volume = fog_nodes.new("ShaderNodeVolumePrincipled")
            volume.inputs["Color"].default_value = _hex_color(background.get("fog_color", color))
            volume.inputs["Density"].default_value = min(0.02, 0.12 / extent)
            material.node_tree.links.new(volume.outputs["Volume"], output.inputs["Volume"])
            fog.data.materials.append(material)
            fog.display_type = "WIRE"
            fog.hide_set(True)
            fog.hide_render = False
            fog["mi_bridge_version"] = core.ADDON_VERSION
            self.report.created["fog"] += 1
        if background.get("sunlight", background.get("sun", True)):
            data = bpy.data.lights.new("Mine-imator Sun", "SUN")
            data.energy = float(background.get("sunlight_strength", 1.0))
            sun = bpy.data.objects.new(data.name, data)
            self.collections["environment"].objects.link(sun)
            sun.rotation_euler = (math.radians(35), 0, math.radians(-35))
            sun["mi_bridge_version"] = core.ADDON_VERSION
            self.report.created["sun"] += 1
        if background.get("ground_show"):
            bpy.ops.mesh.primitive_plane_add(size=2000.0)
            ground = self.context.active_object
            ground.name = "Mine-imator Ground"
            _link_object(ground, self.collections["environment"])
            ground_name = str(background.get("ground_name", "block/grass_block_top"))
            texture = self.assets.materialize(f"textures/{ground_name}.png") if self.assets else None
            if not texture:
                texture = _mcprep_texture(ground_name)
            ground.data.materials.append(_new_material("Mine-imator Ground", texture, _hex_color(background.get("grass_color", "#91BD59"))))
            ground["mi_bridge_version"] = core.ADDON_VERSION
            self.report.created["ground"] += 1
        self.context.scene["mi_environment_source"] = json.dumps(background, separators=(",", ":"))[:60000]

    def _mcprep_materials(self) -> None:
        if not self.options.use_mcprep:
            return
        meshes = [obj for obj in self.root_collection.all_objects if obj.type == "MESH"] if self.root_collection else []
        if not meshes:
            return
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in meshes:
                obj.select_set(True)
            self.context.view_layer.objects.active = meshes[0]
            if hasattr(bpy.ops, "mcprep") and hasattr(bpy.ops.mcprep, "prep_materials"):
                # MCprep exposes this hidden flag for meta-operators; it also
                # avoids its optional telemetry wrapper affecting the return.
                result = bpy.ops.mcprep.prep_materials(skipUsage=True)
                self.report.notes.append(f"MCprep material preparation: {','.join(result)}")
            else:
                self.report.notes.append("MCprep material operator was not registered; bridge materials remain pixel-art ready")
        except Exception as exc:
            self.report.fallbacks.append(f"MCprep material preparation skipped: {exc}")

    def _ensure_static(self) -> None:
        if not self.root_collection:
            return
        for obj in self.root_collection.all_objects:
            if obj.animation_data:
                obj.animation_data_clear()
            if getattr(obj.data, "animation_data", None):
                obj.data.animation_data_clear()


def preflight(project_value: str, options: ImportOptions) -> ImportReport:
    project = core.load_project(project_value)
    report = ImportReport(project_path=str(project.path))
    report.notes.append(f"Project format {project.data.get('format')}, created in Mine-imator {project.created_in}")
    install = core.find_mineimator_install(project.path, options.mineimator_path)
    if install:
        try:
            store = core.AssetStore.open(install)
            report.notes.append(f"Mine-imator {store.asset_version} asset pack found: {store.zip_path}")
        except core.BridgeError as exc:
            report.missing.append(str(exc))
    else:
        report.missing.append("Mine-imator installation was not detected")
    mineways = core.find_mineways(options.mineways_path)
    if mineways:
        report.notes.append(f"Mineways found: {mineways}")
    else:
        report.missing.append("Mineways is not configured; scenery will become labeled placeholders")
    for timeline in project.root_timelines():
        timeline_type = str(timeline.get("type", "unknown")).lower()
        if timeline_type == "audio":
            report.skipped["audio"] += 1
        else:
            report.imported[timeline_type] += 1
    for resource in project.resources.values():
        if resource.get("type") in {"skin", "downloadskin", "model", "texture"} and not core.project_resource_path(project, resource):
            report.missing.append(f"Project resource missing: {resource.get('filename', resource.get('id'))}")
    return report


def write_report(report: ImportReport) -> bpy.types.Text:
    base = "Mine-imator Bridge Report"
    name = core.unique_name(base, bpy.data.texts.keys())
    text = bpy.data.texts.new(name)
    text.write(report.text())
    return text
