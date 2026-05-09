"""Live preview window of the iPhone camera feed via ffplay.

Used during initial alignment to center the iPhone over the eyepiece.
The user iterates on NexYZ adjusters while watching the live feed,
which is many times faster than capture/inspect/adjust loops.

We shell out to ffplay (part of ffmpeg), which already understands
AVFoundation's macOS capture path. This avoids pulling in PyObjC or
opencv as dependencies of mira.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class PreviewError(RuntimeError):
    """Raised when the preview window cannot be launched."""


def _ffplay_binary() -> str:
    p = shutil.which("ffplay")
    if p is None:
        raise PreviewError(
            "ffplay not found on PATH. Install with: brew install ffmpeg"
        )
    return p


def _ffmpeg_binary() -> str:
    p = shutil.which("ffmpeg")
    if p is None:
        raise PreviewError(
            "ffmpeg not found on PATH. Install with: brew install ffmpeg"
        )
    return p


def list_avfoundation_devices() -> list[tuple[int, str]]:
    """Return list of (index, name) AVFoundation video devices.

    Parses ffmpeg's `-list_devices true` output. Audio devices are ignored.
    """
    binary = _ffmpeg_binary()
    proc = subprocess.run(
        [binary, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # ffmpeg prints the device list on stderr and exits non-zero. That is
    # expected; what matters is the parsed list.
    out = proc.stderr or proc.stdout
    devices: list[tuple[int, str]] = []
    in_video_section = False
    pattern = re.compile(r"\[(\d+)\]\s+(.+)$")
    for raw in out.splitlines():
        line = raw.strip()
        if "AVFoundation video devices" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices" in line:
            in_video_section = False
            continue
        if not in_video_section:
            continue
        # ffmpeg prefixes lines with [AVFoundation indev @ 0x...]
        idx = line.find("] [")
        if idx == -1:
            continue
        tail = line[idx + 2:]
        m = pattern.match(tail)
        if not m:
            continue
        devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def resolve_device_index(device_name: str) -> int:
    """Resolve a configured device name to an AVFoundation index.

    Case-insensitive substring match. Returns the lowest-index match.

    Raises:
        PreviewError if no device matches.
    """
    devices = list_avfoundation_devices()
    if not devices:
        raise PreviewError(
            "ffmpeg could not enumerate any AVFoundation video devices. "
            "Is the user logged into a graphical session?"
        )
    needle = device_name.lower()
    for idx, name in devices:
        if needle in name.lower():
            return idx
    visible = ", ".join(f"[{i}] {n}" for i, n in devices)
    raise PreviewError(
        f"camera {device_name!r} not visible to ffmpeg. Available: {visible}. "
        "Check Continuity Camera is enabled and the iPhone is unlocked and nearby."
    )


def launch_preview(
    device_name: str,
    framerate: int = 30,
    window_size: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> int:
    """Open a live preview window. Blocks until the user closes it.

    Args:
        device_name: configured camera name (e.g. "Andrew Camera").
        framerate: target frames per second from the camera.
        window_size: optional WIDTHxHEIGHT for the window (e.g. "1280x720").
        extra_args: pass-through additional ffplay args.

    Returns:
        ffplay's exit code.
    """
    binary = _ffplay_binary()
    idx = resolve_device_index(device_name)
    title = f"Mira preview - {device_name} (idx {idx}) - press q to quit"
    cmd: list[str] = [
        binary,
        "-hide_banner",
        "-loglevel", "warning",
        "-window_title", title,
        "-f", "avfoundation",
        "-framerate", str(framerate),
    ]
    if window_size:
        cmd += ["-video_size", window_size]
    cmd += ["-i", str(idx)]
    if extra_args:
        cmd += list(extra_args)
    logger.debug("preview cmd: %s", cmd)
    print(f"opening preview of {device_name!r} (AVFoundation idx {idx}). Press Q in the window to close.")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
