"""Manifest model: records source, levels, tiles and content hashes.

The manifest is the single source of truth for incremental rebuilds.
It is written atomically and only after every tile it references has
been durably written, so an interrupted run can never leave a manifest
that vouches for half-written files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tile_relpath(z: int, x: int, y: int) -> str:
    return f"tiles/{z}/{x}_{y}.png"


def make_tile_entry(x: int, y: int, tile_size: int,
                    content_w: int, content_h: int, z: int,
                    sha256: str) -> dict:
    return {
        "x": x,
        "y": y,
        "file": tile_relpath(z, x, y),
        # Stored PNG is always a full tile_size square (zero-padded).
        "width": tile_size,
        "height": tile_size,
        # Valid (non-padded) pixels inside the tile; smaller on edges.
        "content_width": content_w,
        "content_height": content_h,
        "sha256": sha256,
    }


def dump_manifest(manifest: dict) -> bytes:
    # sort_keys + fixed separators => byte-stable manifest for identical input.
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_manifest(output_dir: Path) -> dict | None:
    path = Path(output_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return None
    return data


def verify_manifest(output_dir: Path, manifest: dict | None = None) -> list[str]:
    """Check every tile referenced by the manifest exists and matches its hash.

    Returns a list of human-readable problems; empty means fully valid.
    """
    output_dir = Path(output_dir)
    if manifest is None:
        manifest = load_manifest(output_dir)
    if manifest is None:
        return [f"no readable {MANIFEST_NAME} in {output_dir}"]
    problems: list[str] = []
    for level in manifest.get("levels", []):
        for tile in level.get("tiles", []):
            path = output_dir / tile["file"]
            if not path.is_file():
                problems.append(f"missing tile: {tile['file']}")
                continue
            actual = sha256_file(path)
            if actual != tile["sha256"]:
                problems.append(
                    f"hash mismatch: {tile['file']} "
                    f"(manifest {tile['sha256'][:12]}... != actual {actual[:12]}...)"
                )
    return problems
