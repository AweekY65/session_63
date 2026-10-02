"""Core pyramid / tile generation logic.

Design rules (also documented in README.md):

* Level 0 is the source image at full resolution. Level ``z + 1`` is
  produced by downscaling level ``z`` by a factor of two (progressive
  halving) with dimensions ``ceil(w / 2) x ceil(h / 2)``. Generation
  stops at the first level whose ``max(width, height) <= min_size``.
* Tiles are addressed by ``(level, col, row)`` with the origin at the
  top-left corner. Tile ``(col, row)`` covers the pixel rectangle
  ``[col*tile_size, (col+1)*tile_size) x [row*tile_size, (row+1)*tile_size)``.
* Edge rule: edge tiles are *cropped*, never padded. A tile that would
  extend past the image boundary is stored at its real (smaller) size;
  the manifest records the exact width/height of every tile.
* Tiles are always written as PNG so output bytes are deterministic for
  a given input and configuration.
* All writes go through a temporary file in the same directory followed
  by ``os.replace`` (atomic rename). ``manifest.json`` is written last,
  only after every tile succeeded, so an interrupted run can never leave
  a manifest that vouches for half-written files.
* Incremental rebuild: if the previous manifest exists and its source
  hash + configuration match the current request, tiles whose file
  exists and whose sha256 matches the manifest entry are kept as-is;
  missing or corrupted tiles are regenerated. Otherwise a full rebuild
  is performed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # local trusted inputs only

TILE_FORMAT = "PNG"
TILE_EXT = ".png"
MANIFEST_NAME = "manifest.json"
TILES_DIRNAME = "tiles"
TMP_PREFIX = ".tmp-"

_RESAMPLE_FILTERS = {
    "nearest": Image.Resampling.NEAREST,
    "bilinear": Image.Resampling.BILINEAR,
}

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Config:
    tile_size: int = 256
    resample: str = "bilinear"  # "nearest" | "bilinear"
    min_size: int = 256

    def __post_init__(self) -> None:
        if self.tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if self.min_size <= 0:
            raise ValueError("min_size must be positive")
        if self.resample not in _RESAMPLE_FILTERS:
            raise ValueError(
                f"unsupported resample method {self.resample!r}; "
                f"choose from {sorted(_RESAMPLE_FILTERS)}"
            )

    def to_dict(self) -> dict:
        return {
            "tile_size": self.tile_size,
            "resample": self.resample,
            "min_size": self.min_size,
        }


@dataclass
class BuildResult:
    manifest_path: Path
    levels: int
    tiles_total: int
    tiles_written: int
    tiles_reused: int
    full_rebuild: bool


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_levels(width: int, height: int, min_size: int) -> List[Tuple[int, int]]:
    """Return [(w0, h0), (w1, h1), ...] for every pyramid level."""
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    levels = [(width, height)]
    while max(width, height) > min_size:
        width = math.ceil(width / 2)
        height = math.ceil(height / 2)
        levels.append((width, height))
    return levels


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=TMP_PREFIX, suffix=path.suffix
    )
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


def _sweep_temp_files(root: Path) -> int:
    """Remove leftover temp files from a previously interrupted run."""
    removed = 0
    if not root.is_dir():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(TMP_PREFIX):
                try:
                    os.unlink(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def _tile_relpath(level: int, col: int, row: int) -> str:
    return f"{TILES_DIRNAME}/{level}/{col}_{row}{TILE_EXT}"


def _load_manifest(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return None
    return data


def _valid_tile_index(manifest: dict, out_dir: Path) -> Dict[str, str]:
    """Map tile relpath -> sha256 for entries whose file exists and matches."""
    valid: Dict[str, str] = {}
    for level in manifest.get("levels", []):
        for tile in level.get("tiles", []):
            rel = tile.get("file")
            expected = tile.get("sha256")
            if not rel or not expected:
                continue
            target = out_dir / rel
            if target.is_file() and sha256_file(target) == expected:
                valid[rel] = expected
    return valid


def _render_tile_png(image: Image.Image, box: Tuple[int, int, int, int]) -> bytes:
    tile = image.crop(box)
    from io import BytesIO

    buffer = BytesIO()
    # Fixed parameters keep the output bytes deterministic.
    tile.save(buffer, format=TILE_FORMAT, compress_level=6)
    return buffer.getvalue()


def build_pyramid(
    source: Path,
    out_dir: Path,
    config: Optional[Config] = None,
    *,
    _tile_hook=None,
) -> BuildResult:
    """Build (or incrementally rebuild) the tile pyramid for *source*.

    ``_tile_hook`` is a test-only hook invoked as
    ``_tile_hook(level, col, row, png_bytes)`` right before a tile is
    written; it may raise to simulate a mid-build failure.
    """
    config = config or Config()
    source = Path(source)
    out_dir = Path(out_dir)
    if not source.is_file():
        raise FileNotFoundError(f"source image not found: {source}")

    source_hash = sha256_file(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles_root = out_dir / TILES_DIRNAME
    _sweep_temp_files(out_dir)

    with Image.open(source) as probe:
        probe.load()
        base = probe.convert("RGB")
    src_width, src_height = base.size

    level_sizes = compute_levels(src_width, src_height, config.min_size)

    manifest_path = out_dir / MANIFEST_NAME
    previous = _load_manifest(manifest_path)
    reusable: Dict[str, str] = {}
    full_rebuild = True
    if (
        previous is not None
        and previous.get("source", {}).get("sha256") == source_hash
        and previous.get("config") == config.to_dict()
    ):
        reusable = _valid_tile_index(previous, out_dir)
        full_rebuild = False

    if full_rebuild and tiles_root.is_dir():
        # Output directory is tool-managed; drop stale tiles from a
        # previous configuration so they cannot be mistaken as valid.
        import shutil

        shutil.rmtree(tiles_root)

    resample_filter = _RESAMPLE_FILTERS[config.resample]
    manifest_levels: List[dict] = []
    tiles_total = 0
    tiles_written = 0

    current = base
    for level_index, (level_w, level_h) in enumerate(level_sizes):
        if level_index > 0:
            current = current.resize((level_w, level_h), resample_filter)

        cols = math.ceil(level_w / config.tile_size)
        rows = math.ceil(level_h / config.tile_size)
        tile_entries: List[dict] = []

        for row in range(rows):
            for col in range(cols):
                left = col * config.tile_size
                upper = row * config.tile_size
                right = min(left + config.tile_size, level_w)
                lower = min(upper + config.tile_size, level_h)
                rel = _tile_relpath(level_index, col, row)
                tiles_total += 1

                if rel in reusable:
                    tile_entries.append(
                        {
                            "col": col,
                            "row": row,
                            "file": rel,
                            "width": right - left,
                            "height": lower - upper,
                            "sha256": reusable[rel],
                        }
                    )
                    continue

                png_bytes = _render_tile_png(current, (left, upper, right, lower))
                if _tile_hook is not None:
                    _tile_hook(level_index, col, row, png_bytes)
                _atomic_write_bytes(out_dir / rel, png_bytes)
                tiles_written += 1
                tile_entries.append(
                    {
                        "col": col,
                        "row": row,
                        "file": rel,
                        "width": right - left,
                        "height": lower - upper,
                        "sha256": sha256_bytes(png_bytes),
                    }
                )

        manifest_levels.append(
            {
                "level": level_index,
                "width": level_w,
                "height": level_h,
                "cols": cols,
                "rows": rows,
                "tiles": tile_entries,
            }
        )

    manifest = {
        "version": MANIFEST_VERSION,
        "generator": "pytile",
        "source": {
            "name": source.name,
            "sha256": source_hash,
            "width": src_width,
            "height": src_height,
        },
        "config": config.to_dict(),
        "tile_format": TILE_FORMAT,
        "edge_rule": "crop",
        "levels": manifest_levels,
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write_bytes(manifest_path, payload)

    return BuildResult(
        manifest_path=manifest_path,
        levels=len(manifest_levels),
        tiles_total=tiles_total,
        tiles_written=tiles_written,
        tiles_reused=tiles_total - tiles_written,
        full_rebuild=full_rebuild,
    )
