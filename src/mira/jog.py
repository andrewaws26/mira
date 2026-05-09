"""Curses-based keyboard control of the mount.

Use case: at the eyepiece, you want to nudge the scope to center an object
without typing coordinates or talking to Claude. Tap arrow keys, watch the
scope move, lock it in.

Controls (case-insensitive):
    arrow keys      slew by `step` degrees in that direction
    +  -            increase / decrease step size
    1 .. 9          set INDI slew rate (1=slowest, 9=fastest if available)
    space  esc      abort any in-progress slew
    s               sync mount to its current pointing (no-op stub for now)
    p               toggle position polling display
    q               quit

The TUI shows current pointing, step size, slew rate, and recent activity.
"""

from __future__ import annotations

import curses
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .mount import (
    CelestronMount,
    MountError,
    MountTimeoutError,
    STATE_BUSY,
)

logger = logging.getLogger(__name__)


# Step-size ladder. Press + to climb, - to descend.
STEP_LADDER = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
DEFAULT_STEP_INDEX = 3  # 0.5 degrees


@dataclass
class JogState:
    step_deg: float = 0.5
    step_index: int = DEFAULT_STEP_INDEX
    slew_rate_index: int = 5  # mid-range default
    last_message: str = ""
    last_message_at: float = 0.0
    slew_in_progress: bool = False


def _set_slew_rate(mount: CelestronMount, index_one_based: int) -> Optional[str]:
    """Try to set TELESCOPE_SLEW_RATE.SLEW_<n>. Different drivers expose
    different rate counts; we just attempt and let the driver complain.
    Returns the chosen rate label on success, None on failure."""
    rate_name = f"SLEW_{index_one_based}"
    try:
        prop = mount.client.get_property("TELESCOPE_SLEW_RATE")
        if prop is None:
            return None
        # Build a mutual-exclusion switch dict from existing keys.
        switches = {k: (k == rate_name) for k in prop.elements.keys()}
        if rate_name not in switches:
            return None
        mount.client.set_switch("TELESCOPE_SLEW_RATE", switches)
        return rate_name
    except MountError:
        return None


def _abort(mount: CelestronMount) -> None:
    try:
        mount.abort()
    except MountError:
        pass


def _clamp_dec(dec_deg: float) -> float:
    return max(-89.5, min(89.5, dec_deg))


def _safe_get_position(mount: CelestronMount) -> Optional[tuple[float, float]]:
    try:
        return mount.get_position(timeout=2.0)
    except (MountError, MountTimeoutError):
        return None


def _is_slewing(mount: CelestronMount) -> bool:
    prop = mount.client.get_property(mount.PROP_COORD)
    return prop is not None and prop.state == STATE_BUSY


def _format_radec(ra_deg: float, dec_deg: float) -> str:
    return f"RA {ra_deg:8.4f}deg  Dec {dec_deg:+8.4f}deg"


def run_jog(cfg: Config) -> int:
    """Entry point. Connects to the mount, runs the curses loop, disconnects."""
    mount = CelestronMount(
        host=cfg.mount.indi_host,
        port=cfg.mount.indi_port,
        serial_port=cfg.mount.port or None,
    )
    print("connecting to mount...")
    try:
        mount.connect(timeout=15.0)
    except MountError as e:
        print(f"error: {e}")
        return 1
    try:
        return curses.wrapper(_jog_loop, cfg, mount)
    finally:
        try:
            mount.disconnect()
        except MountError:
            pass


