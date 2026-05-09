#!/usr/bin/env python3
"""Smoke test: full pipeline. Capture, solve, sync, slew 1 deg, recapture,
resolve, verify the new pointing is within tolerance of the expected location.

THIS MOVES THE TELESCOPE. Confirm interactively before proceeding.

PASS criteria:
  - first capture saved
  - first solve produces valid RA/Dec
  - mount accepts sync
  - slew completes
  - second capture saved
  - second solve places center within `tolerance_deg` of expected pointing
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.camera import Camera, CameraError  # noqa: E402
from mira.config import ConfigError, load_config  # noqa: E402
from mira.mount import CelestronMount, MountError  # noqa: E402
from mira.solver import SolveFailed, Solver, SolverError  # noqa: E402

SLEW_DELTA_DEG = 1.0
TOLERANCE_DEG = 0.5  # post-slew solve must land this close to expected center


def confirm() -> bool:
    print("This test moves the telescope by 1 degree, then back.")
    print("Make sure the OTA is clear of obstructions and pointed at sky")
    print("with enough stars to plate-solve (twilight is usually fine).")
    reply = input("Type 'yes' to proceed: ").strip().lower()
    return reply == "yes"


def angular_distance_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Approximate small-angle separation in degrees on the sphere."""
    import math

    a1 = math.radians(ra1)
    a2 = math.radians(ra2)
    d1 = math.radians(dec1)
    d2 = math.radians(dec2)
    cos_sep = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    cos_sep = max(-1.0, min(1.0, cos_sep))
    return math.degrees(math.acos(cos_sep))


def main() -> int:
    if not confirm():
        print("[SKIP] user did not confirm")
        return 0

    print("[*] loading config")
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2

    cam = Camera(
        device_name=cfg.camera.device_name,
        capture_dir=cfg.camera.capture_dir,
        warmup_seconds=cfg.camera.warmup_seconds,
    )
    try:
        solver = Solver(
            astap_path=cfg.solver.astap_path,
            estimated_fov_deg=cfg.solver.estimated_fov_deg,
            timeout_seconds=cfg.solver.timeout_seconds,
        )
    except SolverError as e:
        print(f"[FAIL] {e}")
        return 1

    mount = CelestronMount(host=cfg.mount.indi_host, port=cfg.mount.indi_port)
    try:
        mount.connect(timeout=10.0)
    except MountError as e:
        print(f"[FAIL] {e}")
        return 1

    try:
        # 1. Capture first frame.
        print("[*] capturing first frame")
        try:
            img1 = cam.capture()
        except CameraError as e:
            print(f"[FAIL] capture1: {e}")
            return 1
        print(f"    saved {img1}")

        # 2. Solve.
        print("[*] solving first frame")
        cur_ra, cur_dec = mount.get_position()
        try:
            sol1 = solver.solve(img1, ra_hint_deg=cur_ra, dec_hint_deg=cur_dec)
        except SolveFailed as e:
            print(f"[FAIL] solve1: {e}")
            return 1
        print(f"    solved RA={sol1.ra_deg:.4f} Dec={sol1.dec_deg:.4f}")

        # 3. Sync.
        print("[*] syncing mount to solved position")
        if not mount.sync(sol1.ra_deg, sol1.dec_deg):
            print("[FAIL] mount did not accept sync")
            return 1
        time.sleep(0.5)

        # 4. Slew 1 degree east.
        target_ra = (sol1.ra_deg + SLEW_DELTA_DEG) % 360.0
        target_dec = sol1.dec_deg
        print(f"[*] slewing to RA={target_ra:.4f} Dec={target_dec:.4f}")
        if not mount.slew_to(target_ra, target_dec, timeout=60.0):
            print("[FAIL] slew did not complete")
            return 1
        time.sleep(1.0)

        # 5. Capture second frame.
        print("[*] capturing second frame at new position")
        try:
            img2 = cam.capture()
        except CameraError as e:
            print(f"[FAIL] capture2: {e}")
            return 1

        # 6. Solve second frame.
        print("[*] solving second frame")
        try:
            sol2 = solver.solve(img2, ra_hint_deg=target_ra, dec_hint_deg=target_dec)
        except SolveFailed as e:
            print(f"[FAIL] solve2: {e}")
            return 1
        print(f"    solved RA={sol2.ra_deg:.4f} Dec={sol2.dec_deg:.4f}")

        # 7. Verify.
        sep = angular_distance_deg(sol2.ra_deg, sol2.dec_deg, target_ra, target_dec)
        print(f"[*] post-slew separation from expected: {sep:.4f} deg")
        if sep > TOLERANCE_DEG:
            print(f"[FAIL] separation > {TOLERANCE_DEG} deg tolerance")
            return 1

        # 8. Slew back so the user is where they started.
        print("[*] slewing back to start")
        mount.slew_to(sol1.ra_deg, sol1.dec_deg, timeout=60.0)

        print("[PASS] full loop succeeded")
        return 0
    finally:
        mount.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
