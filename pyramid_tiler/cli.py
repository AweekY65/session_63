"""Command line interface: python -m pyramid_tiler ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .builder import BuildConfig, build_pyramid
from .manifest import verify_manifest
from .resample import METHODS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyramid-tiler",
        description="Local image pyramid + tile generator (no network, no external services).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build or incrementally rebuild a pyramid")
    build.add_argument("source", type=Path, help="input PNG/JPEG image")
    build.add_argument("-o", "--output", type=Path, required=True, help="output directory")
    build.add_argument("--tile-size", type=int, default=256)
    build.add_argument("--resample", choices=METHODS, default="bilinear")
    build.add_argument("--min-size", type=int, default=256,
                       help="stop when max(level width, height) <= this")

    verify = sub.add_parser("verify", help="verify tiles against the manifest hashes")
    verify.add_argument("output", type=Path, help="output directory containing manifest.json")

    args = parser.parse_args(argv)

    if args.command == "build":
        config = BuildConfig(tile_size=args.tile_size, resample=args.resample,
                             min_size=args.min_size)
        result = build_pyramid(args.source, args.output, config)
        print(f"levels:            {result.levels}")
        print(f"tiles total:       {result.tiles_total}")
        print(f"tiles written:     {result.tiles_written}")
        print(f"tiles reused:      {result.tiles_reused}")
        print(f"manifest:          {result.output_dir / 'manifest.json'}")
        return 0

    if args.command == "verify":
        problems = verify_manifest(args.output)
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}")
            return 1
        print("OK: all tiles match the manifest")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
