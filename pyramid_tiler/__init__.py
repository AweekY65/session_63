"""Local image pyramid and tile generator.

Everything (sources, levels, tiles, manifest, caches) lives in local
files or in memory. No map server, CDN, cloud storage or any external
service is ever contacted.
"""

from .builder import build_pyramid, BuildConfig, BuildResult
from .manifest import load_manifest, verify_manifest

__all__ = [
    "build_pyramid",
    "BuildConfig",
    "BuildResult",
    "load_manifest",
    "verify_manifest",
]

__version__ = "1.0.0"