def _jog_loop(stdscr, _cfg: Config, mount: CelestronMount) -> int:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    state = JogState(
        step_deg=STEP_LADDER[DEFAULT_STEP_INDEX],
        step_index=DEFAULT_STEP_INDEX,
    )

    def post(msg: str) -> None:
        state.last_message = msg
        state.last_message_at = time.monotonic()

    def render() -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = "Mira Jog  ::  arrows nudge  +/- step  1-9 rate  space stop  s sync  q quit"
        stdscr.addnstr(0, 0, title.ljust(w - 1), w - 1, curses.A_REVERSE)

        pos = _safe_get_position(mount)
        pos_line = _format_radec(*pos) if pos else "RA   ----     Dec   ----"
        stdscr.addnstr(2, 2, f"position:    {pos_line}", w - 3)

        slewing = _is_slewing(mount)
        status = "SLEWING" if slewing else "tracking"
        attr = curses.A_BOLD if slewing else curses.A_DIM
        stdscr.addnstr(3, 2, f"state:       {status}", w - 3, attr)

        stdscr.addnstr(5, 2, f"step:        {state.step_deg:5.2f} deg", w - 3)
        stdscr.addnstr(6, 2, f"slew rate:   {state.slew_rate_index} (1=slow, 9=fast)", w - 3)

        stdscr.addnstr(8, 2, "controls:", w - 3, curses.A_UNDERLINE)
        controls = [
            "  up arrow     slew +step in Dec (north)",
            "  down arrow   slew -step in Dec (south)",
            "  right arrow  slew +step in RA  (east)",
            "  left arrow   slew -step in RA  (west)",
            "  + / =        bigger step",
            "  - / _        smaller step",
            "  1 - 9        slew rate",
            "  space / esc  abort current slew",
            "  s            sync mount to current pointing (advisory)",
            "  q            quit",
        ]
        for i, line in enumerate(controls):
            stdscr.addnstr(9 + i, 2, line, w - 3)

        # Recent activity line
        msg_age = time.monotonic() - state.last_message_at
        if state.last_message and msg_age < 6.0:
            stdscr.addnstr(h - 2, 2, f"> {state.last_message}", w - 3, curses.A_BOLD)

        stdscr.refresh()

    def jog(d_ra_deg: float, d_dec_deg: float, label: str) -> None:
        pos = _safe_get_position(mount)
        if pos is None:
            post("no position")
            return
        ra0, dec0 = pos
        target_ra = (ra0 + d_ra_deg) % 360.0
        target_dec = _clamp_dec(dec0 + d_dec_deg)
        post(f"slewing {label} ({state.step_deg:+.2f} deg)")
        render()
        try:
            ok = mount.slew_to(target_ra, target_dec, timeout=30.0)
            new = _safe_get_position(mount)
            if not ok:
                post(f"REFUSED {label}: mount did not move (firmware horizon limit?)")
            elif new is not None:
                post(f"arrived: {_format_radec(*new)}")
            else:
                post("slew issued")
        except MountError as e:
            post(f"slew error: {e}")

    last_render = 0.0
    while True:
        now = time.monotonic()
        if now - last_render > 0.2:
            render()
            last_render = now

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return 0
        if key == -1:
            time.sleep(0.05)
            continue

        if key in (ord("q"), ord("Q")):
            return 0
        elif key == curses.KEY_UP:
            jog(0.0, +state.step_deg, "north")
        elif key == curses.KEY_DOWN:
            jog(0.0, -state.step_deg, "south")
        elif key == curses.KEY_RIGHT:
            jog(+state.step_deg, 0.0, "east")
        elif key == curses.KEY_LEFT:
            jog(-state.step_deg, 0.0, "west")
        elif key in (ord("+"), ord("=")):
            state.step_index = min(len(STEP_LADDER) - 1, state.step_index + 1)
            state.step_deg = STEP_LADDER[state.step_index]
            post(f"step -> {state.step_deg:.2f} deg")
        elif key in (ord("-"), ord("_")):
            state.step_index = max(0, state.step_index - 1)
            state.step_deg = STEP_LADDER[state.step_index]
            post(f"step -> {state.step_deg:.2f} deg")
        elif ord("1") <= key <= ord("9"):
            n = key - ord("0")
            label = _set_slew_rate(mount, n)
            if label is None:
                post(f"slew rate {n}: not supported by driver")
            else:
                state.slew_rate_index = n
                post(f"slew rate -> {label}")
        elif key in (27, ord(" ")):  # ESC or space
            _abort(mount)
            post("ABORT")
        elif key in (ord("s"), ord("S")):
            post("sync requires a fresh plate solve; use `mira sync` from a shell")
