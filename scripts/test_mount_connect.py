#!/usr/bin/env python3
"""Smoke test: connect to the INDI mount, query position, disconnect.

Does not move the mount. Run after starting indiserver:
    indiserver -v indi_celestron_gps

PASS criteria: connection opens, EQUATORIAL_EOD_COORD is readable, clean disconnect.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.config import ConfigError, load_config  # noqa: E402
from mira.mount import CelestronMount, MountError  # noqa: E402


def main() -> int:
    print("[*] loading config")
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2

    print(
        f"[*] connecting to indiserver at {cfg.mount.indi_host}:{cfg.mount.indi_port} "
        f"(device={cfg.mount.driver!r})"
    )
    mount = CelestronMount(
        host=cfg.mount.indi_host,
        port=cfg.mount.indi_port,
        device="Celestron GPS",
    )
    try:
        mount.connect(timeout=10.0)
    except MountError as e:
        print(f"[FAIL] {e}")
        return 1

    try:
        ra, dec = mount.get_position()
        print(f"[PASS] connected. position: RA={ra:.4f} deg, Dec={dec:.4f} deg")
    except MountError as e:
        print(f"[FAIL] could not read position: {e}")
        return 1
    finally:
        print("[*] disconnecting")
        mount.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
