"""Shared state directory for live preview.

The smart-capture pipeline writes its current state here as it runs.
The preview server (mira watch) polls these files and renders them to
a web page. Two-process design: pipeline (Mira CLI) and preview (mira
watch) don't share memory, just files.

Layout:
    ~/mira/captures/current/
        state.json    -- machine-readable pipeline state
        frame.jpg     -- latest single captured frame (overwritten)
        stack.jpg     -- in-progress stacked image (overwritten as more
                         frames are added; absent until a stack starts)
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def state_dir() -> Path:
    p = Path("~/mira/captures/current").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class PipelineState:
    """Snapshot of what the capture pipeline is doing right now.

    Updated incrementally during long-running operations so the preview
    server can show progress.
    """

    target: str = ""
    category: str = ""
    pipeline: str = ""            # "moon" | "lucky" | "live" | "single"
    phase: str = "idle"           # "idle" | "tuning" | "capturing" | "stacking" | "done" | "error"
    message: str = ""             # human-readable status line

    # Current exposure (set during/after tune)
    iso: Optional[float] = None
    shutter_ms: Optional[float] = None
    bias: Optional[float] = None
    mean_lum: Optional[float] = None

    # Burst / stack progress
    frames_captured: int = 0
    frames_target: int = 0
    frames_stacked: int = 0

    # Final result location, if any
    output_path: Optional[str] = None

    updated_at: str = field(default_factory=lambda: _now())

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def write_state(state: PipelineState) -> None:
    """Atomically replace state.json with the given state.

    Atomic write so the preview server never reads a half-written file.
    """
    state.updated_at = _now()
    target = state_dir() / "state.json"
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".json", dir=str(target.parent))
    try:
        with open(fd, "w") as f:
            f.write(state.to_json())
        Path(tmp).replace(target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def patch_state(**fields) -> PipelineState:
    """Read current state, apply patch, write back. Returns the new state."""
    state = read_state() or PipelineState()
    for k, v in fields.items():
        setattr(state, k, v)
    write_state(state)
    return state


def read_state() -> Optional[PipelineState]:
    """Best-effort read; returns None if not present or unparseable."""
    p = state_dir() / "state.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        # Drop unknown keys defensively (forward/back compat)
        known = {f.name for f in PipelineState.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in known}
        return PipelineState(**data)
    except Exception as e:
        logger.debug("read_state: %s", e)
        return None


def publish_frame(src: Path | str) -> Path:
    """Copy a freshly captured frame to current/frame.jpg.

    The preview server polls this path; copying preserves the original
    capture in the user's normal output location.
    """
    dst = state_dir() / "frame.jpg"
    shutil.copy(str(src), str(dst))
    return dst


def publish_stack(src: Path | str) -> Path:
    """Copy an in-progress (or final) stacked image to current/stack.jpg."""
    dst = state_dir() / "stack.jpg"
    shutil.copy(str(src), str(dst))
    return dst


def reset() -> None:
    """Clear the state directory at the start of a new capture session.

    Removes stale frame.jpg / stack.jpg so the preview doesn't show
    confusing leftovers from the previous run.
    """
    d = state_dir()
    for name in ("state.json", "frame.jpg", "stack.jpg"):
        (d / name).unlink(missing_ok=True)
