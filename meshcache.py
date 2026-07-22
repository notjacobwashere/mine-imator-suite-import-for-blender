"""Reader and discovery helpers for Mine-imator's format-2 world mesh cache.

The cache is a gzip-compressed Qt data stream. Integer counts are written by
QDataStream (big endian), while Vertex and index arrays are dumped from the
native Windows structs (little endian). Blender-side conversion lives in
``blender_importer`` so this module stays usable without ``bpy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator
import gzip
import struct


CACHE_FORMAT = 2
DEPTH_COUNT = 3
BUFFER_NAMES = (
    "normal",
    "animated",
    "grass",
    "foliage",
    "leaves_oak",
    "leaves_spruce",
    "leaves_birch",
    "leaves_jungle",
    "leaves_acacia",
    "leaves_dark_oak",
    "leaves_mangrove",
    "water",
)
VERTEX_SIZE = 36


@dataclass(frozen=True, slots=True)
class CacheHeader:
    format: int
    size: tuple[int, int, int]


@dataclass(slots=True)
class RawMesh:
    depth: int
    buffer_index: int
    mesh_index: int
    vertex_count: int
    index_count: int
    vertices: bytes
    indices: bytes

    @property
    def buffer_name(self) -> str:
        return BUFFER_NAMES[self.buffer_index]


def _read_exact(handle: BinaryIO, amount: int) -> bytes:
    payload = handle.read(amount)
    if len(payload) != amount:
        raise ValueError(f"Truncated Mine-imator meshcache: wanted {amount} bytes, got {len(payload)}")
    return payload


def read_header(path: Path) -> CacheHeader:
    with gzip.open(path, "rb") as handle:
        cache_format = struct.unpack(">B", _read_exact(handle, 1))[0]
        size = struct.unpack(">qqq", _read_exact(handle, 24))
    if cache_format != CACHE_FORMAT:
        raise ValueError(f"Unsupported Mine-imator meshcache format {cache_format}")
    if any(value <= 0 or value > 1_000_000 for value in size):
        raise ValueError(f"Invalid Mine-imator meshcache dimensions: {size}")
    return CacheHeader(cache_format, size)


def iter_raw_meshes(path: Path) -> Iterator[RawMesh]:
    with gzip.open(path, "rb") as handle:
        cache_format = struct.unpack(">B", _read_exact(handle, 1))[0]
        if cache_format != CACHE_FORMAT:
            raise ValueError(f"Unsupported Mine-imator meshcache format {cache_format}")
        _read_exact(handle, 24)  # scenery dimensions, already available via read_header
        for depth in range(DEPTH_COUNT):
            for buffer_index in range(len(BUFFER_NAMES)):
                mesh_count = struct.unpack(">q", _read_exact(handle, 8))[0]
                if mesh_count < 0 or mesh_count > 100_000:
                    raise ValueError(f"Invalid mesh count {mesh_count} in meshcache")
                for mesh_index in range(mesh_count):
                    vertex_count, index_count = struct.unpack(">qq", _read_exact(handle, 16))
                    if vertex_count < 0 or index_count < 0 or index_count % 3:
                        raise ValueError(
                            f"Invalid mesh sizes ({vertex_count}, {index_count}) in meshcache"
                        )
                    yield RawMesh(
                        depth,
                        buffer_index,
                        mesh_index,
                        vertex_count,
                        index_count,
                        _read_exact(handle, vertex_count * VERTEX_SIZE),
                        _read_exact(handle, index_count * 4),
                    )


def discover(project_path: Path, resource_name: str) -> Path | None:
    """Find the cache locally, then in sibling Mine-imator project folders.

    Mine-imator projects are commonly duplicated by Save As without copying
    their large cache. Searching sibling project directories lets such copies
    reuse the original cache, as Mine-imator itself does while the project is
    open.
    """
    project_dir = project_path.parent
    wanted = str(resource_name or "").strip().casefold()
    local = list(project_dir.glob("*.meshcache"))
    exact_local = [path for path in local if path.stem.casefold() == wanted]
    if exact_local:
        return max(exact_local, key=lambda path: path.stat().st_mtime)
    if len(local) == 1:
        return local[0]

    projects_root = project_dir.parent
    if not projects_root.is_dir():
        return None
    matches: list[Path] = []
    for path in projects_root.rglob("*.meshcache"):
        if path.stem.casefold() == wanted:
            matches.append(path)
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None
