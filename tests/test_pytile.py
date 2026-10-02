"""Automated tests for pytile. All checks run in the terminal; no image
windows are opened. Run with: python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from pytile import Config, build_pyramid, compute_levels, sha256_file


def make_test_image(path: Path, size=(517, 389), fmt="PNG") -> Path:
    """Deterministic gradient image so builds are reproducible."""
    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 3 + y) % 256, (x + y * 5) % 256, (x * y) % 256)
    image.save(path, format=fmt)
    return path


def read_manifest(out_dir: Path) -> dict:
    with open(out_dir / "manifest.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def tile_mtimes(out_dir: Path) -> dict:
    return {
        str(p.relative_to(out_dir)): p.stat().st_mtime_ns
        for p in sorted((out_dir / "tiles").rglob("*.png"))
    }


# ---------------------------------------------------------------- levels


def test_compute_levels_odd_sizes():
    levels = compute_levels(517, 389, min_size=64)
    assert levels[0] == (517, 389)
    # ceil halving at every step
    for (w0, h0), (w1, h1) in zip(levels, levels[1:]):
        assert w1 == -(-w0 // 2)
        assert h1 == -(-h0 // 2)
    # stops at the first level whose max dimension <= min_size
    assert max(levels[-1]) <= 64
    assert max(levels[-2]) > 64


def test_compute_levels_small_image_single_level():
    assert compute_levels(100, 80, min_size=256) == [(100, 80)]


# ---------------------------------------------------------------- tiles


def test_edge_tiles_are_cropped_not_padded(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(517, 389))
    config = Config(tile_size=256, min_size=512)  # single level
    build_pyramid(src, tmp_path / "out", config)
    manifest = read_manifest(tmp_path / "out")
    level0 = manifest["levels"][0]
    assert (level0["cols"], level0["rows"]) == (3, 2)
    sizes = {(t["col"], t["row"]): (t["width"], t["height"]) for t in level0["tiles"]}
    # right edge: 517 - 2*256 = 5 px wide; bottom edge: 389 - 256 = 133 px
    assert sizes[(0, 0)] == (256, 256)
    assert sizes[(2, 0)] == (5, 256)
    assert sizes[(0, 1)] == (256, 133)
    assert sizes[(2, 1)] == (5, 133)
    # the file on disk really has the cropped size
    with Image.open(tmp_path / "out" / "tiles" / "0" / "2_1.png") as tile:
        assert tile.size == (5, 133)


def test_manifest_hashes_match_files(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    build_pyramid(src, tmp_path / "out", Config(tile_size=128, min_size=64))
    manifest = read_manifest(tmp_path / "out")
    assert manifest["source"]["width"] == 300
    assert manifest["source"]["height"] == 200
    assert manifest["source"]["sha256"] == sha256_file(src)
    for level in manifest["levels"]:
        for tile in level["tiles"]:
            target = tmp_path / "out" / tile["file"]
            assert target.is_file()
            assert sha256_file(target) == tile["sha256"]


# ------------------------------------------------------------- resampling


def test_nearest_downscale_matches_manual_expectation(tmp_path):
    # 4x4 image, nearest 2x downscale: Pillow maps each destination
    # pixel centre back into the source, i.e. dst 0 -> src 1, dst 1 ->
    # src 3 for a factor-2 reduction.
    image = Image.new("RGB", (4, 4))
    for y in range(4):
        for x in range(4):
            image.putpixel((x, y), (x * 40, y * 40, 0))
    src = tmp_path / "tiny.png"
    image.save(src)
    build_pyramid(src, tmp_path / "out", Config(tile_size=8, resample="nearest", min_size=2))
    with Image.open(tmp_path / "out" / "tiles" / "1" / "0_0.png") as tile:
        assert tile.size == (2, 2)
        assert tile.getpixel((0, 0)) == (40, 40, 0)
        assert tile.getpixel((1, 0)) == (120, 40, 0)
        assert tile.getpixel((0, 1)) == (40, 120, 0)
        assert tile.getpixel((1, 1)) == (120, 120, 0)


@pytest.mark.parametrize("resample", ["nearest", "bilinear"])
def test_same_input_gives_identical_output(tmp_path, resample):
    src = make_test_image(tmp_path / "src.png", size=(300, 211))
    config = Config(tile_size=64, resample=resample, min_size=32)
    build_pyramid(src, tmp_path / "out_a", config)
    build_pyramid(src, tmp_path / "out_b", config)
    manifest_a = read_manifest(tmp_path / "out_a")
    manifest_b = read_manifest(tmp_path / "out_b")
    assert manifest_a == manifest_b  # includes every tile sha256


def test_nearest_and_bilinear_differ_on_gradient(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(128, 128))
    build_pyramid(src, tmp_path / "near", Config(resample="nearest", min_size=32))
    build_pyramid(src, tmp_path / "bilin", Config(resample="bilinear", min_size=32))
    near = read_manifest(tmp_path / "near")["levels"][1]["tiles"][0]["sha256"]
    bilin = read_manifest(tmp_path / "bilin")["levels"][1]["tiles"][0]["sha256"]
    assert near != bilin


# ------------------------------------------------------------ incremental


def test_incremental_rebuild_writes_nothing(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)
    first = build_pyramid(src, out, config)
    assert first.tiles_written == first.tiles_total > 0
    before = tile_mtimes(out)
    second = build_pyramid(src, out, config)
    assert not second.full_rebuild
    assert second.tiles_written == 0
    assert second.tiles_reused == second.tiles_total
    assert tile_mtimes(out) == before  # no tile was touched


def test_corrupted_tile_is_regenerated(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)
    build_pyramid(src, out, config)
    victim = out / "tiles" / "0" / "1_0.png"
    original_hash = sha256_file(victim)
    victim.write_bytes(b"corrupted")
    before = tile_mtimes(out)
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1
    assert sha256_file(victim) == original_hash
    after = tile_mtimes(out)
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"tiles/0/1_0.png"}


def test_missing_tile_is_regenerated(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)
    build_pyramid(src, out, config)
    (out / "tiles" / "1" / "0_0.png").unlink()
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1
    assert (out / "tiles" / "1" / "0_0.png").is_file()


def test_changed_input_triggers_full_rebuild(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)
    build_pyramid(src, out, config)
    make_test_image(src, size=(200, 200))  # overwrite source
    result = build_pyramid(src, out, config)
    assert result.full_rebuild
    assert result.tiles_written == result.tiles_total
    manifest = read_manifest(out)
    assert manifest["source"]["width"] == 200


def test_changed_config_triggers_full_rebuild(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    build_pyramid(src, out, Config(tile_size=128, min_size=64))
    result = build_pyramid(src, out, Config(tile_size=64, min_size=64))
    assert result.full_rebuild
    assert result.tiles_written == result.tiles_total


# ------------------------------------------------------ failure / atomicity


def test_interrupted_build_leaves_no_valid_half_state(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)

    calls = {"count": 0}

    def fail_on_third_tile(level, col, row, data):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        build_pyramid(src, out, config, _tile_hook=fail_on_third_tile)

    # manifest must not exist / must not vouch for the partial tiles
    assert not (out / "manifest.json").exists()
    # no leftover temp files
    leftovers = [p for p in out.rglob("*") if p.name.startswith(".tmp-")]
    assert leftovers == []
    # a normal rebuild succeeds and produces a fully consistent manifest
    result = build_pyramid(src, out, config)
    assert result.tiles_written == result.tiles_total
    manifest = read_manifest(out)
    for level in manifest["levels"]:
        for tile in level["tiles"]:
            assert sha256_file(out / tile["file"]) == tile["sha256"]


def test_interrupted_rebuild_keeps_previous_manifest(tmp_path):
    src = make_test_image(tmp_path / "src.png", size=(300, 200))
    out = tmp_path / "out"
    config = Config(tile_size=128, min_size=64)
    build_pyramid(src, out, config)
    good_manifest = (out / "manifest.json").read_bytes()

    # corrupt one tile, then crash while regenerating it
    (out / "tiles" / "0" / "0_0.png").write_bytes(b"broken")

    def crash(level, col, row, data):
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        build_pyramid(src, out, config, _tile_hook=crash)
    # previous manifest is untouched
    assert (out / "manifest.json").read_bytes() == good_manifest
    # next run repairs repairs the damage
    result = build_pyramid(src, out, config)
    assert result.tiles_written == 1


# ------------------------------------------------------------------- input


def test_jpeg_input_supported(tmp_path):
    src = make_test_image(tmp_path / "src.jpg", size=(200, 150), fmt="JPEG")
    out = tmp_path / "out"
    result = build_pyramid(src, out, Config(tile_size=128, min_size=64))
    manifest = read_manifest(out)
    assert manifest["source"]["name"] == "src.jpg"
    assert manifest["source"]["width"] == 200
    assert result.levels == len(manifest["levels"])


def test_invalid_resample_rejected():
    with pytest.raises(ValueError):
        Config(resample="cubic")
