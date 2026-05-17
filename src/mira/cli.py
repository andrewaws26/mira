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
from .speech import SpeechError
from .narration import CompositionError
from .sfx import SfxError
from .tools import (
    ToolContext,
    capture_frame,
    compose_narration,
    generate_sfx,
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
        help="solve current pointing, sync, slew, and smart-capture",
        description=(
            "Capture a frame, plate-solve to learn the true current pointing, "
            "sync the mount, slew to the named target, and (when the iPhone "
            "bridge is the camera backend) run the target-aware smart capture "
            "pipeline: target-tuned ISO + shutter, then lucky-imaging burst "
            "for planets, live-stack for deep-sky, stretch+sharpen for the "
            "Moon, or a single tuned frame for stars."
        ),
    )
    p_goto.add_argument("target", help="object name (e.g. Jupiter, M31, Vega)")
    p_goto.add_argument(
        "--no-capture",
        action="store_true",
        help="skip the smart-capture step; just slew",
    )
    p_goto.add_argument(
        "--out",
        type=Path,
        default=None,
        help="path to write the final image to",
    )

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
        help="capture one frame, or run the smart-capture pipeline",
        description=(
            "Without --target: capture one frame from the configured camera "
            "backend (imagesnap or iPhone bridge) and save it to disk. "
            "With --target X: run the target-aware smart-capture pipeline "
            "(auto-tune ISO + shutter, lucky-image / live-stack / moon "
            "process / single capture based on target type). No mount slew."
        ),
    )
    p_capture.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional output path. Default: timestamped name in capture_dir.",
    )
    p_capture.add_argument(
        "--target",
        type=str,
        default=None,
        help=(
            "if set, run smart-capture for this target type (e.g. 'Moon', "
            "'Jupiter', 'M42'). Auto-tunes exposure and picks the right "
            "capture pipeline."
        ),
    )
    p_capture.add_argument(
        "--pipeline",
        type=str,
        choices=["lucky", "live", "moon", "single"],
        default=None,
        help="force a specific pipeline regardless of target type",
    )
    p_capture.add_argument(
        "--n-frames",
        type=int,
        default=None,
        help="frame count for lucky/live pipelines (default 30)",
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

    p_say = sub.add_parser(
        "say",
        help="speak text via ElevenLabs TTS",
        description=(
            "Synthesize and play `text` through the configured ElevenLabs "
            "voice. Useful for testing the voice and the API key. "
            "ELEVENLABS_API_KEY must be set, either in the environment or "
            "in ~/mira/.env."
        ),
    )
    p_say.add_argument("text", nargs="+", help="text to speak (joined with spaces)")
    p_say.add_argument(
        "--voice",
        type=str,
        default=None,
        help="override the voice ID from config",
    )
    p_say.add_argument(
        "--blocking",
        action="store_true",
        help="wait for playback to finish before returning",
    )

    p_orient = sub.add_parser(
        "orient",
        help="coarse home: drive the mount north toward Polaris (~12s)",
        description=(
            "Drive the mount northward via TELESCOPE_MOTION_NS for a "
            "fixed duration (default 12 seconds). Brings the scope to a "
            "known-up reference position so you can center Polaris in "
            "the eyepiece, then sync. Useful as a 'restart' when fake "
            "alignment has put the mount in a no-go corner where "
            "coordinate slews keep getting refused by the firmware."
        ),
    )
    p_orient.add_argument(
        "--seconds", type=float, default=12.0,
        help="how long to drive north (default 12s)",
    )

    p_up = sub.add_parser(
        "up",
        help="start indiserver and connect; the one-button-up command",
        description=(
            "Bring the whole stack online. Starts indiserver in the "
            "background if it is not already running, waits for it to "
            "listen on port 7624, then tries to connect to the mount. "
            "If the connect fails, prints actionable next steps "
            "(power on, get past the alignment menus). Idempotent: "
            "running it twice is fine."
        ),
    )
    p_up.add_argument(
        "--no-voice",
        action="store_true",
        help="suppress the spoken 'mira ready' confirmation",
    )

    p_down = sub.add_parser(
        "down",
        help="stop indiserver and free the serial port",
        description=(
            "Disconnect from the mount, kill the indiserver process "
            "Mira started with `mira up`, and clean up state. "
            "Idempotent."
        ),
    )
    _ = p_down

    p_gps = sub.add_parser(
        "gps-push",
        help="push observer location and current UTC to the mount",
        description=(
            "Send the configured observer latitude / longitude / elevation "
            "and the current UTC time to the mount's GEOGRAPHIC_COORD and "
            "TIME_UTC properties. Mira's plate-solve workflow does not "
            "require this, but the hand controller's standalone GoTo does. "
            "ToolContext also pushes these automatically on every connect."
        ),
    )
    _ = p_gps

    p_jog = sub.add_parser(
        "jog",
        help="keyboard control of the mount (curses TUI)",
        description=(
            "Open a curses TUI to nudge the mount with the keyboard. "
            "Arrow keys slew by the current step size, +/- adjust step, "
            "1-9 set slew rate, space aborts, s syncs, q quits. Requires "
            "indiserver running and the mount connected."
        ),
    )
    _ = p_jog

    p_voices = sub.add_parser(
        "voices",
        help="list ElevenLabs voices on this account",
        description=(
            "Print every voice available to the configured ElevenLabs API "
            "key, with descriptive labels. Use the resulting voice_id in "
            "speech.voice_id in config.yaml."
        ),
    )

    p_compose = sub.add_parser(
        "compose",
        help="create a narrated audio piece (voice over a music bed) and save it",
        description=(
            "Synthesize narration via ElevenLabs TTS, generate a matched-length "
            "music bed via the ElevenLabs Music API, mix them into one mp3, and "
            "save it under ~/mira/captures/narrations/. Nothing is played. "
            "Requires ffmpeg and ELEVENLABS_API_KEY. Audio tags inside the "
            "story text ([warmly], [softly], [whispers], [excited], "
            "[confidently]) color delivery."
        ),
    )
    p_compose.add_argument(
        "story_file",
        type=Path,
        help="path to a UTF-8 text file containing the narration",
    )
    p_compose.add_argument(
        "--music",
        type=str,
        required=True,
        help="prompt describing the music bed (instruments, tempo, mood)",
    )
    p_compose.add_argument(
        "--voice",
        type=str,
        default=None,
        help=(
            "ElevenLabs voice id. Defaults to George (warm storyteller). "
            "Must be a voice that supports eleven_v3."
        ),
    )
    p_compose.add_argument(
        "--music-volume",
        type=float,
        default=0.35,
        help="music bed volume relative to narration (0.0 to 1.0, default 0.35)",
    )
    p_compose.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output path. Defaults to a timestamped name in ~/mira/captures/narrations/.",
    )
    p_compose.add_argument(
        "--stability", type=float, default=None,
        help="override voice stability (0..1, lower = more emotional swing)",
    )
    p_compose.add_argument(
        "--style", type=float, default=None,
        help="override voice style (0..1, higher = more emphasis)",
    )
    p_compose.add_argument(
        "--intro-sfx", type=str, default=None,
        help="optional SFX prompt for the cinematic open (crossfades into music)",
    )
    p_compose.add_argument(
        "--outro-sfx", type=str, default=None,
        help="optional SFX prompt for the cinematic close (crossfades from music tail)",
    )
    p_compose.add_argument(
        "--intro-sfx-duration", type=float, default=6.0,
        help="intro SFX length in seconds (0.5..22). Default 6.0.",
    )
    p_compose.add_argument(
        "--outro-sfx-duration", type=float, default=8.0,
        help="outro SFX length in seconds (0.5..22). Default 8.0.",
    )

    p_sfx = sub.add_parser(
        "sfx",
        help="generate a sound effect from a text prompt and save as mp3",
        description=(
            "Synthesize a one-shot sound effect via the ElevenLabs Sound "
            "Effects API and save it under ~/mira/captures/sfx/. Useful "
            "for stingers, atmospheric beds, conch calls, owl hoots, "
            "thunder, rope creaks. Nothing is played. Requires "
            "ELEVENLABS_API_KEY."
        ),
    )
    p_sfx.add_argument(
        "prompt",
        nargs="+",
        help="text describing the sound (joined with spaces)",
    )
    p_sfx.add_argument(
        "--duration",
        type=float,
        default=None,
        help="length in seconds, in [0.5, 22]. Default: model decides.",
    )
    p_sfx.add_argument(
        "--influence",
        type=float,
        default=0.3,
        help="prompt_influence in [0, 1]. Higher follows the prompt more strictly.",
    )
    p_sfx.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output path. Defaults to a timestamped name in ~/mira/captures/sfx/.",
    )

    p_preview = sub.add_parser(
        "preview",
        help="open a live preview window of the iPhone feed",
        description=(
            "Launch ffplay against the configured Continuity Camera. "
            "Use during initial alignment to center the iPhone over the "
            "eyepiece by adjusting the NexYZ while watching the live feed. "
            "Press Q in the preview window to close. Requires ffmpeg "
            "(brew install ffmpeg)."
        ),
    )
    p_preview.add_argument(
        "--device",
        type=str,
        default=None,
        help="override camera.device_name from config",
    )
    p_preview.add_argument(
        "--framerate",
        type=int,
        default=30,
        help="target frames per second (default: 30)",
    )
    p_preview.add_argument(
        "--size",
        type=str,
        default=None,
        help="window size as WIDTHxHEIGHT (e.g. 1280x720)",
    )

    p_fly = sub.add_parser(
        "fly",
        help="preview + global jog keys (arrows control mount while ffplay focused)",
        description=(
            "Opens the ffplay preview AND a global keyboard listener so "
            "arrow keys / 1-9 / q control the mount regardless of focus. "
            "Requires macOS Accessibility permission (granted via System "
            "Settings -> Privacy & Security -> Accessibility). Footgun: "
            "the listener is GLOBAL while running, so arrows pressed in "
            "any other app will also move the mount. Press q anywhere "
            "to quit cleanly."
        ),
    )

    # Make sure type checkers know these locals stay used.
    _ = (p_goto, p_sync, p_where, p_capture, p_solve, p_status, p_devices, p_resolve, p_fly)

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
        ok = goto(
            args.target,
            auto_capture=not args.no_capture,
            capture_out=args.out,
            ctx=ctx,
        )
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
        # Smart-capture mode: --target invokes the auto-tune + pipeline
        # routing. Useful for indoor testing (no mount slew) and for
        # re-capturing the current pointing with a target-tuned exposure.
        if args.target:
            from .tools import smart_capture
            final = smart_capture(
                args.target,
                ctx=ctx,
                pipeline=args.pipeline,
                n_frames=args.n_frames,
                out_path=args.output,
            )
            if final is None:
                print(f"smart_capture {args.target}: failed.")
                return EXIT_FAILURE
            print(final)
            return EXIT_OK

        # Legacy single-frame capture
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


