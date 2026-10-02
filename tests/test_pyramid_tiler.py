"""Automated tests for the local pyramid tiler.

All tests generate small synthetic images on the fly, run entirely in
the terminal (no image windows), and print verification results via
pytest's normal reporting.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pyramid_tiler import resample
from pyramid_tiler.builder import (
    BuildConfig,
    build_pyramid,
    compute_level_shapes,
    load_source,
)
from pyramid_tiler.manifest import MANIFEST_NAME, load_manifest, verify_manifest


def make_test_image(path: Path, width: int, height: int, fmt: str = "PNG",
                    alpha: bool = False) -> np.ndarray:
    """Deterministic synthetic image: gradients + shapes, no randomness."""
    yy, xx = np.mgrid[0:height, 0:width]
    r = (xx * 255 // max(width - 1, 1)).astype(np.uint8)
    g = (yy * 255 // max(height - 1, 1)).astype(np.uint8)
    b = ((xx // 7 + yy // 5) % 2 * 255).astype(np.uint8)  # checker detail
    if alpha:
        a = np.where((xx + yy) % 3 == 0, 128, 255).astype(np.uint8)
        arr = np.stack([r, g, b, a], axis=-1)
        mode = "RGBA"
    else:
        arr = np.stack([r, g, b], axis=-1)
        mode = "RGB"
    Image.fromarray(arr, mode=mode).save(path, format=fmt)
    return arr


def read_tile(output_dir: Path, z: int, x: int, y: int) -> np.ndarray:
    with Image.open(output_dir / f"tiles/{z}/{x}_{y}.png") as img:
        return np.asarray(img)


# ---------------------------------------------------------------- levels

def test_level_shapes_odd_sizes():
    # 517x389 with min_size 64: 517->259->130->65->33, 389->195->98->49->25
    shapes = compute_level_shapes(517, 389, min_size=64)
    assert shapes[0] == (517, 389)
    assert shapes[-1] == (33, 25)
    for (w, h), (nw, nh) in zip(shapes, shapes[1:]):
        assert nw == (w + 1) // 2
        assert nh == (h + 1) // 2
    assert max(shapes[-1]) <= 64
    assert max(shapes[-2]) > 64


def test_level_shapes_stops_at_min_size():
    assert compute_level_shapes(100, 80, min_size=256) == [(100, 80)]
    assert compute_level_shapes(256, 256, min_size=256) == [(256, 256)]
    assert compute_level_shapes(257, 10, min_size=256) == [(257, 10), (129, 5)]


# ---------------------------------------------------------------- tiling

def test_odd_size_edge_tiles_and_padding(tmp_path):
    src = tmp_path / "odd.png"
    make_test_image(src, 517, 389)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=256, resample="bilinear", min_size=64)
    result = build_pyramid(src, out, config)

    manifest = load_manifest(out)
    assert manifest["source"]["width"] == 517
    assert manifest["source"]["height"] == 389

    level0 = manifest["levels"][0]
    assert (level0["width"], level0["height"]) == (517, 389)
    assert level0["tiles_x"] == 3  # ceil(517/256)
    assert level0["tiles_y"] == 2  # ceil(389/256)

    # Every stored tile is a full 256x256 PNG (zero-padding rule).
    for tile in level0["tiles"]:
        pixels = read_tile(out, 0, tile["x"], tile["y"])
        assert pixels.shape[:2] == (256, 256)
        assert tile["width"] == 256 and tile["height"] == 256

    # Bottom-right edge tile: valid content is 5 x 133.
    edge = next(t for t in level0["tiles"] if (t["x"], t["y"]) == (2, 1))
    assert (edge["content_width"], edge["content_height"]) == (5, 133)
    pixels = read_tile(out, 0, 2, 1)
    assert (pixels[:133, :5] != 0).any()          # real content present
    assert (pixels[:, 5:] == 0).all()             # padded columns are zero
    assert (pixels[133:, :] == 0).all()           # padded rows are zero

    # Reassembling level 0 from tiles reproduces the source exactly.
    source_pixels, _ = load_source(src)
    canvas = np.zeros((2 * 256, 3 * 256, 3), dtype=np.uint8)
    for tile in level0["tiles"]:
        canvas[tile["y"] * 256:(tile["y"] + 1) * 256,
               tile["x"] * 256:(tile["x"] + 1) * 256] = read_tile(out, 0, tile["x"], tile["y"])
    np.testing.assert_array_equal(canvas[:389, :517], source_pixels)


# ---------------------------------------------------------------- resample

def test_resample_methods_differ_and_are_deterministic(tmp_path):
    src = tmp_path / "in.png"
    make_test_image(src, 150, 111)
    pixels, _ = load_source(src)

    nearest = resample.halve(pixels, "nearest")
    bilinear = resample.halve(pixels, "bilinear")
    assert nearest.shape == (56, 75, 3)  # ceil halving of odd dims
    assert bilinear.shape == (56, 75, 3)
    assert not np.array_equal(nearest, bilinear)

    # Bit-for-bit stability across repeated runs.
    np.testing.assert_array_equal(resample.halve(pixels, "nearest"), nearest)
    np.testing.assert_array_equal(resample.halve(pixels, "bilinear"), bilinear)

    # Nearest picks exact source pixels; bilinear smooths (<= max of inputs).
    assert set(np.unique(nearest)) <= set(np.unique(pixels))
    assert bilinear.max() <= pixels.max()

    # Full builds with both methods yield stable, method-specific hashes.
    hashes = {}
    for method in ("nearest", "bilinear"):
        out = tmp_path / f"out_{method}"
        build_pyramid(src, out, BuildConfig(tile_size=64, resample=method, min_size=32))
        manifest = load_manifest(out)
        hashes[method] = [t["sha256"] for lv in manifest["levels"] for t in lv["tiles"]]
        # Rebuild from scratch: identical hashes => deterministic output.
        out2 = tmp_path / f"out_{method}_again"
        build_pyramid(src, out2, BuildConfig(tile_size=64, resample=method, min_size=32))
        manifest2 = load_manifest(out2)
        assert [t["sha256"] for lv in manifest2["levels"] for t in lv["tiles"]] == hashes[method]
    assert hashes["nearest"] != hashes["bilinear"]


def test_resample_rejects_unknown_method():
    with pytest.raises(ValueError):
        resample.resize(np.zeros((4, 4, 3), np.uint8), 2, 2, "cubic")
    with pytest.raises(ValueError):
        BuildConfig(resample="cubic")


# ---------------------------------------------------------------- formats

def test_jpeg_and_alpha_inputs(tmp_path):
    jpg = tmp_path / "photo.jpg"
    make_test_image(jpg, 200, 120, fmt="JPEG")
    out = tmp_path / "out_jpg"
    build_pyramid(jpg, out, BuildConfig(tile_size=64, min_size=32))
    assert verify_manifest(out) == []
    with Image.open(out / "tiles/0/0_0.png") as tile:
        assert tile.mode == "RGB"

    png_alpha = tmp_path / "alpha.png"
    make_test_image(png_alpha, 130, 70, alpha=True)
    out_a = tmp_path / "out_alpha"
    build_pyramid(png_alpha, out_a, BuildConfig(tile_size=64, min_size=32))
    assert verify_manifest(out_a) == []
    with Image.open(out_a / "tiles/0/2_1.png") as edge_tile:
        assert edge_tile.mode == "RGBA"
        # Padded corner of an RGBA edge tile is fully transparent.
        px = np.asarray(edge_tile)
        assert (px[69:, 2:, 3] == 0).all()


# ---------------------------------------------------------------- manifest

def test_manifest_contents(tmp_path):
    src = tmp_path / "m.png"
    make_test_image(src, 300, 200)
    out = tmp_path / "out"
    build_pyramid(src, out, BuildConfig(tile_size=128, min_size=64))
    manifest = json.loads((out / MANIFEST_NAME).read_text())

    assert manifest["version"] == 1
    assert manifest["config"] == {"tile_size": 128, "resample": "bilinear", "min_size": 64}
    assert manifest["source"]["width"] == 300
    assert len(manifest["source"]["sha256"]) == 64

    for level in manifest["levels"]:
        assert len(level["tiles"]) == level["tiles_x"] * level["tiles_y"]
        for tile in level["tiles"]:
            assert len(tile["sha256"]) == 64
            assert tile["file"] == f"tiles/{level['z']}/{tile['x']}_{tile['y']}.png"
            assert 1 <= tile["content_width"] <= 128
            assert 1 <= tile["content_height"] <= 128
    assert verify_manifest(out) == []


# ---------------------------------------------------------------- incremental

def test_incremental_rebuild_skips_unchanged_tiles(tmp_path):
    src = tmp_path / "inc.png"
    make_test_image(src, 300, 210)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)

    first = build_pyramid(src, out, config)
    assert first.tiles_written == first.tiles_total
    assert first.tiles_reused == 0

    second = build_pyramid(src, out, config)
    assert second.tiles_written == 0
    assert second.tiles_reused == second.tiles_total
    assert verify_manifest(out) == []


def test_corrupt_tile_is_regenerated(tmp_path):
    src = tmp_path / "corrupt.png"
    make_test_image(src, 200, 140)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)
    build_pyramid(src, out, config)

    victim = out / "tiles/0/1_0.png"
    original = victim.read_bytes()
    victim.write_bytes(b"corrupted garbage")
    assert verify_manifest(out) != []  # corruption is detected

    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1
    assert result.written_files == ["tiles/0/1_0.png"]
    assert victim.read_bytes() == original
    assert verify_manifest(out) == []


def test_missing_tile_is_regenerated(tmp_path):
    src = tmp_path / "missing.png"
    make_test_image(src, 200, 140)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)
    build_pyramid(src, out, config)

    (out / "tiles/1/0_0.png").unlink()
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1
    assert verify_manifest(out) == []


def test_changed_source_triggers_full_rebuild(tmp_path):
    src = tmp_path / "changing.png"
    make_test_image(src, 200, 140)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)
    build_pyramid(src, out, config)

    make_test_image(src, 200, 141)  # same name, different content
    result = build_pyramid(src, out, config)
    assert result.tiles_reused == 0
    assert result.tiles_written == result.tiles_total
    assert verify_manifest(out) == []


# ---------------------------------------------------------------- failure

def test_interrupted_build_leaves_no_half_files(tmp_path, monkeypatch):
    src = tmp_path / "fragile.png"
    make_test_image(src, 200, 140)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)

    import pyramid_tiler.builder as builder

    real_atomic_write = builder._atomic_write
    calls = {"count": 0}

    def failing_write(path, data):
        calls["count"] += 1
        if calls["count"] == 5:  # crash in the middle of tile writes
            raise RuntimeError("simulated power failure")
        return real_atomic_write(path, data)

    monkeypatch.setattr(builder, "_atomic_write", failing_write)
    with pytest.raises(RuntimeError, match="power failure"):
        build_pyramid(src, out, config)

    # No manifest was published, and no temp files were left behind.
    assert not (out / MANIFEST_NAME).exists()
    leftovers = [p for p in out.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []

    # A rerun from the messy state completes and fully verifies.
    monkeypatch.setattr(builder, "_atomic_write", real_atomic_write)
    result = build_pyramid(src, out, config)
    assert result.tiles_written >= 5  # previously written tiles are NOT
    assert verify_manifest(out) == []  # trusted (no manifest existed)


def test_stray_temp_file_is_never_treated_as_valid(tmp_path):
    src = tmp_path / "stray.png"
    make_test_image(src, 100, 80)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)
    build_pyramid(src, out, config)

    # Simulate a crash leftover next to a real tile.
    stray = out / "tiles/0" / "0_0.png.abcd.tmp"
    stray.write_bytes(b"half-written junk")
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 0  # real tiles still verified & reused
    assert verify_manifest(out) == []
    stray.unlink()


def test_truncated_tile_file_is_rewritten(tmp_path):
    src = tmp_path / "trunc.png"
    make_test_image(src, 100, 80)
    out = tmp_path / "out"
    config = BuildConfig(tile_size=64, min_size=32)
    build_pyramid(src, out, config)

    victim = out / "tiles/0/0_0.png"
    data = victim.read_bytes()
    victim.write_bytes(data[: len(data) // 2])  # truncated half-file
    assert verify_manifest(out) != []
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1
    assert victim.read_bytes() == data
    assert verify_manifest(out) == []
