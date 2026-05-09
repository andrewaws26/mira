#!/usr/bin/env python3
"""Smoke test: capture a single frame from the configured camera.

Run after installing imagesnap, pairing the iPhone, and ensuring
Continuity Camera is enabled. Reads camera config from ~/mira/config.yaml.

PASS criteria: image written to capture_dir, file size greater than zero.
"""

from __future__ import annotations

import sys
from pathlib import Path

# allow running as `python scripts/test_camera.py` from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.camera import Camera, CameraError, CameraNotFoundError, list_devices  # noqa: E402
from mira.config import ConfigError, load_config  # noqa: E402


def main() -> int:
    print("[*] loading config")
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2

    print(f"[*] enumerating cameras")
    try:
        devices = list_devices()
    except CameraError as e:
        print(f"[FAIL] {e}")
        return 2
    if not devices:
        print("[FAIL] imagesnap reported no devices")
        return 2
    for d in devices:
        marker = "*" if cfg.camera.device_name.lower() in d.lower() else " "
        print(f"  [{marker}] {d}")

    cam = Camera(
        device_name=cfg.camera.device_name,
        capture_dir=cfg.camera.capture_dir,
        warmup_seconds=cfg.camera.warmup_seconds,
    )
    print(f"[*] capturing single frame from {cfg.camera.device_name!r}")
    try:
        path = cam.capture()
    except CameraNotFoundError as e:
        print(f"[FAIL] {e}")
        return 1
    except CameraError as e:
        print(f"[FAIL] {e}")
        return 1

    size = path.stat().st_size
    if size == 0:
        print(f"[FAIL] captured file is empty: {path}")
        return 1
    print(f"[PASS] saved {path} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
