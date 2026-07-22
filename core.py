"""Pure-Python Mine-imator project parsing and asset resolution.

This module deliberately has no bpy dependency so it can be tested outside
Blender.  Mine-imator's format 34 is JSON even though several inputs use
custom extensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
import hashlib
import json
import math
import os
import re
import tempfile
import zipfile


ADDON_VERSION = "0.1.1"
SUPPORTED_FORMAT = 34
MI_UNITS_PER_BLOCK = 16.0
NULL_IDS = {None, "", "null", "default", -1}


class BridgeError(RuntimeError):
    """A user-facing import error."""


def _is_null(value: Any) -> bool:
    try:
        return value in NULL_IDS
    except TypeError:
        return False


def resolve_project_path(value: str | os.PathLike[str]) -> Path:
    """Resolve either a .miproject path or a directory containing exactly one."""
    path = Path(value).expanduser()
    if path.is_file():
        if path.suffix.lower() != ".miproject":
            raise BridgeError(f"Not a .miproject file: {path}")
        return path.resolve()
    if not path.is_dir():
        raise BridgeError(f"Project path does not exist: {path}")
    candidates = sorted(path.glob("*.miproject"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise BridgeError(f"No .miproject file found in {path}")
    names = ", ".join(candidate.name for candidate in candidates)
    raise BridgeError(f"Multiple .miproject files found; select one: {names}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeError(f"Expected a JSON object in {path}")
    return data


def load_project(value: str | os.PathLike[str]) -> "ProjectIndex":
    path = resolve_project_path(value)
    data = load_json(path)
    project_format = int(data.get("format", -1))
    if project_format != SUPPORTED_FORMAT:
        raise BridgeError(
            f"Unsupported Mine-imator project format {project_format}; "
            f"this version supports format {SUPPORTED_FORMAT}"
        )
    return ProjectIndex(path, data)


def _index_by_id(values: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(value["id"]): value
        for value in values
        if isinstance(value, dict) and not _is_null(value.get("id"))
    }


@dataclass(slots=True)
class ProjectIndex:
    path: Path
    data: dict[str, Any]
    resources: dict[str, dict[str, Any]] = field(init=False)
    templates: dict[str, dict[str, Any]] = field(init=False)
    timelines: dict[str, dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.resources = _index_by_id(self.data.get("resources", []))
        self.templates = _index_by_id(self.data.get("templates", []))
        self.timelines = _index_by_id(self.data.get("timelines", []))

    @property
    def project_dir(self) -> Path:
        return self.path.parent

    @property
    def name(self) -> str:
        project = self.data.get("project", {})
        return str(project.get("name") or self.path.stem)

    @property
    def created_in(self) -> str:
        return str(self.data.get("created_in", "unknown"))

    def resource(self, resource_id: Any) -> dict[str, Any] | None:
        return None if _is_null(resource_id) else self.resources.get(str(resource_id))

    def template(self, template_id: Any) -> dict[str, Any] | None:
        return None if _is_null(template_id) else self.templates.get(str(template_id))

    def timeline(self, timeline_id: Any) -> dict[str, Any] | None:
        return None if _is_null(timeline_id) else self.timelines.get(str(timeline_id))

    def template_for_timeline(self, timeline: dict[str, Any]) -> dict[str, Any] | None:
        return self.template(timeline.get("temp"))

    def root_timelines(self) -> list[dict[str, Any]]:
        return [
            timeline
            for timeline in self.data.get("timelines", [])
            if str(timeline.get("parent", "root")) == "root"
        ]

    def children(self, timeline_id: str) -> list[dict[str, Any]]:
        return [
            timeline
            for timeline in self.data.get("timelines", [])
            if str(timeline.get("parent")) == str(timeline_id)
        ]


ENGINE_DEFAULTS: dict[str, Any] = {
    "POS_X": 0.0,
    "POS_Y": 0.0,
    "POS_Z": 0.0,
    "ROT_X": 0.0,
    "ROT_Y": 0.0,
    "ROT_Z": 0.0,
    "SCALE_X": 1.0,
    "SCALE_Y": 1.0,
    "SCALE_Z": 1.0,
    "ALPHA": 1.0,
    "VISIBLE": True,
    "COLOR": "#FFFFFF",
    "BEND_ANGLE_X": 0.0,
    "BEND_ANGLE_Y": 0.0,
    "BEND_ANGLE_Z": 0.0,
    "LIGHT_COLOR": "#FFFFFF",
    "LIGHT_STRENGTH": 1.0,
    "LIGHT_SPECULAR_STRENGTH": 1.0,
    "LIGHT_RANGE": 16.0,
    "LIGHT_FADE_SIZE": 0.0,
    "CAMERA_FOV": 70.0,
}


def frame0_state(timeline: dict[str, Any]) -> dict[str, Any]:
    """Merge engine defaults, timeline defaults, then only the frame-zero key."""
    state = dict(ENGINE_DEFAULTS)
    defaults = timeline.get("default_values", {})
    if isinstance(defaults, dict):
        state.update(defaults)
    keyframes = timeline.get("keyframes", {})
    if isinstance(keyframes, dict):
        zero = keyframes.get("0")
        if zero is None:
            zero = keyframes.get(0)
        if isinstance(zero, dict):
            state.update(zero)
    if timeline.get("hide"):
        state["VISIBLE"] = False
    return state


def mi_position(state: dict[str, Any], unit_scale: bool = True) -> tuple[float, float, float]:
    """Convert Mine-imator scene-space X,Y,Z-up to Blender X,-Y,Z."""
    factor = 1.0 / MI_UNITS_PER_BLOCK if unit_scale else 1.0
    return (
        float(state.get("POS_X", 0.0)) * factor,
        -float(state.get("POS_Y", 0.0)) * factor,
        float(state.get("POS_Z", 0.0)) * factor,
    )


def mi_vector(value: Iterable[float], unit_scale: bool = True) -> tuple[float, float, float]:
    """Convert .mimodel local X,Y-up,Z to Blender X,-Z,Y-up."""
    values = list(value)
    values = (values + [0.0, 0.0, 0.0])[:3]
    factor = 1.0 / MI_UNITS_PER_BLOCK if unit_scale else 1.0
    return (float(values[0]) * factor, -float(values[2]) * factor, float(values[1]) * factor)


def mi_scale(state: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(state.get("SCA_X", state.get("SCALE_X", 1.0))),
        float(state.get("SCA_Y", state.get("SCALE_Y", 1.0))),
        float(state.get("SCA_Z", state.get("SCALE_Z", 1.0))),
    )


def model_scale(state: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(state.get("SCA_X", state.get("SCALE_X", 1.0))),
        float(state.get("SCA_Z", state.get("SCALE_Z", 1.0))),
        float(state.get("SCA_Y", state.get("SCALE_Y", 1.0))),
    )


def mi_rotation_radians(state: dict[str, Any]) -> tuple[float, float, float]:
    # Scene-space basis conversion X,Y,Z -> X,-Y,Z (a handedness flip).
    return tuple(
        math.radians(value)
        for value in (
            -float(state.get("ROT_X", 0.0)),
            float(state.get("ROT_Y", 0.0)),
            -float(state.get("ROT_Z", 0.0)),
        )
    )


def model_rotation_radians(state: dict[str, Any]) -> tuple[float, float, float]:
    """Convert .mimodel local rotation axes into Blender local axes."""
    return tuple(
        math.radians(value)
        for value in (
            float(state.get("ROT_X", 0.0)),
            -float(state.get("ROT_Z", 0.0)),
            float(state.get("ROT_Y", 0.0)),
        )
    )


def bend_rotation_radians(state: dict[str, Any]) -> tuple[float, float, float]:
    return tuple(
        math.radians(value)
        for value in (
            float(state.get("BEND_ANGLE_X", 0.0)),
            -float(state.get("BEND_ANGLE_Z", 0.0)),
            float(state.get("BEND_ANGLE_Y", 0.0)),
        )
    )


def world_bounds(resource: dict[str, Any]) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """Convert Mine-imator's saved X,Z,Y region order into Mineways X,Y,Z."""
    start = resource.get("start") or resource.get("world_start")
    end = resource.get("end") or resource.get("world_end")
    if not (isinstance(start, list) and isinstance(end, list) and len(start) >= 3 and len(end) >= 3):
        return None
    return (
        (int(start[0]), int(start[2]), int(start[1])),
        (int(end[0]), int(end[2]), int(end[1])),
    )


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "asset"


