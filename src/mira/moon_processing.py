"""Single-frame processing for the Moon.

The Moon is bright enough that a single iPhone capture has plenty of
signal. What it lacks is contrast (a "raw" Moon photo looks like a flat
white disk on a black field). Two filters fix that:

  1. histogram stretch -- map the actual luminance range (which sits
     somewhere in the middle of 0..255) back out to the full 0..255,
     so dark crater shadows go to true black and bright peaks go to
     true white.
  2. unsharp mask -- subtract a blurred copy from the original to
     amplify high-frequency detail. This is the same "sharpen" filter
     planetary imagers have used for 30 years.

No stacking, no alignment. One frame in, one polished frame out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class MoonProcessingParams:
    """Tunable parameters. Defaults work for full + gibbous phases at
    iPhone-afocal-through-a-130mm scope. Crescent phases may want a
    lower stretch_low to preserve the earthshine."""

    # Percentile-based stretch endpoints. Pixels at <= stretch_low% go
    # to 0; pixels at >= stretch_high% go to 255; linear in between.
    stretch_low: float = 1.0
    stretch_high: float = 99.5

    # Unsharp-mask sigma (Gaussian blur radius). Larger sigma = more
    # large-scale detail enhancement.
    unsharp_sigma: float = 2.5
    # How much of the high-frequency signal to add back. 1.0 is a
    # mild boost; 1.5 is "internet astrophotography sharp".
    unsharp_amount: float = 1.2

    # Final gamma. <1.0 brightens midtones; >1.0 darkens. 0.9 lifts
    # crater rims without blowing out highlights.
    gamma: float = 0.9


def process_moon_frame(
    input_path: Path | str,
    output_path: Optional[Path] = None,
    *,
    params: Optional[MoonProcessingParams] = None,
) -> Path:
    """Apply stretch + unsharp + gamma to a Moon JPEG. Returns the output path.

    Operates on the LUMINANCE channel only -- preserves the bluish/yellow
    color cast of the iPhone capture, just lifts contrast.
    """
    p = params or MoonProcessingParams()
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.parent / f"{input_path.stem}-processed.jpg"

    # Load as BGR (cv2 default), split off luminance via YCrCb so we can
    # preserve color while operating on brightness.
    bgr = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(f"cannot read {input_path}")

    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[:, :, 0]

    # 1. Histogram stretch
    lo = np.percentile(y, p.stretch_low)
    hi = np.percentile(y, p.stretch_high)
    if hi > lo:
        stretched = np.clip((y.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255)
    else:
        stretched = y.astype(np.float32)
    logger.info("moon stretch: %.1f..%.1f -> 0..255", lo, hi)

    # 2. Unsharp mask
    blurred = cv2.GaussianBlur(stretched, ksize=(0, 0), sigmaX=p.unsharp_sigma)
    sharp = stretched + p.unsharp_amount * (stretched - blurred)
    sharp = np.clip(sharp, 0, 255)

    # 3. Gamma
    if abs(p.gamma - 1.0) > 0.01:
        sharp = (np.power(sharp / 255.0, p.gamma) * 255.0)
    sharp = np.clip(sharp, 0, 255).astype(np.uint8)

    # Recompose with original chroma
    ycrcb[:, :, 0] = sharp
    out_bgr = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)

    # Save via Pillow for consistent JPEG quality
    out_rgb = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)
    Image.fromarray(out_rgb).save(output_path, quality=92)
    logger.info("moon processed -> %s", output_path)
    return output_path