def cmd_say(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .speech import SpeechDisabled, SpeechError, Speaker

    text = " ".join(args.text)
    voice = args.voice or cfg.speech.voice_id
    speaker = Speaker(
        voice_id=voice,
        model_id=cfg.speech.model_id,
        voice_settings={
            "stability": cfg.speech.stability,
            "similarity_boost": cfg.speech.similarity_boost,
            "style": cfg.speech.style,
            "use_speaker_boost": cfg.speech.use_speaker_boost,
        },
    )
    try:
        speaker.speak(text, blocking=args.blocking or cfg.speech.blocking)
    except SpeechDisabled as e:
        return _exit_with_clean_error(str(e))
    except SpeechError as e:
        return _exit_with_clean_error(str(e))
    return EXIT_OK


def cmd_voices(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .speech import SpeechDisabled, SpeechError, list_voices

    try:
        voices = list_voices()
    except SpeechDisabled as e:
        return _exit_with_clean_error(str(e))
    except SpeechError as e:
        return _exit_with_clean_error(str(e))
    cur = cfg.speech.voice_id
    for v in voices:
        marker = "*" if v.voice_id == cur else " "
        print(f"  [{marker}] {v.voice_id}  {v.name:24s}  {v.description}")
    return EXIT_OK


INDISERVER_PIDFILE = Path("~/mira/indiserver.pid").expanduser()
INDISERVER_LOGFILE = Path("~/mira/indiserver.log").expanduser()


def _indiserver_listening() -> bool:
    import socket as _socket
    try:
        with _socket.create_connection(("localhost", 7624), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_orient(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        from .tools import orient

        ok = orient(ctx=ctx, drive_seconds=args.seconds)
        if ok:
            ra, dec = ctx.mount.get_position()
            print(f"oriented: now at RA={ra:.4f}, Dec={dec:.4f}")
            print("center Polaris in the eyepiece (mira jog), then `mira sync`.")
            return EXIT_OK
        return _exit_with_clean_error("orient failed; mount did not respond to motion switch")
    finally:
        ctx.shutdown()


def cmd_up(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)

    import shutil as _shutil
    import subprocess as _subprocess
    import time as _time

    indi_bin = _shutil.which("indiserver")
    if indi_bin is None:
        return _exit_with_clean_error(
            "indiserver not on PATH. Build INDI first (see README step 2b).",
            code=EXIT_USAGE,
        )

    # Already up?
    if _indiserver_listening():
        print("indiserver: already listening on 7624")
    else:
        # Start in background; survive our exit.
        INDISERVER_LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(INDISERVER_LOGFILE, "ab")
        proc = _subprocess.Popen(
            [indi_bin, "-v", "indi_celestron_gps"],
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
        INDISERVER_PIDFILE.write_text(str(proc.pid))
        # Wait up to 8s for the port to come up.
        for _ in range(80):
            _time.sleep(0.1)
            if _indiserver_listening():
                break
        else:
            return _exit_with_clean_error(
                "indiserver started but never listened on 7624. "
                f"see {INDISERVER_LOGFILE}",
                code=EXIT_FAILURE,
            )
        print(f"indiserver: started (pid {proc.pid}, log {INDISERVER_LOGFILE})")

    # Connect to the mount.
    print("connecting to mount...", end=" ", flush=True)
    ctx = ToolContext.from_config(args.config)
    try:
        try:
            ctx.connect_mount(timeout=8.0)
        except MountError as e:
            print("FAIL")
            print()
            print(f"mount: {e}")
            print()
            print("If the hand controller still says 'Press ENTER to begin")
            print("alignment', walk it through any fake alignment (Solar System")
            print("Align is fastest; pick any object) until you reach the main")
            print("menu, then re-run `mira up`.")
            return EXIT_FAILURE
        ra, dec = ctx.mount.get_position()
        print("OK")
        print(f"  pointing: RA={ra:.4f} deg, Dec={dec:.4f} deg")
        print(f"  observer: lat={ctx.config.observer.latitude:.4f}, lon={ctx.config.observer.longitude:.4f}")
        print()
        print("mira is up. Try: mira jog, mira status, or talk to Claude Code.")
        if not args.no_voice and ctx.speaker is not None and ctx.speaker.is_configured():
            try:
                ctx.speaker.speak(
                    "[excited] Mira is up. The mount is connected. The sky is yours.",
                    blocking=False,
                )
            except SpeechError:
                pass
        return EXIT_OK
    finally:
        ctx.shutdown()


def cmd_down(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)

    import os as _os
    import signal as _signal

    killed = False
    if INDISERVER_PIDFILE.exists():
        try:
            pid = int(INDISERVER_PIDFILE.read_text().strip())
            try:
                _os.kill(pid, _signal.SIGTERM)
                killed = True
                print(f"sent SIGTERM to indiserver (pid {pid})")
            except ProcessLookupError:
                print(f"indiserver pid {pid} already gone")
            INDISERVER_PIDFILE.unlink(missing_ok=True)
        except (ValueError, OSError):
            INDISERVER_PIDFILE.unlink(missing_ok=True)
    if not killed:
        # Fallback: pkill any indiserver we may have started outside `mira up`.
        import subprocess as _subprocess

        result = _subprocess.run(
            ["pkill", "-f", "indiserver.*indi_celestron_gps"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("killed stray indiserver via pkill")
        elif _indiserver_listening():
            print("indiserver still listening on 7624 (not started by `mira up`); leaving it alone")
        else:
            print("indiserver already down")
    return EXIT_OK


def cmd_gps_push(args: argparse.Namespace) -> int:
    ctx = _build_context(args)
    try:
        ctx.connect_mount()
        # connect_mount() already invokes set_observer_info via mount.connect(),
        # but call it explicitly so the user sees a pass/fail line.
        from .tools import _local_utc_offset_hours

        ok = ctx.mount.set_observer_info(
            lat_deg=ctx.config.observer.latitude,
            lon_deg=ctx.config.observer.longitude,
            elev_m=ctx.config.observer.elevation_m,
            utc_offset_hours=_local_utc_offset_hours(),
        )
        if ok:
            print(
                f"pushed observer ({ctx.config.observer.latitude:.4f}, "
                f"{ctx.config.observer.longitude:.4f}) elev={ctx.config.observer.elevation_m}m"
                f" and current UTC to the mount."
            )
            return EXIT_OK
        else:
            print("partial push; check mira.log for which property failed.")
            return EXIT_FAILURE
    finally:
        ctx.shutdown()


def cmd_jog(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .jog import run_jog

    return run_jog(cfg)


def cmd_compose(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)

    story_path = args.story_file.expanduser()
    if not story_path.exists():
        return _exit_with_clean_error(f"story file not found: {story_path}", code=EXIT_USAGE)
    story_text = story_path.read_text(encoding="utf-8").strip()
    if not story_text:
        return _exit_with_clean_error(f"story file is empty: {story_path}", code=EXIT_USAGE)

    overrides: dict = {}
    if args.stability is not None:
        overrides["stability"] = args.stability
    if args.style is not None:
        overrides["style"] = args.style

    try:
        result = compose_narration(
            story_text=story_text,
            music_prompt=args.music,
            voice_id=args.voice,
            voice_settings=overrides or None,
            music_volume=args.music_volume,
            intro_sfx_prompt=args.intro_sfx,
            outro_sfx_prompt=args.outro_sfx,
            intro_sfx_duration_s=args.intro_sfx_duration,
            outro_sfx_duration_s=args.outro_sfx_duration,
            output_path=args.output,
        )
    except CompositionError as e:
        return _exit_with_clean_error(str(e))

    print(f"saved: {result['output_path']}")
    print(
        f"voice {result['voice_duration_s']:.1f}s, "
        f"music {result['music_duration_s']:.1f}s, voice_id {result['voice_id']}"
    )
    if result.get("intro_sfx_duration_s"):
        print(f"intro sfx {result['intro_sfx_duration_s']:.1f}s")
    if result.get("outro_sfx_duration_s"):
        print(f"outro sfx {result['outro_sfx_duration_s']:.1f}s")
    return EXIT_OK


def cmd_sfx(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)

    prompt = " ".join(args.prompt)
    try:
        result = generate_sfx(
            prompt=prompt,
            duration_seconds=args.duration,
            prompt_influence=args.influence,
            output_path=args.output,
        )
    except SfxError as e:
        return _exit_with_clean_error(str(e))

    print(f"saved: {result['output_path']}")
    if result["duration_seconds"] is not None:
        print(f"duration {result['duration_seconds']}s, prompt_influence {result['prompt_influence']}")
    else:
        print(f"duration auto, prompt_influence {result['prompt_influence']}")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .preview import PreviewError, launch_preview

    device = args.device or cfg.camera.device_name
    try:
        rc = launch_preview(
            device_name=device,
            framerate=args.framerate,
            window_size=args.size,
            flip_180=cfg.camera.flip_180,
        )
    except PreviewError as e:
        return _exit_with_clean_error(str(e))
    return rc


def cmd_fly(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)
    from .fly import run_fly
    return run_fly(cfg)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "goto":     cmd_goto,
    "sync":     cmd_sync,
    "where":    cmd_where,
    "capture":  cmd_capture,
    "solve":    cmd_solve,
    "status":   cmd_status,
    "devices":  cmd_devices,
    "resolve":  cmd_resolve,
    "preview":  cmd_preview,
    "compose":  cmd_compose,
    "sfx":      cmd_sfx,
    "say":      cmd_say,
    "voices":   cmd_voices,
    "jog":      cmd_jog,
    "fly":      cmd_fly,
    "gps-push": cmd_gps_push,
    "up":       cmd_up,
    "down":     cmd_down,
    "orient":   cmd_orient,
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
    except CompositionError as e:
        return _exit_with_clean_error(str(e))
    except SfxError as e:
        return _exit_with_clean_error(str(e))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_FAILURE


def main(argv: Optional[Sequence[str]] = None) -> Any:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()
