"""Image analysis primitives used by the auto-exposure tuner, lucky imaging,
and live stacking pipelines.

Everything in here operates on JPEG bytes (or Path to a JPEG file) and
returns plain Python numbers / numpy arrays. No camera, no telescope, no
state -- pure functions so they're trivially testable against synthetic
inputs.

Why these specific metrics:
    luminance_stats   -- mean / median / clip stats let auto-tune decide
                         "too dark", "too bright", or "good".
    star_count        -- a smarter dark/bright signal for star fields, where
                         most pixels are noise floor but a few are bright.
                         Lets the tuner stop at "enough stars visible" rather
                         than try to lift the overall image.
    sharpness         -- Laplacian variance; the standard frame-quality
                         metric for lucky imaging. Sharper frames = higher
                         variance of the Laplacian.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

ImageInput = Union[Path, str, bytes, np.ndarray]


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_grayscale(src: ImageInput) -> np.ndarray:
    """Return an HxW uint8 grayscale ndarray from a path, bytes, or ndarray."""
    if isinstance(src, np.ndarray):
        arr = src
    elif isinstance(src, (str, Path)):
        with Image.open(src) as im:
            arr = np.asarray(im)
    elif isinstance(src, (bytes, bytearray, memoryview)):
        with Image.open(io.BytesIO(bytes(src))) as im:
            arr = np.asarray(im)
    else:
        raise TypeError(f"unsupported image input: {type(src)}")

    if arr.ndim == 3:
        # RGB(A) -> luminance via ITU-R BT.601 coefficients
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if arr.dtype != np.uint8:
        # Normalize floats / 16-bit -> uint8
        mn, mx = float(arr.min()), float(arr.max())
        if mx > mn:
            arr = ((arr - mn) / (mx - mn) * 255).astype(np.uint8)
        else:
            arr = np.zeros_like(arr, dtype=np.uint8)
    return arr


# ----------------------------------------------------------------------
# Luminance / histogram
# ----------------------------------------------------------------------

@dataclass
class LuminanceStats:
    """Whole-frame brightness summary. All values 0..255 (uint8 space)."""
    mean: float
    median: float
    p1: float
    p99: float
    """Fraction of pixels at the floor (value == 0)."""
    frac_black: float
    """Fraction of pixels at the ceiling (value == 255)."""
    frac_white: float

    def is_clipped_dark(self, threshold: float = 0.20) -> bool:
        """True if more than `threshold` of the frame sits at 0 (under-exposed)."""
        return self.frac_black > threshold

    def is_clipped_bright(self, threshold: float = 0.10) -> bool:
        """True if more than `threshold` of the frame sits at 255 (blown out)."""
        return self.frac_white > threshold


def luminance_stats(src: ImageInput) -> LuminanceStats:
    g = load_grayscale(src)
    flat = g.flatten()
    return LuminanceStats(
        mean=float(g.mean()),
        median=float(np.median(g)),
        p1=float(np.percentile(g, 1)),
        p99=float(np.percentile(g, 99)),
        frac_black=float((flat == 0).sum()) / flat.size,
        frac_white=float((flat == 255).sum()) / flat.size,
    )


# ----------------------------------------------------------------------
# Star detection
# ----------------------------------------------------------------------

@dataclass
class StarCount:
    """Result of a star-counting pass."""
    count: int
    """Threshold used to separate stars from background (uint8)."""
    threshold: int
    """Fraction of frame area considered bright (sanity check)."""
    bright_frac: float


def count_stars(
    src: ImageInput,
    *,
    sigma_above_background: float = 6.0,
    min_area_px: int = 2,
    max_area_px: int = 400,
) -> StarCount:
    """Count blob-like bright features.

    Uses the median + MAD-derived sigma to set an adaptive threshold, then
    connected-components on the thresholded mask. Filters blobs by area to
    drop hot pixels (too small) and overexposed light sources (too large).

    Tuned for a sky frame where most pixels are background noise and stars
    are localized bright peaks. Works fine on the iPhone's natively noisy
    afocal frames; the sigma threshold adapts to the noise floor.
    """
    g = load_grayscale(src)
    bg = float(np.median(g))
    mad = float(np.median(np.abs(g.astype(np.int16) - bg)))
    # MAD -> Gaussian sigma scaling
    sigma = max(1.0, mad * 1.4826)
    threshold = int(min(255, bg + sigma_above_background * sigma))

    mask = (g >= threshold).astype(np.uint8)
    bright_frac = float(mask.sum()) / mask.size

    # Connected components labels each blob; index 0 is background.
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    count = 0
    for i in range(1, n_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if min_area_px <= area <= max_area_px:
            count += 1

    return StarCount(count=count, threshold=threshold, bright_frac=bright_frac)


# ----------------------------------------------------------------------
# Sharpness (for lucky imaging)
# ----------------------------------------------------------------------

def sharpness(src: ImageInput) -> float:
    """Variance of the Laplacian. Higher = sharper.

    Standard metric used by RegiStax / AutoStakkert for frame quality
    ranking. Atmospheric seeing makes some frames in a planetary video
    sharper than others; we keep the top N percent by this score.
    """
    g = load_grayscale(src)
    lap = cv2.Laplacian(g, cv2.CV_64F)
    return float(lap.var())


# ----------------------------------------------------------------------
# Convenience: full report on one frame
# ----------------------------------------------------------------------

@dataclass
class FrameReport:
    luminance: LuminanceStats
    stars: StarCount
    sharpness: float
    width: int
    height: int


def analyze(src: ImageInput) -> FrameReport:
    """Run all metrics in one pass. Avoids re-loading the image."""
    g = load_grayscale(src)
    return FrameReport(
        luminance=luminance_stats(g),
        stars=count_stars(g),
        sharpness=sharpness(g),
        height=g.shape[0],
        width=g.shape[1],
    )
