"""Editable Mine-imator-style static environment rig for Blender."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import math

import bpy
import bmesh
from mathutils import Vector

from . import core


ROLE_KEY = "mi_suite_role"
SUITE_KEY = "mi_suite_id"


def _rgba(value: Any, alpha: float = 1.0) -> tuple[float, float, float, float]:
    if isinstance(value, (list, tuple)):
        values = [float(item) for item in value]
        if values and max(values) > 1.0:
            values = [item / 255.0 for item in values]
        return tuple((values + [alpha] * 4)[:4])
    raw = str(value or "#FFFFFF").lstrip("#")
    if len(raw) == 8:
        alpha = int(raw[6:8], 16) / 255.0
        raw = raw[:6]
    try:
        return tuple(int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4)) + (alpha,)
    except (ValueError, IndexError):
        return (1.0, 1.0, 1.0, alpha)


def _mix(left: tuple[float, ...], right: tuple[float, ...], amount: float) -> tuple[float, float, float, float]:
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(float(left[i]) * (1.0 - amount) + float(right[i]) * amount for i in range(4))


def _tag(block: Any, suite_id: str, role: str, source_project: str) -> None:
    block["mi_bridge_version"] = core.ADDON_VERSION
    block[SUITE_KEY] = suite_id
    block[ROLE_KEY] = role
    block["mi_source_project"] = source_project


def _load_image(path: str | Path | None, suite_id: str, role: str, source_project: str) -> bpy.types.Image | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        image = bpy.data.images.load(str(candidate), check_existing=True)
        image.colorspace_settings.name = "sRGB"
        _tag(image, suite_id, role, source_project)
        return image
    except Exception:
        return None


def _resolve_texture(project: core.ProjectIndex, assets: core.AssetStore | None, value: Any, default_name: str) -> Path | None:
    if value not in core.NULL_IDS:
        resource = project.resource(value)
        path = core.project_resource_path(project, resource)
        if path:
            return path
        raw = Path(str(value))
        for candidate in (raw, project.project_dir / raw, project.project_dir / raw.name):
            if candidate.is_file():
                return candidate.resolve()
        if assets and resource:
            filename = resource.get("filename") or resource.get("path")
            if filename:
                materialized = assets.materialize(str(filename))
                if materialized:
                    return materialized
    return assets.materialize(default_name) if assets else None


def _quad_mesh(name: str, uv_bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (0.5, 0.5, 0.0), (-0.5, 0.5, 0.0)], [], [(0, 1, 2, 3)])
    layer = mesh.uv_layers.new(name="UVMap")
    u0, v0, u1, v1 = uv_bounds
    for loop, uv in zip(mesh.loops, ((u0, v0), (u1, v0), (u1, v1), (u0, v1))):
        layer.data[loop.index].uv = uv
    return mesh


def _box_mesh(name: str) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(name)
    vertices = [
        (-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
        (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5),
    ]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh.from_pydata(vertices, [], faces)
    layer = mesh.uv_layers.new(name="UVMap")
    face_uvs = ((0, 0), (1, 0), (1, 1), (0, 1))
    for polygon in mesh.polygons:
        for index, loop_index in enumerate(polygon.loop_indices):
            layer.data[loop_index].uv = face_uvs[index]
    return mesh


def _surface_material(name: str, image: bpy.types.Image | None, tint: tuple[float, float, float, float], *, emission: bool = False) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    shader = nodes.new("ShaderNodeEmission" if emission else "ShaderNodeBsdfPrincipled")
    shader.name = "MI Shader"
    tint_node = nodes.new("ShaderNodeRGB")
    tint_node.name = "MI Tint"
    tint_node.outputs[0].default_value = tint
    if image:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name = "MI Texture"
        texture.image = image
        texture.interpolation = "Closest"
        texture.extension = "REPEAT"
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.name = "MI Texture Tint"
        multiply.blend_type = "MULTIPLY"
        multiply.inputs[0].default_value = 1.0
        links.new(texture.outputs["Color"], multiply.inputs[1])
        links.new(tint_node.outputs[0], multiply.inputs[2])
        color_output = multiply.outputs[0]
        if not emission:
            alpha = nodes.new("ShaderNodeMath")
            alpha.name = "MI Alpha"
            alpha.operation = "MULTIPLY"
            alpha.inputs[1].default_value = tint[3]
            links.new(texture.outputs["Alpha"], alpha.inputs[0])
            links.new(alpha.outputs[0], shader.inputs["Alpha"])
    else:
        color_output = tint_node.outputs[0]
    links.new(color_output, shader.inputs["Color"] if emission else shader.inputs["Base Color"])
    if emission:
        shader.inputs["Strength"].default_value = 1.0
    else:
        shader.inputs["Roughness"].default_value = 0.9
    if emission and image:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        mix_shader = nodes.new("ShaderNodeMixShader")
        mix_shader.name = "MI Alpha Mix"
        links.new(transparent.outputs[0], mix_shader.inputs[1])
        links.new(shader.outputs[0], mix_shader.inputs[2])
        links.new(texture.outputs["Alpha"], mix_shader.inputs[0])
        links.new(mix_shader.outputs[0], output.inputs["Surface"])
    else:
        links.new(shader.outputs[0], output.inputs["Surface"])
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    material.diffuse_color = tint
    material["mi_pixel_art"] = True
    return material


def _volume_material(name: str, color: tuple[float, float, float, float]) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    volume = nodes.new("ShaderNodeVolumePrincipled")
    volume.name = "MI Fog Volume"
    volume.inputs["Color"].default_value = color
    volume.inputs["Density"].default_value = 0.0001
    links.new(volume.outputs["Volume"], output.inputs["Volume"])
    return material


def _star_material(name: str) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.name = "MI Stars Emission"
    emission.inputs["Color"].default_value = (1, 1, 1, 1)
    emission.inputs["Strength"].default_value = 0.0
    noise = nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "3D"
    noise.inputs["Scale"].default_value = 300.0
    noise.inputs["Detail"].default_value = 1.0
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.982
    ramp.color_ramp.elements[1].position = 0.995
    ramp.color_ramp.elements[0].color = (0, 0, 0, 1)
    ramp.color_ramp.elements[1].color = (1, 1, 1, 1)
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    mix_shader = nodes.new("ShaderNodeMixShader")
    mix_shader.name = "MI Stars Mix"
    links.new(ramp.outputs["Color"], emission.inputs["Color"])
    links.new(transparent.outputs[0], mix_shader.inputs[1])
    links.new(emission.outputs[0], mix_shader.inputs[2])
    links.new(ramp.outputs["Color"], mix_shader.inputs[0])
    links.new(mix_shader.outputs[0], output.inputs["Surface"])
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    return material


def _set_material_image(material: bpy.types.Material | None, path: str, suite_id: str, role: str) -> None:
    if not material or not material.use_nodes:
        return
    texture = material.node_tree.nodes.get("MI Texture")
    if texture:
        texture.image = _load_image(path, suite_id, role, "")


def _new_object(name: str, data: Any, collection: bpy.types.Collection, suite_id: str, role: str, source_project: str) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    _tag(obj, suite_id, role, source_project)
    if data is not None:
        _tag(data, suite_id, role, source_project)
    return obj


def _role_objects(suite_id: str) -> dict[str, bpy.types.Object]:
    return {
        str(obj.get(ROLE_KEY)): obj
        for obj in bpy.data.objects
        if obj.get(SUITE_KEY) == suite_id and obj.get(ROLE_KEY)
    }


def _set_moon_uv(obj: bpy.types.Object, phase: int) -> None:
    if not obj or obj.type != "MESH" or not obj.data.uv_layers:
        return
    u0, v0, u1, v1 = core.moon_phase_uv(phase)
    for loop, uv in zip(obj.data.loops, ((u0, v0), (u1, v0), (u1, v1), (u0, v1))):
        obj.data.uv_layers[0].data[loop.index].uv = uv


def _image_sample(path: Path | None, coordinate: Any) -> tuple[float, float, float, float] | None:
    if not path or not isinstance(coordinate, list) or len(coordinate) < 2:
        return None
    image = _load_image(path, "biome-cache", "biome-colormap", "")
    if not image or image.size[0] < 1 or image.size[1] < 1:
        return None
    x = max(0, min(image.size[0] - 1, int(coordinate[0])))
    y = max(0, min(image.size[1] - 1, int(coordinate[1])))
    index = ((image.size[1] - 1 - y) * image.size[0] + x) * 4
    pixels = image.pixels[index:index + 4]
    return tuple(float(value) for value in pixels)


def _biome_presets(assets: core.AssetStore | None, state: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, list[float]]]]:
    current = str(state.get("biome", "plains"))
    names = [current, "custom"]
    presets: dict[str, dict[str, list[float]]] = {}
    if assets:
        grass_map = assets.materialize("textures/colormap/grass.png")
        foliage_map = assets.materialize("textures/colormap/foliage.png")
        for base in assets.records("biomes"):
            for record in [base, *base.get("variant", [])]:
                name = str(record.get("name", ""))
                if not name:
                    continue
                names.append(name)
                foliage_value = record.get("foliage", base.get("foliage"))
                grass = _image_sample(grass_map, foliage_value) or _rgba(state.get("grass_color"))
                foliage = _image_sample(foliage_map, foliage_value) or _rgba(foliage_value or state.get("foliage_color"))
                water = _rgba(record.get("water", base.get("water", state.get("water_color"))))
                presets[name] = {"grass": list(grass), "foliage": list(foliage), "water": list(water)}
    presets[current] = {
        "grass": list(_rgba(state.get("grass_color"))),
        "foliage": list(_rgba(state.get("foliage_color"))),
        "water": list(_rgba(state.get("water_color"))),
    }
    return sorted(set(names)), presets


def biome_items(_self: Any, context: bpy.types.Context) -> list[tuple[str, str, str]]:
    scene = context.scene if context else None
    try:
        names = json.loads(scene.get("mi_suite_biomes", "[]")) if scene else []
    except (TypeError, json.JSONDecodeError):
        names = []
    names = names or ["plains", "custom"]
    return [(name, name.replace("_", " ").title(), "") for name in names]


def apply_biome_preset(scene: bpy.types.Scene) -> None:
    if scene.get("mi_suite_loading") or not hasattr(scene, "mi_environment"):
        return
    env = scene.mi_environment
    if env.biome == "custom":
        return
    try:
        preset = json.loads(scene.get("mi_suite_biome_presets", "{}"))[env.biome]
    except (KeyError, TypeError, json.JSONDecodeError):
        return
    scene["mi_suite_loading"] = True
    try:
        env.grass_color = preset["grass"]
        env.foliage_color = preset["foliage"]
        env.water_color = preset["water"]
    finally:
        scene["mi_suite_loading"] = False


def _populate_properties(scene: bpy.types.Scene, state: dict[str, Any], *, suite_id: str, collection_name: str, world_name: str, paths: dict[str, Path | None], source_json: str) -> None:
    env = scene.mi_environment
    scene["mi_suite_loading"] = True
    try:
        env.active = True
        env.suite_id = suite_id
        env.collection_name = collection_name
        env.world_name = world_name
        env.source_json = source_json
        env.time_hours = core.sky_time_to_hours(float(state["sky_time"]))
        env.source_sky_time = float(state["sky_time"])
        env.sky_rotation = float(state["sky_rotation"])
        env.sunlight_angle = float(state["sunlight_angle"])
        env.sunlight_strength = float(state["sunlight_strength"]) * 100.0
        env.sky_mode = "CUSTOM" if state.get("image_show") else "MINECRAFT"
        env.background_image = str(paths.get("background") or "")
        env.background_type = str(state.get("image_type", "image")).upper()
        env.background_stretch = bool(state.get("image_stretch", True))
        env.background_box_mapped = bool(state.get("image_box_mapped", False))
        env.background_rotation = float(state.get("image_rotation", 0.0))
        env.sun_texture = str(paths.get("sun") or "")
        env.sun_angle = float(state["sky_sun_angle"])
        env.sun_size = float(state["sky_sun_scale"]) * 100.0
        env.moon_texture = str(paths.get("moon") or "")
        env.moon_phase = str(int(state["sky_moon_phase"]) % 8)
        env.moon_angle = float(state["sky_moon_angle"])
        env.moon_size = float(state["sky_moon_scale"]) * 100.0
        env.clouds_show = bool(state["sky_clouds_show"])
        env.clouds_mode = str(state["sky_clouds_mode"]).upper()
        env.clouds_texture = str(paths.get("clouds") or "")
        env.clouds_speed = float(state["sky_clouds_speed"]) * 100.0
        env.clouds_offset = float(state["sky_clouds_offset"])
        env.clouds_height = float(state["sky_clouds_height"])
        env.clouds_size = float(state["sky_clouds_size"])
        env.clouds_thickness = float(state["sky_clouds_thickness"])
        env.ground_show = True
        env.ground_texture = str(paths.get("ground") or "")
        env.biome = str(state.get("biome", "plains"))
        for key in (
            "sky_color", "sky_clouds_color", "sunlight_color", "ambient_color", "night_color",
            "grass_color", "foliage_color", "water_color", "leaves_oak_color", "leaves_spruce_color",
            "leaves_birch_color", "leaves_jungle_color", "leaves_acacia_color", "leaves_dark_oak_color",
            "leaves_mangrove_color", "fog_color", "fog_object_color",
        ):
            setattr(env, key, _rgba(state.get(key)))
        env.twilight = bool(state["twilight"])
        env.fog_show = bool(state["fog_show"])
        env.fog_sky = bool(state["fog_sky"])
        env.fog_color_custom = bool(state["fog_color_custom"])
        env.fog_object_color_custom = bool(state["fog_object_color_custom"])
        env.fog_distance = float(state["fog_distance"])
        env.fog_size = float(state["fog_size"])
        env.fog_height = float(state["fog_height"])
        env.wind = bool(state["wind"])
        env.wind_speed = float(state["wind_speed"]) * 100.0
        env.wind_strength = float(state["wind_strength"])
        env.wind_direction = float(state["wind_direction"])
        env.wind_directional_speed = float(state["wind_directional_speed"]) * 100.0
        env.wind_directional_strength = float(state["wind_directional_strength"])
        env.texture_animation_speed = float(state["texture_animation_speed"])
    finally:
        scene["mi_suite_loading"] = False


def build_suite(context: bpy.types.Context, parent: bpy.types.Collection, project: core.ProjectIndex, assets: core.AssetStore | None, report: Any) -> bpy.types.Collection:
    source = core.environment_state(project.data)
    state = core.environment_state(project.data, force_ground=True)
    suite_id = report.collection_name
    for collection in bpy.data.collections:
        if collection.get("mi_suite_active"):
            collection["mi_suite_active"] = False
            collection.hide_render = True
            collection.hide_viewport = True
    suite = bpy.data.collections.new("Mine-imator Suite")
    parent.children.link(suite)
    suite["mi_suite_active"] = True
    suite["mi_suite_id"] = suite_id
    suite["mi_source_project"] = str(project.path)
    suite["mi_environment_source"] = json.dumps(source, separators=(",", ":"))
    suite["mi_bridge_version"] = core.ADDON_VERSION

    paths = {
        "background": _resolve_texture(project, assets, state.get("image"), "textures/environment/end_sky.png") if state.get("image_show") else None,
        "sun": _resolve_texture(project, assets, state.get("sky_sun_tex"), "textures/environment/sun.png"),
        "moon": _resolve_texture(project, assets, state.get("sky_moon_tex"), "textures/environment/moon_phases.png"),
        "clouds": _resolve_texture(project, assets, state.get("sky_clouds_tex"), "textures/environment/clouds.png"),
        "ground": _resolve_texture(project, assets, state.get("ground_tex"), f"textures/{state.get('ground_name', 'block/grass_block_top')}.png"),
    }
    images = {key: _load_image(path, suite_id, key, str(project.path)) for key, path in paths.items()}

    world = bpy.data.worlds.new(f"Mine-imator World - {project.name}")
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputWorld")
    background = nodes.new("ShaderNodeBackground")
    background.name = "MI Background"
    links.new(background.outputs[0], output.inputs["Surface"])
    _tag(world, suite_id, "world", str(project.path))
    context.scene.world = world

    sky_root = _new_object("Mine-imator Sky Rig", None, suite, suite_id, "sky_root", str(project.path))
    if context.scene.camera:
        constraint = sky_root.constraints.new("COPY_LOCATION")
        constraint.name = "Follow Active Mine-imator Camera"
        constraint.target = context.scene.camera

    sun_data = bpy.data.lights.new("Mine-imator Sunlight", "SUN")
    sun_light = _new_object("Mine-imator Sunlight", sun_data, suite, suite_id, "sun_light", str(project.path))

    sun_material = _surface_material("Mine-imator Sun Disc", images["sun"], (1, 1, 1, 1), emission=True)
    _tag(sun_material, suite_id, "sun_material", str(project.path))
    sun_disc = _new_object("Mine-imator Sun", _quad_mesh("Mine-imator Sun Mesh"), suite, suite_id, "sun_disc", str(project.path))
    sun_disc.data.materials.append(sun_material)
    sun_disc.parent = sky_root

    moon_material = _surface_material("Mine-imator Moon Disc", images["moon"], (1, 1, 1, 1), emission=True)
    _tag(moon_material, suite_id, "moon_material", str(project.path))
    moon_disc = _new_object("Mine-imator Moon", _quad_mesh("Mine-imator Moon Mesh", core.moon_phase_uv(int(state["sky_moon_phase"]))), suite, suite_id, "moon_disc", str(project.path))
    moon_disc.data.materials.append(moon_material)
    moon_disc.parent = sky_root

    star_mesh = bpy.data.meshes.new("Mine-imator Stars Mesh")
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=3, radius=190.0)
    bm.to_mesh(star_mesh)
    bm.free()
    stars = _new_object("Mine-imator Stars", star_mesh, suite, suite_id, "stars", str(project.path))
    star_material = _star_material("Mine-imator Pixel Stars")
    _tag(star_material, suite_id, "stars_material", str(project.path))
    stars.data.materials.append(star_material)
    stars.parent = sky_root

    cloud_material = _surface_material("Mine-imator Clouds", images["clouds"], _rgba(state["sky_clouds_color"]))
    _tag(cloud_material, suite_id, "clouds_material", str(project.path))
    clouds = _new_object("Mine-imator Clouds", _box_mesh("Mine-imator Clouds Mesh"), suite, suite_id, "clouds", str(project.path))
    clouds.data.materials.append(cloud_material)

    ground_size = max(2048.0, 2.0 * (float(state["fog_distance"]) + float(state["fog_size"])) / core.MI_UNITS_PER_BLOCK)
    ground_mesh = _quad_mesh("Mine-imator Ground Mesh", (0.0, 0.0, ground_size, ground_size))
    ground_material = _surface_material("Mine-imator Grass Ground", images["ground"], _rgba(state["grass_color"]))
    _tag(ground_material, suite_id, "ground_material", str(project.path))
    ground = _new_object("Mine-imator Grass Ground", ground_mesh, suite, suite_id, "ground", str(project.path))
    ground.scale = (ground_size, ground_size, 1.0)
    ground.data.materials.append(ground_material)

    fog_material = _volume_material("Mine-imator Fog", _rgba(state["fog_color"]))
    _tag(fog_material, suite_id, "fog_material", str(project.path))
    fog = _new_object("Mine-imator Fog Volume", _box_mesh("Mine-imator Fog Mesh"), suite, suite_id, "fog", str(project.path))
    fog.data.materials.append(fog_material)
    fog.display_type = "WIRE"
    fog.hide_set(True)

    names, presets = _biome_presets(assets, state)
    context.scene["mi_suite_biomes"] = json.dumps(names)
    context.scene["mi_suite_biome_presets"] = json.dumps(presets)
    context.scene["mi_suite_active_id"] = suite_id
    _populate_properties(
        context.scene,
        state,
        suite_id=suite_id,
        collection_name=suite.name,
        world_name=world.name,
        paths=paths,
        source_json=json.dumps(source, separators=(",", ":")),
    )
    apply_environment(context.scene)
    report.created.update({"suite": 1, "ground": 1, "sun": 1, "sun_disc": 1, "moon": 1, "clouds": 1, "stars": 1, "fog": 1})
    report.notes.append("Mine-imator Suite enabled; grass ground forced visible and remains editable")
    report.notes.append("Wind, cloud speed, and texture-animation speed are preserved as static values; no animation was created")
    report.notes.append("Mineways scenery atlases retain their exported biome colors after sidebar biome changes")
    return suite


def apply_environment(scene: bpy.types.Scene) -> None:
    if scene.get("mi_suite_loading") or not hasattr(scene, "mi_environment"):
        return
    env = scene.mi_environment
    if not env.active or not env.suite_id:
        return
    objects = _role_objects(env.suite_id)
    factors = core.environment_light_factors(core.hours_to_sky_time(env.time_hours), env.sky_rotation)
    sky_color = tuple(env.sky_color)
    night_color = tuple(env.night_color)
    final_sky = _mix(sky_color, night_color, factors["night"])
    if env.twilight and factors["sunrise"] + factors["sunset"] > 0.0:
        final_sky = _mix(final_sky, (1.0, 0.35, 0.08, 1.0), min(0.35, (factors["sunrise"] + factors["sunset"]) * 0.35))

    world = bpy.data.worlds.get(env.world_name)
    if world and world.use_nodes:
        background = world.node_tree.nodes.get("MI Background")
        if background:
            background.inputs["Color"].default_value = final_sky
            ambient = sum(tuple(env.ambient_color)[:3]) / 3.0
            background.inputs["Strength"].default_value = max(0.03, ambient * (0.3 + 0.7 * factors["day"]))
        texture = world.node_tree.nodes.get("MI Environment Texture")
        if env.sky_mode == "CUSTOM" and env.background_image and Path(env.background_image).is_file():
            if not texture:
                texture = world.node_tree.nodes.new("ShaderNodeTexEnvironment")
                texture.name = "MI Environment Texture"
            image = _load_image(env.background_image, env.suite_id, "background", "")
            texture.image = image
            texture.projection = "EQUIRECTANGULAR"
            coordinates = world.node_tree.nodes.get("MI Environment Coordinates")
            mapping = world.node_tree.nodes.get("MI Environment Mapping")
            if not coordinates:
                coordinates = world.node_tree.nodes.new("ShaderNodeTexCoord")
                coordinates.name = "MI Environment Coordinates"
            if not mapping:
                mapping = world.node_tree.nodes.new("ShaderNodeMapping")
                mapping.name = "MI Environment Mapping"
                world.node_tree.links.new(coordinates.outputs["Generated"], mapping.inputs["Vector"])
                world.node_tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
            mapping.inputs["Rotation"].default_value[2] = math.radians(env.background_rotation)
            if background and not any(link.from_node == texture and link.to_node == background for link in world.node_tree.links):
                world.node_tree.links.new(texture.outputs["Color"], background.inputs["Color"])
        elif texture:
            for link in list(world.node_tree.links):
                if link.from_node == texture:
                    world.node_tree.links.remove(link)

    sky_time = core.hours_to_sky_time(env.time_hours)
    direction = Vector(core.sky_sun_direction(sky_time, env.sky_rotation))
    sun_light = objects.get("sun_light")
    if sun_light and sun_light.type == "LIGHT":
        sun_light.data.energy = max(0.0, env.sunlight_strength / 100.0) * factors["day"]
        sun_light.data.color = tuple(env.sunlight_color)[:3]
        sun_light.data.angle = math.radians(max(0.0, env.sunlight_angle))
        sun_light.rotation_mode = "QUATERNION"
        sun_light.rotation_quaternion = (-direction).to_track_quat("-Z", "Y")
        sun_light["mi_sun_direction"] = list(direction)
    for role, radial, size, angle in (
        ("sun_disc", direction, env.sun_size, env.sun_angle),
        ("moon_disc", -direction, env.moon_size, env.moon_angle),
    ):
        obj = objects.get(role)
        if not obj:
            continue
        obj.location = radial * 180.0
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = (-radial).to_track_quat("Z", "Y")
        obj.rotation_quaternion @= Vector((0.0, 0.0, 1.0)).rotation_difference(Vector((math.sin(math.radians(angle)), 0.0, math.cos(math.radians(angle)))))
        scale = 12.0 * max(0.0, size / 100.0)
        obj.scale = (scale, scale, scale)
        obj.hide_render = radial.z < -0.15
        obj.hide_viewport = obj.hide_render
    if objects.get("sun_disc") and objects["sun_disc"].data.materials:
        _set_material_image(objects["sun_disc"].data.materials[0], env.sun_texture, env.suite_id, "sun")
    if objects.get("moon_disc") and objects["moon_disc"].data.materials:
        _set_material_image(objects["moon_disc"].data.materials[0], env.moon_texture, env.suite_id, "moon")
    _set_moon_uv(objects.get("moon_disc"), int(env.moon_phase))

    stars = objects.get("stars")
    if stars:
        stars.hide_render = factors["night"] <= 0.001
        stars.hide_viewport = stars.hide_render
        if stars.data.materials:
            emission = stars.data.materials[0].node_tree.nodes.get("MI Stars Emission")
            if emission:
                emission.inputs["Strength"].default_value = factors["night"] * 0.8

    ground = objects.get("ground")
    if ground:
        ground.hide_render = not env.ground_show
        ground.hide_viewport = not env.ground_show
        if ground.data.materials:
            material = ground.data.materials[0]
            _set_material_image(material, env.ground_texture, env.suite_id, "ground")
            tint = material.node_tree.nodes.get("MI Tint")
            if tint:
                tint.outputs[0].default_value = env.grass_color

    clouds = objects.get("clouds")
    if clouds:
        thickness = 0.01 if env.clouds_mode == "FLAT" else max(0.01, env.clouds_thickness / core.MI_UNITS_PER_BLOCK)
        span = max(1.0, env.clouds_size * 2.0)
        clouds.dimensions = (span, span, thickness)
        clouds.location = (0.0, -env.clouds_offset / core.MI_UNITS_PER_BLOCK, env.clouds_height / core.MI_UNITS_PER_BLOCK + thickness / 2.0)
        clouds.hide_render = not env.clouds_show
        clouds.hide_viewport = not env.clouds_show
        if clouds.data.materials:
            material = clouds.data.materials[0]
            _set_material_image(material, env.clouds_texture, env.suite_id, "clouds")
            tint = material.node_tree.nodes.get("MI Tint")
            if tint:
                color = tuple(env.sky_clouds_color)
                tint.outputs[0].default_value = (*color[:3], 0.35 if env.clouds_mode == "FADED" else 0.8)
            alpha = material.node_tree.nodes.get("MI Alpha")
            if alpha:
                alpha.inputs[1].default_value = 0.35 if env.clouds_mode == "FADED" else 0.8

    fog = objects.get("fog")
    if fog:
        distance = max(1.0, env.fog_distance / core.MI_UNITS_PER_BLOCK)
        fade = max(1.0, env.fog_size / core.MI_UNITS_PER_BLOCK)
        height = max(1.0, env.fog_height / core.MI_UNITS_PER_BLOCK)
        extent = distance + fade
        fog.dimensions = (extent * 2.0, extent * 2.0, height)
        fog.location = (0.0, 0.0, height / 2.0)
        fog.hide_render = not env.fog_show
        if fog.data.materials:
            volume = fog.data.materials[0].node_tree.nodes.get("MI Fog Volume")
            if volume:
                fog_color = tuple(env.fog_object_color if env.fog_object_color_custom else (env.fog_color if env.fog_color_custom else final_sky))
                volume.inputs["Color"].default_value = fog_color
                volume.inputs["Density"].default_value = min(0.002, 0.02 / fade)

    scene["mi_environment_source"] = env.source_json


def reload_environment(scene: bpy.types.Scene) -> bool:
    if not hasattr(scene, "mi_environment") or not scene.mi_environment.active:
        return False
    env = scene.mi_environment
    try:
        source = json.loads(env.source_json)
    except (TypeError, json.JSONDecodeError):
        return False
    paths = {
        "background": Path(env.background_image) if env.background_image else None,
        "sun": Path(env.sun_texture) if env.sun_texture else None,
        "moon": Path(env.moon_texture) if env.moon_texture else None,
        "clouds": Path(env.clouds_texture) if env.clouds_texture else None,
        "ground": Path(env.ground_texture) if env.ground_texture else None,
    }
    _populate_properties(
        scene,
        core.environment_state(source, force_ground=True),
        suite_id=env.suite_id,
        collection_name=env.collection_name,
        world_name=env.world_name,
        paths=paths,
        source_json=env.source_json,
    )
    apply_environment(scene)
    return True
