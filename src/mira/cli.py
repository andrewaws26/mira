"""argparse-based CLI for offline use without an LLM.

This is the fallback when observing from a dark sky site with no signal.
Each subcommand maps to one or two tool functions in mira.tools.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import __version__
from .camera import CameraError, list_devices
from .config import ConfigError, load_config, setup_logging
from .ephemeris import NameNotFoundError
from .mount import MountError
from .solver import SolveFailed, SolverError
from .tools import (
    ToolContext,
    capture_frame,
    get_mount_position,
    get_target_coordinates,
    goto,
    plate_solve,
    sync_mount,
)

logger = logging.getLogger(__name__)


# Exit codes follow common UNIX-ish conventions.
EXIT_OK = 0
EXIT_FAILURE = 1   # operation failed (no solve, mount unreachable, etc.)
EXIT_USAGE = 2     # bad args or missing config


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser. Pulled out for testability."""
    parser = argparse.ArgumentParser(
        prog="mira",
        description="Conversational telescope control for the Celestron NexStar 130SLT.",
    )
    parser.add_argument(
        "--version", action="version", version=f"mira {__version__}"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.yaml (default: ~/mira/config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )

    sub = parser.add_subparsers(dest="cmd", required=False, metavar="COMMAND")

    p_goto = sub.add_parser(
        "goto",
        help="solve current pointing, sync, then slew to a named target",
        description=(
            "Capture a frame, plate-solve to learn the true current pointing, "
            "sync the mount, then slew to the named target. This is the "
            "headline operation. No prior star alignment is needed."
        ),
    )
    p_goto.add_argument("target", help="object name (e.g. Jupiter, M31, Vega)")

    p_sync = sub.add_parser(
        "sync",
        help="capture, solve, and sync the mount without slewing",
        description=(
            "Capture a frame and plate-solve, then tell the mount its "
            "current pointing matches the solved coordinates. Useful for "
            "calibrating without moving."
        ),
    )

    p_where = sub.add_parser(
        "where",
        help="print the mount's reported current pointing",
        description=(
            "Query the mount and print apparent RA/Dec in degrees. "
            "Reflects the mount's belief, not ground truth. Use the "
            "sync subcommand if it disagrees with the sky."
        ),
    )

    p_capture = sub.add_parser(
        "capture",
        help="capture a single frame from the iPhone and save to disk",
        description=(
            "Capture one frame via Continuity Camera. Writes to the "
            "configured capture directory and prints the path."
        ),
    )
    p_capture.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional output path. Default: timestamped name in capture_dir.",
    )

    p_solve = sub.add_parser(
        "solve",
        help="run ASTAP on a saved image",
        description=(
            "Plate-solve a saved JPEG, PNG, or FITS image. Optional RA/Dec "
            "hints accelerate the solve."
        ),
    )
    p_solve.add_argument("image", type=Path, help="path to image file")
    p_solve.add_argument("--ra", type=float, default=None, help="RA hint in degrees")
    p_solve.add_argument("--dec", type=float, default=None, help="Dec hint in degrees")
    p_solve.add_argument(
        "--fov",
        type=float,
        default=None,
        help="FOV hint in degrees (overrides solver.estimated_fov_deg in config)",
    )

    p_status = sub.add_parser(
        "status",
        help="print mount and sync state",
        description=(
            "Print mount connection status, current pointing, the most "
            "recent sync, the most recent slew, and the configured "
            "observer location."
        ),
    )

    p_devices = sub.add_parser(
        "devices",
        help="list cameras visible to imagesnap",
        description=(
            "Enumerate the cameras visible to imagesnap. Use to confirm "
            "Continuity Camera is connected."
        ),
    )

    p_resolve = sub.add_parser(
        "resolve",
        help="resolve a target name to apparent RA/Dec without moving the mount",
        description=(
            "Look up a target name in the catalog and print its apparent "
            "RA/Dec in degrees at the configured observer location. "
            "Useful for testing name resolution offline."
        ),
    )
    p_resolve.add_argument("target", help="object name")

    # Make sure type checkers know these locals stay used.
    _ = (p_goto, p_sync, p_where, p_capture, p_solve, p_status, p_devices, p_resolve)

    return parser


def _format_radec(ra_deg: float, dec_deg: float) -> str:
    return f"RA={ra_deg:.4f} deg, Dec={dec_deg:.4f} deg"


def _format_radec_sexagesimal(ra_deg: float, dec_deg: float) -> str:
    """Format RA in HH:MM:SS and Dec in +/-DD:MM:SS for human consumption."""
    ra_h = (ra_deg / 15.0) % 24.0
    rh = int(ra_h)
    rm_full = (ra_h - rh) * 60.0
    rm = int(rm_full)
    rs = (rm_full - rm) * 60.0
    sign = "+" if dec_deg >= 0 else "-"
    abs_dec = abs(dec_deg)
    dd = int(abs_dec)
    dm_full = (abs_dec - dd) * 60.0
    dm = int(dm_full)
    ds = (dm_full - dm) * 60.0
    return f"{rh:02d}h{rm:02d}m{rs:05.2f}s, {sign}{dd:02d}d{dm:02d}m{ds:04.1f}s"


