"""Frame stacking for lucky imaging and live deep-sky stacking.

Two patterns supported:

  lucky_image(cam, n_frames, keep_pct):
      Burst-capture N frames as fast as the camera allows. Score each by
      Laplacian variance (sharpness). Keep the top keep_pct%. Align via
      cv2.findTransformECC (sub-pixel translation+rotation). Mean-stack
      to a final image. Standard RegiStax / AutoStakkert! technique.

      Best for: planets, Moon, bright stars, double stars.
      Why it works: atmospheric seeing varies frame-to-frame; the
      sharpest frames have the least distortion. Throwing away the
      blurry ones and averaging the sharp ones beats a single capture
      every time.

  live_stack(cam, n_frames, frame_pause_s):
      Capture N frames at deliberate intervals. Align via phase
      correlation (faster than ECC). Sum (not mean) to integrate flux,
      then normalize. Pulls faint targets out of noise.

      Best for: clusters, nebulae, galaxies on iPhone-class sensors.
      Why it works: signal grows linearly with N, noise grows as
      sqrt(N), so SNR improves by sqrt(N). 100 frames -> 10x SNR.

All operations are pure-ish: take a camera handle, return paths to the
output JPEGs. Intermediate frames land in /tmp/mira-stack-*/ for
debugging.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from .imaging import load_grayscale, sharpness
from .iphone_camera import IphoneCamera
from .pipeline_state import patch_state, publish_frame, publish_stack

logger = logging.getLogger(__name__)


@dataclass
class FrameMeta:
    """One captured frame plus its analysis."""

    index: int
    path: Path
    sharpness: float


@dataclass
class StackResult:
    """Result of a lucky-image or live-stack run."""

    output_path: Path
    n_captured: int
    n_kept: int
    n_aligned: int
    mode: str  # "lucky" | "live"
    elapsed_s: float
    frames: list[FrameMeta]
    """Best single frame -- a sanity reference. Equals output_path if N=1."""
    best_frame_path: Optional[Path] = None


# --------------------------------------------------------------------------
# Burst capture
# --------------------------------------------------------------------------

def capture_burst(
    cam: IphoneCamera,
    n_frames: int,
    *,
    output_dir: Optional[Path] = None,
    pause_s: float = 0.0,
) -> list[FrameMeta]:
    """Capture n_frames as fast as the camera allows.

    pause_s lets the caller throttle between captures -- useful for live
    stacking where each frame is a separate 1s exposure and the mount
    needs to settle.
    """
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="mira-stack-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[FrameMeta] = []
    for i in range(n_frames):
        path = output_dir / f"frame_{i:03d}.jpg"
        t0 = time.monotonic()
        cam.capture(path)
        t1 = time.monotonic()
        sharp = sharpness(path)
        frames.append(FrameMeta(index=i, path=path, sharpness=sharp))
        logger.info(
            "capture_burst %d/%d: %s (sharp=%.1f, %.2fs)",
            i + 1, n_frames, path.name, sharp, t1 - t0,
        )
        # Push to live-preview state so `mira watch` shows progress
        publish_frame(path)
        patch_state(
            phase="capturing",
            frames_captured=i + 1,
            frames_target=n_frames,
            message=f"capturing frame {i + 1}/{n_frames}",
        )
        if pause_s > 0 and i < n_frames - 1:
            time.sleep(pause_s)
    return frames


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------

def align_to_reference(
    reference: np.ndarray,
    target: np.ndarray,
    *,
    mode: str = "translation",
    max_iter: int = 100,
    termination_eps: float = 1e-4,
) -> Optional[np.ndarray]:
    """Warp `target` to match `reference` using ECC alignment.

    mode:
        "translation" -- 2 DoF (x, y). Fast, good for planets through a
                         tracking mount where the target stays centered.
        "euclidean"   -- 3 DoF (x, y, rotation). Use when the mount may
                         be alt-az and field rotation is small but visible.
        "affine"      -- 6 DoF. Heavier; rarely needed.

    Returns the warped image, or None if ECC failed to converge.
    """
    warp_mode = {
        "translation": cv2.MOTION_TRANSLATION,
        "euclidean": cv2.MOTION_EUCLIDEAN,
        "affine": cv2.MOTION_AFFINE,
    }[mode]

    warp_matrix = (
        np.eye(2, 3, dtype=np.float32)
        if warp_mode != cv2.MOTION_HOMOGRAPHY
        else np.eye(3, 3, dtype=np.float32)
    )
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        max_iter,
        termination_eps,
    )
    try:
        cc, warp_matrix = cv2.findTransformECC(
            reference, target, warp_matrix, warp_mode, criteria, None, 5,
        )
    except cv2.error as e:
        logger.debug("ECC failed: %s", e)
        return None

    h, w = reference.shape[:2]
    aligned = cv2.warpAffine(
        target, warp_matrix, (w, h),
        flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
    )
    return aligned


def align_phase_correlation(
    reference: np.ndarray, target: np.ndarray,
) -> np.ndarray:
    """Faster translation-only alignment via FFT phase correlation.

    Good enough for live-stack on bright extended targets where ECC
    convergence might be slow. Sub-pixel accurate.
    """
    ref_f = reference.astype(np.float32)
    tgt_f = target.astype(np.float32)
    shift, _ = cv2.phaseCorrelate(ref_f, tgt_f)
    dx, dy = shift
    M = np.array([[1, 0, -dx], [0, 1, -dy]], dtype=np.float32)
    h, w = reference.shape[:2]
    aligned = cv2.warpAffine(target, M, (w, h), flags=cv2.INTER_LINEAR)
    return aligned


# --------------------------------------------------------------------------
# Stacking
# --------------------------------------------------------------------------

def stack_mean(frames: list[np.ndarray]) -> np.ndarray:
    """Pixel-wise mean of a list of same-shape uint8 frames.

    Use for lucky-imaging where each frame is a complete capture of the
    target and we want SNR improvement without changing brightness.
    """
    if not frames:
        raise ValueError("no frames to stack")
    acc = np.zeros(frames[0].shape, dtype=np.float64)
    for f in frames:
        acc += f.astype(np.float64)
    acc /= len(frames)
    return np.clip(acc, 0, 255).astype(np.uint8)


def stack_sum_normalized(frames: list[np.ndarray]) -> np.ndarray:
    """Pixel-wise sum, then renormalize to 0..255.

    Use for live-stack: integrates faint flux from many short exposures.
    The renormalization preserves dynamic range; without it, bright stars
    saturate while faint structure stays buried.
    """
    if not frames:
        raise ValueError("no frames to stack")
    acc = np.zeros(frames[0].shape, dtype=np.float64)
    for f in frames:
        acc += f.astype(np.float64)
    mn, mx = float(acc.min()), float(acc.max())
    if mx > mn:
        acc = (acc - mn) / (mx - mn) * 255
    return np.clip(acc, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# Headline ops
# --------------------------------------------------------------------------

def lucky_image(
    cam: IphoneCamera,
    *,
    n_frames: int = 30,
    keep_pct: float = 0.30,
    align_mode: str = "translation",
    output_path: Optional[Path] = None,
) -> StackResult:
    """Burst capture -> sharpness rank -> align best -> mean stack.

    n_frames:  how many to capture. 30 is a good baseline for planets.
    keep_pct:  fraction to retain after sharpness ranking. 0.30 = top 30%.
    """
    t0 = time.monotonic()
    work_dir = Path(tempfile.mkdtemp(prefix="mira-lucky-"))
    logger.info("lucky_image: capturing %d frames -> %s", n_frames, work_dir)

    frames = capture_burst(cam, n_frames, output_dir=work_dir)

    # Rank and keep top keep_pct
    n_keep = max(1, int(n_frames * keep_pct))
    sorted_frames = sorted(frames, key=lambda f: f.sharpness, reverse=True)
    kept = sorted_frames[:n_keep]
    logger.info(
        "lucky_image: keeping %d/%d (sharpness range %.1f .. %.1f)",
        len(kept), n_frames, kept[-1].sharpness, kept[0].sharpness,
    )

    # Load + align against the sharpest frame
    reference = load_grayscale(kept[0].path)
    aligned: list[np.ndarray] = [reference]
    aligned_count = 1
    for fm in kept[1:]:
        tgt = load_grayscale(fm.path)
        warped = align_to_reference(reference, tgt, mode=align_mode)
        if warped is not None:
            aligned.append(warped)
            aligned_count += 1
        else:
            logger.debug("lucky_image: dropping frame %d (alignment failed)", fm.index)

    stacked = stack_mean(aligned)

    out = output_path or (work_dir / "stacked.jpg")
    Image.fromarray(stacked).save(out, quality=92)
    elapsed = time.monotonic() - t0
    logger.info(
        "lucky_image: stacked %d/%d aligned frames in %.1fs -> %s",
        aligned_count, len(kept), elapsed, out,
    )

    publish_stack(out)
    patch_state(
        phase="done",
        frames_stacked=aligned_count,
        output_path=str(out),
        message=f"lucky-stack of {aligned_count} frames in {elapsed:.1f}s",
    )

    return StackResult(
        output_path=out,
        n_captured=len(frames),
        n_kept=len(kept),
        n_aligned=aligned_count,
        mode="lucky",
        elapsed_s=elapsed,
        frames=frames,
        best_frame_path=kept[0].path,
    )


def live_stack(
    cam: IphoneCamera,
    *,
    n_frames: int = 30,
    pause_s: float = 0.5,
    align_mode: str = "translation",
    output_path: Optional[Path] = None,
) -> StackResult:
    """Capture N frames at intervals -> align all -> sum and normalize.

    Better than lucky-imaging for faint extended targets where each frame
    has so little signal that "best frames" all look like noise. We trade
    sharpness selectivity for SNR gain.

    pause_s sized to let the iPhone's per-frame buffer flush and the
    mount to settle between captures.
    """
    t0 = time.monotonic()
    work_dir = Path(tempfile.mkdtemp(prefix="mira-live-"))
    logger.info("live_stack: capturing %d frames at %.2fs intervals", n_frames, pause_s)

    frames = capture_burst(cam, n_frames, output_dir=work_dir, pause_s=pause_s)

    reference = load_grayscale(frames[0].path)
    aligned: list[np.ndarray] = [reference]
    aligned_count = 1
    # Publish the very first frame as the initial "stack" so the user
    # sees something before averaging starts to take effect.
    Image.fromarray(reference).save(work_dir / "_progress.jpg", quality=92)
    publish_stack(work_dir / "_progress.jpg")

    for idx, fm in enumerate(frames[1:], start=2):
        tgt = load_grayscale(fm.path)
        warped = align_phase_correlation(reference, tgt) if align_mode == "translation" \
            else align_to_reference(reference, tgt, mode=align_mode)
        if warped is not None:
            aligned.append(warped)
            aligned_count += 1
            # Publish the running stack so `mira watch` shows it improving
            partial = stack_sum_normalized(aligned)
            Image.fromarray(partial).save(work_dir / "_progress.jpg", quality=92)
            publish_stack(work_dir / "_progress.jpg")
            patch_state(
                phase="stacking",
                frames_stacked=aligned_count,
                message=f"live-stack {aligned_count}/{len(frames)} aligned",
            )

    stacked = stack_sum_normalized(aligned)

    out = output_path or (work_dir / "stacked.jpg")
    Image.fromarray(stacked).save(out, quality=92)
    elapsed = time.monotonic() - t0
    logger.info("live_stack: stacked %d frames in %.1fs -> %s", aligned_count, elapsed, out)

    publish_stack(out)
    patch_state(
        phase="done",
        frames_stacked=aligned_count,
        output_path=str(out),
        message=f"live-stack of {aligned_count} frames in {elapsed:.1f}s",
    )

    return StackResult(
        output_path=out,
        n_captured=len(frames),
        n_kept=len(frames),
        n_aligned=aligned_count,
        mode="live",
        elapsed_s=elapsed,
        frames=frames,
        best_frame_path=frames[0].path,
    )


def cleanup_workdir(result: StackResult) -> None:
    """Optionally wipe the intermediate frame directory. Leave the
    stacked output behind."""
    if result.output_path.parent.name.startswith("mira-"):
        out = result.output_path
        # Move the output up out of the temp dir before wiping
        permanent = out.parent.parent / out.name
        shutil.move(str(out), permanent)
        shutil.rmtree(out.parent, ignore_errors=True)
        result.output_path = permanent
