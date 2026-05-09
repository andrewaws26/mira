#!/usr/bin/env python3
"""Smoke test: slew the mount by ~1 degree, then slew back.

THIS MOVES THE TELESCOPE. Confirm interactively before proceeding.

PASS criteria: position changes, then returns within tolerance.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.config import ConfigError, load_config  # noqa: E402
from mira.mount import CelestronMount, MountError  # noqa: E402

SLEW_DELTA_DEG = 1.0
TOLERANCE_DEG = 0.5


def confirm() -> bool:
    print("THIS WILL MOVE THE TELESCOPE.")
    print(f"It will slew {SLEW_DELTA_DEG} degree east, then back.")
    print("Make sure the OTA is clear of obstructions.")
    reply = input("Type 'yes' to proceed: ").strip().lower()
    return reply == "yes"


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

    mount = CelestronMount(
        host=cfg.mount.indi_host,
        port=cfg.mount.indi_port,
    )
    try:
        mount.connect(timeout=10.0)
    except MountError as e:
        print(f"[FAIL] {e}")
        return 1

    try:
        start_ra, start_dec = mount.get_position()
        print(f"[*] start: RA={start_ra:.4f} Dec={start_dec:.4f}")

        target_ra = (start_ra + SLEW_DELTA_DEG) % 360.0
        target_dec = start_dec
        print(f"[*] slewing to: RA={target_ra:.4f} Dec={target_dec:.4f}")
        if not mount.slew_to(target_ra, target_dec, timeout=60.0):
            print("[FAIL] slew did not complete in time")
            return 1

        mid_ra, mid_dec = mount.get_position()
        print(f"[*] arrived: RA={mid_ra:.4f} Dec={mid_dec:.4f}")
        if abs(mid_ra - target_ra) > TOLERANCE_DEG:
            print(f"[FAIL] arrived position not within {TOLERANCE_DEG} deg of target")
            return 1

        print(f"[*] slewing back to: RA={start_ra:.4f} Dec={start_dec:.4f}")
        if not mount.slew_to(start_ra, start_dec, timeout=60.0):
            print("[FAIL] return slew did not complete")
            return 1
        end_ra, end_dec = mount.get_position()
        print(f"[*] back at: RA={end_ra:.4f} Dec={end_dec:.4f}")
        print("[PASS] slew round trip succeeded")
        return 0
    except MountError as e:
        print(f"[FAIL] {e}")
        return 1
    finally:
        mount.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
