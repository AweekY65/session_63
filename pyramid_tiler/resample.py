"""Deterministic image resampling (nearest / bilinear).

Implemented directly on numpy arrays so results depend only on the
input pixels and the algorithm -- never on library internals that may
change between versions. All arithmetic uses float64 and a single
final rounding step, so the same input always yields the same bytes.
"""

from __future__ import annotations

import numpy as np

NEAREST = "nearest"
BILINEAR = "bilinear"
METHODS = (NEAREST, BILINEAR)


def _source_positions(dst_size: int, src_size: int) -> np.ndarray:
    """Map destination pixel centers to source coordinates.

    Uses the standard "pixel center" convention (align_corners=False):
    src = (dst + 0.5) * (src_size / dst_size) - 0.5
    """
    scale = src_size / dst_size
    return (np.arange(dst_size, dtype=np.float64) + 0.5) * scale - 0.5


def resize_nearest(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    src_h, src_w = img.shape[:2]
    ys = np.clip(np.floor(_source_positions(out_h, src_h) + 0.5).astype(np.int64), 0, src_h - 1)
    xs = np.clip(np.floor(_source_positions(out_w, src_w) + 0.5).astype(np.int64), 0, src_w - 1)
    return img[ys][:, xs]


def resize_bilinear(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    src_h, src_w = img.shape[:2]
    work = img.astype(np.float64)

    # Interpolate along rows (x axis) first, then columns (y axis).
    xs = _source_positions(out_w, src_w)
    x0 = np.clip(np.floor(xs).astype(np.int64), 0, src_w - 1)
    x1 = np.clip(x0 + 1, 0, src_w - 1)
    fx = np.clip(xs - np.floor(xs), 0.0, 1.0)
    if img.ndim == 3:
        fx_b = fx[None, :, None]
    else:
        fx_b = fx[None, :]
    rows = work[:, x0] * (1.0 - fx_b) + work[:, x1] * fx_b

    ys = _source_positions(out_h, src_h)
    y0 = np.clip(np.floor(ys).astype(np.int64), 0, src_h - 1)
    y1 = np.clip(y0 + 1, 0, src_h - 1)
    fy = np.clip(ys - np.floor(ys), 0.0, 1.0)
    if img.ndim == 3:
        fy_b = fy[:, None, None]
    else:
        fy_b = fy[:, None]
    out = rows[y0] * (1.0 - fy_b) + rows[y1] * fy_b

    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def resize(img: np.ndarray, out_h: int, out_w: int, method: str) -> np.ndarray:
    if method == NEAREST:
        return resize_nearest(img, out_h, out_w)
    if method == BILINEAR:
        return resize_bilinear(img, out_h, out_w)
    raise ValueError(f"unknown resample method: {method!r} (expected one of {METHODS})")


def halve(img: np.ndarray, method: str) -> np.ndarray:
    """Shrink by exactly 2x using ceil division so odd sizes keep coverage."""
    src_h, src_w = img.shape[:2]
    return resize(img, (src_h + 1) // 2, (src_w + 1) // 2, method)
