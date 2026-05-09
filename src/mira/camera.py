"""Image capture from the iPhone over Continuity Camera, via imagesnap.

We shell out to the imagesnap CLI (Homebrew package) rather than using PyObjC
with AVCaptureSession. This trades a small amount of latency per capture for
much simpler code and far fewer ways to break. AVFoundation through PyObjC is
the right upgrade path if frame rate ever matters.

The iPhone shows up as a standard AVFoundation device once it is unlocked,
near the Mac, signed into the same Apple ID, and Continuity Camera is enabled.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Raised on any image capture failure."""


class CameraNotFoundError(CameraError):
    """Raised when the configured camera device is not visible to imagesnap."""


def _imagesnap_path() -> str:
    p = shutil.which("imagesnap")
    if p is None:
        raise CameraError(
            "imagesnap not found on PATH. Install with: brew install imagesnap"
        )
    return p


def list_devices() -> list[str]:
    """Return device names visible to imagesnap.

    imagesnap -l prints "Video devices found:" then one device per line.
    """
    binary = _imagesnap_path()
    try:
        result = subprocess.run(
            [binary, "-l"], capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired as e:
        raise CameraError("imagesnap -l timed out") from e
    devices: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("video devices found"):
            continue
        # Lines often start with "=> " or similar prefix from newer imagesnap
        # versions; strip it.
        if line.startswith("=> "):
            line = line[3:].strip()
        devices.append(line)
    return devices


def device_visible(name: str) -> bool:
    """Case-insensitive substring match against known devices."""
    n = name.lower()
    return any(n in d.lower() for d in list_devices())


class Camera:
    """Wraps imagesnap to capture single frames from a named device."""

    def __init__(
        self,
        device_name: str = "iPhone",
        capture_dir: Path | str = Path("~/mira/captures"),
        warmup_seconds: float = 1.0,
    ) -> None:
        self.device_name = device_name
        self.capture_dir = Path(capture_dir).expanduser()
        self.warmup_seconds = warmup_seconds

    def ensure_dir(self) -> Path:
        """Make sure today's capture subdirectory exists. Returns the path."""
        today = datetime.now().strftime("%Y-%m-%d")
        d = self.capture_dir / today
        d.mkdir(parents=True, exist_ok=True)
        return d

    def capture(self, filename: str | None = None, warmup: float | None = None) -> Path:
        """Take a single still image. Returns the path to the saved file.

        Args:
            filename: optional filename. If None, uses a timestamp-based name.
            warmup: override the configured warmup_seconds for this capture.

        Raises:
            CameraNotFoundError: if the configured device is not visible.
            CameraError: if imagesnap fails or the file is not produced.
        """
        binary = _imagesnap_path()
        if not device_visible(self.device_name):
            visible = list_devices()
            raise CameraNotFoundError(
                f"camera {self.device_name!r} not visible to imagesnap. "
                f"Visible devices: {visible}. "
                "Check the iPhone is unlocked, near the Mac, and Continuity Camera is enabled."
            )
        out_dir = self.ensure_dir()
        if filename is None:
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            filename = f"capture_{ts}.jpg"
        out_path = out_dir / filename
        warm = self.warmup_seconds if warmup is None else warmup
        cmd = [binary, "-d", self.device_name, "-w", str(warm), str(out_path)]
        logger.debug("capture cmd: %s", cmd)
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=max(30.0, warm + 15.0)
            )
        except subprocess.TimeoutExpired as e:
            raise CameraError(
                f"imagesnap timed out after {e.timeout:.0f}s capturing {self.device_name!r}"
            ) from e
        if result.returncode != 0:
            raise CameraError(
                f"imagesnap exited with code {result.returncode}. "
                f"stderr: {result.stderr.strip()}"
            )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise CameraError(
                f"imagesnap reported success but no file at {out_path}. "
                f"stdout: {result.stdout.strip()}"
            )
        elapsed = time.monotonic() - t0
        logger.info("captured %s (%d bytes) in %.2fs", out_path, out_path.stat().st_size, elapsed)
        return out_path