def find_mineways(override: str = "") -> Path | None:
    """Find Mineways in common portable and installed locations."""
    candidates: list[Path] = []
    if override:
        supplied = Path(override).expanduser()
        candidates.append(supplied / "Mineways.exe" if supplied.is_dir() else supplied)
    home = Path.home()
    candidates.extend((
        home / "Mineways" / "Mineways.exe",
        home / "Documents" / "Mineways" / "Mineways.exe",
        home / "Documents" / "Codex" / "Mineways" / "Mineways.exe",
        home / "Documents" / "Codex" / "Mineways" / "mineways_min" / "Mineways.exe",
        home / "Downloads" / "Mineways" / "Mineways.exe",
        Path("C:/Mineways/Mineways.exe"),
        Path("C:/Program Files/Mineways/Mineways.exe"),
        Path("C:/Program Files (x86)/Mineways/Mineways.exe"),
    ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def find_mineimator_install(project_path: Path | None = None, override: str = "") -> Path | None:
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    if project_path:
        for parent in [project_path.parent, *project_path.parents]:
            candidates.append(parent)
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / "Mine-imator")
    for candidate in candidates:
        root = candidate.parent if candidate.is_file() else candidate
        if (root / "Data" / "settings.midata").is_file():
            return root.resolve()
    return None


def read_asset_version(install_dir: Path) -> str:
    settings = load_json(install_dir / "Data" / "settings.midata")
    version = settings.get("version")
    assets = settings.get("assets", {})
    if not version and isinstance(assets, dict):
        version = assets.get("version")
    if not version:
        minecraft = settings.get("minecraft", {})
        if isinstance(minecraft, dict):
            version = minecraft.get("version")
    if not version:
        raise BridgeError("Mine-imator settings.midata has no Minecraft asset version")
    return str(version)


@dataclass
class AssetStore:
    install_dir: Path
    asset_version: str
    metadata: dict[str, Any]
    zip_path: Path
    extract_root: Path
    _names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def open(cls, install_dir: Path, asset_version: str | None = None) -> "AssetStore":
        version = asset_version or read_asset_version(install_dir)
        minecraft_dir = install_dir / "Data" / "Minecraft"
        metadata_path = minecraft_dir / f"{version}.midata"
        zip_path = minecraft_dir / f"{version}.zip"
        return cls.open_paths(install_dir, version, metadata_path, zip_path)

    @classmethod
    def open_paths(cls, install_dir: Path, version: str, metadata_path: Path, zip_path: Path) -> "AssetStore":
        if not metadata_path.is_file() or not zip_path.is_file():
            raise BridgeError(f"Mine-imator asset data for {version} is incomplete")
        digest = hashlib.sha1(str(zip_path).encode("utf-8")).hexdigest()[:10]
        extract_root = Path(tempfile.gettempdir()) / "mineimator_mcprep_bridge" / f"{version}-{digest}"
        store = cls(install_dir, version, load_json(metadata_path), zip_path, extract_root)
        with zipfile.ZipFile(zip_path) as archive:
            store._names = {name.lower(): name for name in archive.namelist() if not name.endswith("/")}
        return store

    def _resolve_archive_name(self, name: str) -> str | None:
        clean = str(PurePosixPath(str(name).replace("\\", "/"))).lstrip("/")
        candidates = [clean, f"assets/minecraft/{clean}"]
        for candidate in list(candidates):
            if not PurePosixPath(candidate).suffix:
                candidates.extend([f"{candidate}.png", f"{candidate}.json", f"{candidate}.mimodel"])
        for candidate in candidates:
            actual = self._names.get(candidate.lower())
            if actual:
                return actual
        suffix = clean.lower()
        matches = [actual for lower, actual in self._names.items() if lower.endswith(suffix)]
        return min(matches, key=len) if matches else None

    def read_bytes(self, name: str) -> bytes | None:
        actual = self._resolve_archive_name(name)
        if not actual:
            return None
        with zipfile.ZipFile(self.zip_path) as archive:
            return archive.read(actual)

    def read_json(self, name: str) -> dict[str, Any] | None:
        payload = self.read_bytes(name)
        if payload is None:
            return None
        try:
            data = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def materialize(self, name: str) -> Path | None:
        actual = self._resolve_archive_name(name)
        if not actual:
            return None
        target = self.extract_root / PurePosixPath(actual)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(self.zip_path) as archive:
                payload = archive.read(actual)
            target.write_bytes(payload)
        return target

    def records(self, category: str) -> list[dict[str, Any]]:
        values = self.metadata.get(category, [])
        return values if isinstance(values, list) else []

    def find_record(self, category: str, name: Any) -> dict[str, Any] | None:
        needle = str(name or "").lower().replace("minecraft:", "")
        if not needle:
            return None
        for record in self.records(category):
            fields = [record.get(key) for key in ("name", "id", "display_name", "identifier")]
            if any(str(field or "").lower().replace("minecraft:", "") == needle for field in fields):
                return record
        return None


def project_resource_path(project: ProjectIndex, resource: dict[str, Any] | None) -> Path | None:
    if not resource:
        return None
    filename = resource.get("filename") or resource.get("file") or resource.get("path")
    if not filename:
        return None
    path = Path(str(filename).replace("/", os.sep))
    candidates = [path] if path.is_absolute() else []
    candidates += [project.project_dir / path, project.project_dir / path.name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_mimodel(path: Path) -> dict[str, Any]:
    data = load_json(path)
    if "parts" not in data:
        raise BridgeError(f"Model has no parts: {path}")
    return data


def count_model_shapes(model: dict[str, Any]) -> dict[str, int]:
    counts = {"parts": 0, "block": 0, "plane": 0, "other": 0}

    def visit(parts: list[dict[str, Any]]) -> None:
        for part in parts:
            counts["parts"] += 1
            for shape in part.get("shapes", []):
                shape_type = str(shape.get("type", "block")).lower()
                counts[shape_type if shape_type in {"block", "plane"} else "other"] += 1
            visit(part.get("parts", []))

    visit(model.get("parts", []))
    return counts


def unique_name(base: str, existing: Iterable[str]) -> str:
    taken = set(existing)
    if base not in taken:
        return base
    index = 2
    while f"{base} ({index})" in taken:
        index += 1
    return f"{base} ({index})"
