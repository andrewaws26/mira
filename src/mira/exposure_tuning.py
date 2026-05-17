"""Closed-loop exposure tuning.

Given a target type (planet, moon, cluster, etc.), pick a base ISO +
shutter from the preset table, then iterate:

    1. set the iPhone to current (iso, duration_ms)
    2. wait for AE to settle
    3. capture a frame
    4. analyze brightness / clipping / star count
    5. decide: too dark -> double exposure, too bright -> halve it, good -> stop
    6. loop, up to MAX_ITERATIONS

This is the foundational "smart" behavior the Vespera-style smart telescopes
do under the hood. Works the same way on iPhone hardware -- bounded by the
sensor's noise floor, but the FEEDBACK loop is identical.

Presets are tunable; the initial values reflect what works for the iPhone
16 main camera afocal through a 130mm reflector. Values will drift as the
optics or sky transparency change; the auto-tune compensates.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .imaging import analyze, FrameReport
from .iphone_camera import IphoneCamera, IphoneCameraError
from .pipeline_state import patch_state, publish_frame

logger = logging.getLogger(__name__)

# Target categories. Names match what get_target_type() returns for any
# Mira target name (see target_type.py, Phase 8).
TargetType = str  # "moon" | "planet" | "cluster" | "nebula" | "galaxy" | "star" | "default"


@dataclass
class ExposurePreset:
    """Starting exposure parameters for a category. Auto-tune refines."""

    iso: float
    duration_ms: float
    """Target mean luminance the tuner aims for (uint8 space, 0..255).
       A low value (e.g. 30) means "mostly dark, just a few bright peaks"
       which is what you want for a sky frame -- the stars should pop
       out against a near-black background."""
    target_mean_lum: float
    """Acceptable +/- on the mean before the tuner declares convergence."""
    mean_lum_tolerance: float = 8.0
    """Hard ceiling on shutter duration in ms (iPhone caps around 1000ms
       on the main 16-Plus camera; for a star field don't go past 500 even
       if the device would let you, to avoid alt-az tracking trails)."""
    max_duration_ms: float = 500.0
    """Hard ceiling on ISO (iPhone 16 Plus active format reports
       maxISO=1716; we cap a touch below to avoid the noisiest extreme)."""
    max_iso: float = 1600.0
    """Star count threshold the tuner uses to stop early on sky frames.
       If we see >= this many stars, we stop trying to lift the bias
       even if the mean luminance is still low.
       Only consulted when use_star_count_shortcut=True."""
    min_star_count: int = 30
    """Whether the star-count short-circuit applies. Targets that look like
       a star field (cluster, nebula, galaxy) want this; single-bright-object
       targets (moon, planet) do not."""
    use_star_count_shortcut: bool = False


# Tuned for iPhone 16 main camera + 130mm Newtonian afocal. These are
# starting points; auto-tune refines per scene. Use Phase 4 commits to
# update if a target type consistently lands wrong.
PRESETS: dict[TargetType, ExposurePreset] = {
    # Moon is bright. Short shutter, low ISO, target a mid-grey histogram.
    "moon": ExposurePreset(
        iso=50, duration_ms=2.0, target_mean_lum=130.0, mean_lum_tolerance=15.0,
        max_duration_ms=20.0, max_iso=200,
    ),
    # Planets are point-bright on a black background. Want them detectable
    # but not saturated; mean stays low.
    "planet": ExposurePreset(
        iso=100, duration_ms=8.0, target_mean_lum=12.0, mean_lum_tolerance=6.0,
        max_duration_ms=33.0, max_iso=400,
    ),
    # Bright cluster: many bright point sources on dark background.
    # Lift exposure until we see plenty of stars; mean stays low.
    "cluster": ExposurePreset(
        iso=800, duration_ms=200.0, target_mean_lum=15.0, mean_lum_tolerance=10.0,
        max_duration_ms=500.0, max_iso=1600, min_star_count=80,
        use_star_count_shortcut=True,
    ),
    # Bright nebula (M42 Orion, M8 Lagoon): extended faint source. iPhone
    # struggles past this; auto-tune may hit max-duration without
    # converging. That's diagnostic, not a bug.
    "nebula": ExposurePreset(
        iso=1600, duration_ms=500.0, target_mean_lum=20.0, mean_lum_tolerance=10.0,
        max_duration_ms=500.0, max_iso=1600,
        use_star_count_shortcut=True, min_star_count=20,
    ),
    # Galaxy: dimmer extended source. Mostly diagnostic for iPhone.
    "galaxy": ExposurePreset(
        iso=1600, duration_ms=500.0, target_mean_lum=10.0, mean_lum_tolerance=8.0,
        max_duration_ms=500.0, max_iso=1600,
        use_star_count_shortcut=True, min_star_count=5,
    ),
    # Bright star or double: just detect, don't try to lift background.
    "star": ExposurePreset(
        iso=100, duration_ms=10.0, target_mean_lum=8.0, mean_lum_tolerance=6.0,
        max_duration_ms=50.0, max_iso=400,
    ),
    # Catch-all: moderate settings, let the loop find a balance.
    "default": ExposurePreset(
        iso=400, duration_ms=50.0, target_mean_lum=30.0, mean_lum_tolerance=12.0,
        max_duration_ms=500.0, max_iso=1600,
    ),
}


@dataclass
class TuneIteration:
    """One step of the auto-tune loop, recorded for diagnostics."""

    iso: float
    duration_ms: float
    report: FrameReport
    action: str
    converged: bool


@dataclass
class TuneResult:
    """End-of-loop summary returned to the caller."""

    target_type: TargetType
    final_iso: float
    final_duration_ms: float
    iterations: list[TuneIteration] = field(default_factory=list)
    converged: bool = False
    reason: str = ""

    @property
    def steps(self) -> int:
        return len(self.iterations)


# Auto-tune control parameters
MAX_ITERATIONS = 6
SETTLE_TIME_S = 1.2  # let AVCaptureDevice apply new exposure before capturing
ADJUST_FACTOR_DARK = 2.0  # multiplicative boost when too dark
ADJUST_FACTOR_BRIGHT = 0.5  # multiplicative cut when too bright


def tune_for_target(
    cam: IphoneCamera,
    target_type: TargetType,
    *,
    overrides: Optional[ExposurePreset] = None,
    max_iterations: int = MAX_ITERATIONS,
    settle_time_s: float = SETTLE_TIME_S,
) -> TuneResult:
    """Iteratively converge on a good exposure for the target type.

    Returns when:
      - mean luminance is within tolerance of target, OR
      - star count threshold is met (sky frames only), OR
      - the device has been clamped to max_iso AND max_duration_ms
        (we can't lift any further), OR
      - max_iterations exhausted.

    Side effects:
      - leaves the iPhone in custom exposure mode at the final settings.
        Caller must reset_exposure() if continuing to auto-mode operation.

    Captures land in /tmp/mira-tune-{n}.jpg for inspection.
    """
    preset = overrides or PRESETS.get(target_type, PRESETS["default"])
    iso = preset.iso
    duration_ms = preset.duration_ms

    result = TuneResult(
        target_type=target_type,
        final_iso=iso,
        final_duration_ms=duration_ms,
    )

    for step in range(max_iterations):
        # Apply settings
        try:
            cam.set_manual_exposure(iso=iso, duration_ms=duration_ms)
        except IphoneCameraError as e:
            result.reason = f"set_manual_exposure failed: {e}"
            return result

        time.sleep(settle_time_s)

        # Capture + analyze
        from pathlib import Path
        capture_path = Path(f"/tmp/mira-tune-{step}.jpg")
        try:
            cam.capture(capture_path)
        except IphoneCameraError as e:
            result.reason = f"capture failed: {e}"
            return result

        report = analyze(capture_path)
        logger.info(
            "tune step %d: iso=%.0f dur=%.2fms -> mean=%.1f black=%.1f%% white=%.1f%% stars=%d",
            step, iso, duration_ms,
            report.luminance.mean, report.luminance.frac_black * 100,
            report.luminance.frac_white * 100, report.stars.count,
        )

        # Push to preview state so `mira watch` shows the tuning progress.
        publish_frame(capture_path)
        patch_state(
            phase="tuning",
            iso=iso,
            shutter_ms=duration_ms,
            mean_lum=report.luminance.mean,
            message=f"tune step {step + 1}/{max_iterations}: ISO {iso:.0f} @ {duration_ms:.1f}ms → mean {report.luminance.mean:.1f}",
        )

        # Decide next action
        action, converged, new_iso, new_duration = _decide_next_step(
            preset, iso, duration_ms, report,
        )
        iteration = TuneIteration(
            iso=iso, duration_ms=duration_ms,
            report=report, action=action, converged=converged,
        )
        result.iterations.append(iteration)

        if converged:
            result.converged = True
            result.reason = action
            result.final_iso = iso
            result.final_duration_ms = duration_ms
            return result

        iso = new_iso
        duration_ms = new_duration

    result.reason = f"max iterations ({max_iterations}) reached without convergence"
    result.final_iso = iso
    result.final_duration_ms = duration_ms
    return result


def _decide_next_step(
    preset: ExposurePreset,
    iso: float,
    duration_ms: float,
    report: FrameReport,
) -> tuple[str, bool, float, float]:
    """Return (action_description, converged, next_iso, next_duration_ms).

    Decision tree (in priority order):
      1. clipped white      -> halve exposure
      2. enough stars       -> converged
      3. mean within band   -> converged
      4. too dark           -> double exposure (within caps)
      5. too bright         -> halve exposure
      6. at caps but dark   -> converged (can't go higher; iPhone topped out)
    """
    lum = report.luminance
    target = preset.target_mean_lum
    tol = preset.mean_lum_tolerance

    # 1. Always pull back from white clipping first
    if lum.is_clipped_bright(threshold=0.05):
        return _scale_down("white-clipped, halving", iso, duration_ms)

    # 2. Star-count short-circuit (only for star-field-style targets)
    if preset.use_star_count_shortcut and report.stars.count >= preset.min_star_count:
        return (f"star count {report.stars.count} >= {preset.min_star_count}, converged",
                True, iso, duration_ms)

    # 3. Mean-luminance band
    if abs(lum.mean - target) <= tol:
        return (f"mean {lum.mean:.1f} within +/-{tol} of {target}, converged",
                True, iso, duration_ms)

    if lum.mean < target - tol:
        # Too dark; check headroom
        at_iso_cap = iso >= preset.max_iso
        at_dur_cap = duration_ms >= preset.max_duration_ms
        if at_iso_cap and at_dur_cap:
            return ("at iso + duration caps, can't go brighter, converged",
                    True, iso, duration_ms)
        return _scale_up("too dark, boosting", iso, duration_ms, preset)

    # 4. Too bright (but not clipping)
    return _scale_down("too bright, halving", iso, duration_ms)


def _scale_up(
    reason: str, iso: float, duration_ms: float, preset: ExposurePreset,
) -> tuple[str, bool, float, float]:
    """Multiplicatively raise exposure. Prefer extending duration over ISO
    (less noise), but cap at preset's max_duration_ms; then raise ISO."""
    factor = ADJUST_FACTOR_DARK
    new_dur = min(preset.max_duration_ms, duration_ms * factor)
    if new_dur > duration_ms:
        # Duration headroom available
        return (reason, False, iso, new_dur)
    # Duration capped; try ISO
    new_iso = min(preset.max_iso, iso * factor)
    return (reason, False, new_iso, duration_ms)


def _scale_down(
    reason: str, iso: float, duration_ms: float,
) -> tuple[str, bool, float, float]:
    """Cut exposure. Prefer reducing ISO first (cleaner), then shutter."""
    factor = ADJUST_FACTOR_BRIGHT
    new_iso = max(33, iso * factor)
    if new_iso < iso:
        return (reason, False, new_iso, duration_ms)
    # ISO at floor; cut duration
    new_dur = max(0.125, duration_ms * factor)
    return (reason, False, iso, new_dur)
