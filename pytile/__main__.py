"""Command line interface: python -m pytile <image> --output DIR ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import Config, build_pyramid


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytile",
        description="Build a local image pyramid with tiles and a manifest. "
        "All data stays on the local filesystem; no external services.",
    )
    parser.add_argument("source", type=Path, help="input image (PNG/JPEG)")
    parser.add_argument(
        "--output", "-o", type=Path, required=True, help="output directory"
    )
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument(
        "--resample", choices=["nearest", "bilinear"], default="bilinear"
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=256,
        help="stop when max(level dimension) <= min-size",
    )
    args = parser.parse_args(argv)

    config = Config(
        tile_size=args.tile_size, resample=args.resample, min_size=args.min_size
    )
    result = build_pyramid(args.source, args.output, config)
    mode = "full rebuild" if result.full_rebuild else "incremental"
    print(
        f"[pytile] {mode}: {result.levels} level(s), "
        f"{result.tiles_total} tile(s) total, "
        f"{result.tiles_written} written, {result.tiles_reused} reused"
    )
    print(f"[pytile] manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