def _build_context(args: argparse.Namespace) -> ToolContext:
    """Load config and construct a fully-populated ToolContext."""
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    return ToolContext.from_config(args.config)


def cmd_goto(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        ok = goto(args.target, ctx=ctx)
        if not ok:
            print(f"goto {args.target}: failed. Check ~/mira/mira.log for details.")
            return EXIT_FAILURE
        ra, dec = get_mount_position(ctx=ctx)
        print(f"goto {args.target}: arrived. {_format_radec(ra, dec)}")
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        ctx.connect_mount()
        try:
            hint = ctx.mount.get_position()
        except MountError:
            hint = (None, None)
        image = capture_frame(ctx=ctx)
        print(f"captured {image}")
        solved = plate_solve(image, ra_hint_deg=hint[0], dec_hint_deg=hint[1], ctx=ctx)
        if solved is None:
            print("solve failed. try a darker exposure or a different patch of sky.")
            return EXIT_FAILURE
        ra, dec = solved
        print(f"solved at {_format_radec(ra, dec)}")
        if not sync_mount(ra, dec, ctx=ctx):
            print("mount did not accept sync.")
            return EXIT_FAILURE
        print("sync OK")
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_where(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        ra, dec = get_mount_position(ctx=ctx)
        print(_format_radec(ra, dec))
        print(_format_radec_sexagesimal(ra, dec))
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_capture(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        path = capture_frame(ctx=ctx)
        if args.output is not None:
            target = Path(args.output).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            print(target)
        else:
            print(path)
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_solve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .solver import Solver

    solver = Solver(
        astap_path=cfg.solver.astap_path,
        estimated_fov_deg=cfg.solver.estimated_fov_deg,
        timeout_seconds=cfg.solver.timeout_seconds,
        star_db=cfg.solver.star_db,
    )
    result = solver.solve(args.image, ra_hint_deg=args.ra, dec_hint_deg=args.dec, fov_deg=args.fov)
    print(_format_radec(result.ra_deg, result.dec_deg))
    print(_format_radec_sexagesimal(result.ra_deg, result.dec_deg))
    if result.pixel_scale_arcsec is not None:
        print(f"pixel scale: {result.pixel_scale_arcsec:.3f} arcsec/pixel")
    if result.fov_x_deg and result.fov_y_deg:
        print(f"field of view: {result.fov_x_deg:.3f} x {result.fov_y_deg:.3f} deg")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        cfg = ctx.config
        print(f"mira {__version__}")
        print(f"observer: lat={cfg.observer.latitude:.4f}, lon={cfg.observer.longitude:.4f}")
        try:
            ctx.connect_mount(timeout=5.0)
            ra, dec = ctx.mount.get_position()
            print(f"mount: connected at {cfg.mount.indi_host}:{cfg.mount.indi_port}")
            print(f"  pointing: {_format_radec(ra, dec)}")
            print(f"  slewing:  {ctx.mount.is_slewing()}")
        except MountError as e:
            print(f"mount: NOT connected ({e})")

        latest_sync = ctx.state.latest_sync()
        if latest_sync:
            print(
                f"last sync: {latest_sync.ts}  "
                f"{_format_radec(latest_sync.ra_deg, latest_sync.dec_deg)}"
            )
        else:
            print("last sync: never")

        latest_slew = ctx.state.latest_slew()
        if latest_slew:
            print(
                f"last slew: {latest_slew.ts}  target={latest_slew.target_name!r} "
                f"{_format_radec(latest_slew.target_ra_deg, latest_slew.target_dec_deg)} "
                f"success={latest_slew.success}"
            )
        else:
            print("last slew: never")
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_devices(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    devices = list_devices()
    if not devices:
        print("no cameras found by imagesnap")
        return EXIT_FAILURE
    for d in devices:
        marker = "*" if cfg.camera.device_name.lower() in d.lower() else " "
        print(f"  [{marker}] {d}")
    return EXIT_OK


def cmd_resolve(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        ra, dec = get_target_coordinates(args.target, ctx=ctx)
        print(_format_radec(ra, dec))
        print(_format_radec_sexagesimal(ra, dec))
        return EXIT_OK
    finally:
        ctx.shutdown()


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "goto":     cmd_goto,
    "sync":     cmd_sync,
    "where":    cmd_where,
    "capture":  cmd_capture,
    "solve":    cmd_solve,
    "status":   cmd_status,
    "devices":  cmd_devices,
    "resolve":  cmd_resolve,
}


def _exit_with_clean_error(msg: str, code: int = EXIT_FAILURE) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return EXIT_USAGE
    handler = COMMANDS.get(args.cmd)
    if handler is None:
        print(f"unknown command: {args.cmd}", file=sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args)
    except ConfigError as e:
        return _exit_with_clean_error(str(e), code=EXIT_USAGE)
    except NameNotFoundError as e:
        return _exit_with_clean_error(str(e))
    except CameraError as e:
        return _exit_with_clean_error(str(e))
    except MountError as e:
        return _exit_with_clean_error(str(e))
    except SolveFailed as e:
        return _exit_with_clean_error(f"plate solve failed: {e}")
    except SolverError as e:
        return _exit_with_clean_error(str(e))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_FAILURE


def main(argv: Optional[Sequence[str]] = None) -> Any:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()
