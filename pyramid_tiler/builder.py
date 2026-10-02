"""Pyramid construction: levels, tiling, atomic writes, incremental rebuild.

Level algorithm
-----------
Level 0 is the source image at full resolution. Each subsequent level
is the previous level shrunk by exactly 2x with ceil division
(``(n + 1) // 2``), so odd dimensions stay fully covered. Generation
stops as soon as ``max(width, height) <= min_size``; that final level
is included in the pyramid.

Tile / coordinate rule
----------------------
Every level is cut into a grid of ``tile_size`` squares. Tile ``(x, y)``
covers pixels ``[x*ts, (x+1)*ts) x [y*ts, (y+1)*ts)`` with the origin
at the top-left. Edge tiles smaller than ``tile_size`` are zero-padded
(black for RGB, transparent for RGBA) up to the full tile size, so
every stored PNG has identical dimensions; the manifest records the
valid ``content_width``/``content_height`` of each tile.
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import resample
from .manifest import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    dump_manifest,
    load_manifest,
    make_tile_entry,
    sha256_bytes,
    sha256_file,
    tile_relpath,
)

TILE_SUFFIX = ".png"
TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class BuildConfig:
    tile_size: int = 256
    resample: str = resample.BILINEAR
    min_size: int = 256

    def __post_init__(self):
        if self.tile_size < 1:
            raise ValueError("tile_size must be >= 1")
        if self.min_size < 1:
            raise ValueError("min_size must be >= 1")
        if self.resample not in resample.METHODS:
            raise ValueError(f"resample must be one of {resample.METHODS}")

    def as_dict(self) -> dict:
        return {
            "tile_size": self.tile_size,
            "resample": self.resample,
            "min_size": self.min_size,
        }


@dataclass
class BuildResult:
    output_dir: Path
    manifest: dict
    tiles_total: int = 0
    tiles_written: int = 0
    tiles_reused: int = 0
    levels: int = 0
    written_files: list[str] = field(default_factory=list)


def load_source(path: Path) -> tuple[np.ndarray, str]:
    """Load a PNG/JPEG into a uint8 array; returns (array, mode).

    Modes are normalized to RGB or RGBA so padding semantics are clear.
    """
    with Image.open(path) as img:
        img.load()
        if img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        return np.asarray(img, dtype=np.uint8), img.mode


def compute_level_shapes(width: int, height: int, min_size: int) -> list[tuple[int, int]]:
    shapes = [(width, height)]
    while max(width, height) > min_size:
        width, height = (width + 1) // 2, (height + 1) // 2
        shapes.append((width, height))
    return shapes


def encode_tile(tile: np.ndarray, mode: str) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(tile, mode=mode).save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def pad_tile(region: np.ndarray, tile_size: int, mode: str) -> np.ndarray:
    """Zero-pad an edge region to a full tile (black RGB / transparent RGBA)."""
    h, w = region.shape[:2]
    if h == tile_size and w == tile_size:
        return region
    channels = region.shape[2] if region.ndim == 3 else 1
    out = np.zeros((tile_size, tile_size, channels), dtype=np.uint8)
    out[:h, :w] = region
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory + os.replace.

    A crash can leave a ``*.tmp`` file behind, but never a truncated
    file at the final path, and temp files are never referenced by the
    manifest.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=TMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _reusable_tiles(old_manifest: dict | None, source_sha: str, config: BuildConfig) -> dict:
    """Tiles from a previous manifest we may keep, keyed by (z, x, y)."""
    if not old_manifest:
        return {}
    source = old_manifest.get("source", {})
    if source.get("sha256") != source_sha:
        return {}
    if old_manifest.get("config") != config.as_dict():
        return {}
    reusable = {}
    for level in old_manifest.get("levels", []):
        for tile in level.get("tiles", []):
            reusable[(level["z"], tile["x"], tile["y"])] = tile
    return reusable


def build_pyramid(source_path: Path, output_dir: Path, config: BuildConfig | None = None) -> BuildResult:
    config = config or BuildConfig()
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_sha = sha256_file(source_path)
    pixels, mode = load_source(source_path)
    src_h, src_w = pixels.shape[:2]

    old_manifest = load_manifest(output_dir)
    reusable = _reusable_tiles(old_manifest, source_sha, config)

    result = BuildResult(output_dir=output_dir, manifest={}, levels=0)
    level_shapes = compute_level_shapes(src_w, src_h, config.min_size)
    manifest_levels = []

    level_pixels = pixels
    for z, (level_w, level_h) in enumerate(level_shapes):
        if z > 0:
            level_pixels = resample.halve(level_pixels, config.resample)

        tiles_x = (level_w + config.tile_size - 1) // config.tile_size
        tiles_y = (level_h + config.tile_size - 1) // config.tile_size
        tile_entries = []

        for ty in range(tiles_y):
            for tx in range(tiles_x):
                result.tiles_total += 1
                key = (z, tx, ty)
                rel = tile_relpath(z, tx, ty)
                dest = output_dir / rel
                old = reusable.get(key)

                if old is not None and old.get("file") == rel and dest.is_file():
                    if sha256_file(dest) == old["sha256"]:
                        tile_entries.append(old)
                        result.tiles_reused += 1
                        continue
                    # Corrupt or stale tile on disk: fall through and rewrite.

                y0, x0 = ty * config.tile_size, tx * config.tile_size
                region = level_pixels[y0:y0 + config.tile_size, x0:x0 + config.tile_size]
                content_h, content_w = region.shape[:2]
                tile = pad_tile(region, config.tile_size, mode)
                data = encode_tile(tile, mode)
                digest = sha256_bytes(data)
                _atomic_write(dest, data)
                result.tiles_written += 1
                result.written_files.append(rel)
                tile_entries.append(
                    make_tile_entry(tx, ty, config.tile_size, content_w, content_h, z, digest)
                )

        manifest_levels.append({
            "z": z,
            "width": level_w,
            "height": level_h,
            "tiles_x": tiles_x,
            "tiles_y": tiles_y,
            "tiles": tile_entries,
        })

    result.levels = len(manifest_levels)
    manifest = {
        "version": MANIFEST_VERSION,
        "generator": f"pyramid-tiler (local, deterministic; resample={config.resample})",
        "source": {
            "file": source_path.name,
            "sha256": source_sha,
            "width": src_w,
            "height": src_h,
        },
        "config": config.as_dict(),
        "levels": manifest_levels,
    }
    # Manifest goes last and atomically: an interrupted run leaves the
    # previous manifest (or none), never one pointing at half files.
    _atomic_write(output_dir / MANIFEST_NAME, dump_manifest(manifest))
    result.manifest = manifest
    return result
