"""Tool layer: standalone Python functions exposed to the CLI and to MCP.

Each function in this module is one capability. The CLI maps subcommands to
these functions. The MCP server exposes these same functions as MCP tools so
Claude can call them directly. Tests can pass a fake ToolContext to swap in
mocks for the mount, camera, solver, and ephemeris.

Docstrings here become MCP tool descriptions. Write them so a model picking
between tools can decide what to call from the description alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .camera import Camera, CameraError
from .config import Config, load_config, setup_logging
from .ephemeris import Ephemeris, NameNotFoundError, get_ephemeris
from .mount import CelestronMount, MountError, ObserverInfo
from .solver import SolveFailed, Solver, SolverError
from .speech import SpeechError, Speaker
from .state import StateDB

logger = logging.getLogger(__name__)


def _local_utc_offset_hours() -> float:
    """Local timezone offset from UTC, in hours. Positive east of UTC."""
    import time as _time
    if _time.daylight and _time.localtime().tm_isdst:
        return -_time.altzone / 3600.0
    return -_time.timezone / 3600.0


@dataclass
class ToolContext:
    """Shared dependencies for the tool layer. Construct once, reuse across calls.

    The mount is the only stateful piece: it owns a TCP connection to indiserver.
    Call `.connect_mount()` before any mount-touching tool runs, and
    `.shutdown()` on exit.
    """

    config: Config
    state: StateDB
    mount: CelestronMount
    camera: Camera
    solver: Solver
    ephemeris: Ephemeris
    speaker: Optional[Speaker] = None
    session_id: Optional[int] = None
    _mount_connected: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_config(cls, config_path: Path | str | None = None) -> "ToolContext":
        cfg = load_config(config_path)
        setup_logging(cfg)
        state = StateDB(cfg.storage.state_db)
        state.init()
        mount = CelestronMount(
            host=cfg.mount.indi_host,
            port=cfg.mount.indi_port,
            serial_port=cfg.mount.port or None,
            observer=ObserverInfo(
                latitude_deg=cfg.observer.latitude,
                longitude_deg=cfg.observer.longitude,
                elevation_m=cfg.observer.elevation_m,
                utc_offset_hours=_local_utc_offset_hours(),
            ),
        )
        camera = Camera(
            device_name=cfg.camera.device_name,
            capture_dir=cfg.camera.capture_dir,
            warmup_seconds=cfg.camera.warmup_seconds,
        )
        # Solver does not validate the ASTAP binary at construction time, so
        # subcommands that do not need plate solving (resolve, where, status)
        # still work even when ASTAP is not yet installed.
        solver = Solver(
            astap_path=cfg.solver.astap_path,
            estimated_fov_deg=cfg.solver.estimated_fov_deg,
            timeout_seconds=cfg.solver.timeout_seconds,
            star_db=cfg.solver.star_db,
        )
        ephemeris = get_ephemeris(
            observer_lat_deg=cfg.observer.latitude,
            observer_lon_deg=cfg.observer.longitude,
            elevation_m=cfg.observer.elevation_m,
        )
        speaker = (
            Speaker(
                voice_id=cfg.speech.voice_id,
                model_id=cfg.speech.model_id,
                voice_settings={
                    "stability": cfg.speech.stability,
                    "similarity_boost": cfg.speech.similarity_boost,
                    "style": cfg.speech.style,
                    "use_speaker_boost": cfg.speech.use_speaker_boost,
                },
            )
            if cfg.speech.enabled
            else None
        )
        return cls(
            config=cfg,
            state=state,
            mount=mount,
            camera=camera,
            solver=solver,
            ephemeris=ephemeris,
            speaker=speaker,
        )

    def connect_mount(self, timeout: float = 10.0) -> None:
        if not self._mount_connected:
            self.mount.connect(timeout=timeout)
            self._mount_connected = True

    def disconnect_mount(self) -> None:
        if self._mount_connected:
            try:
                self.mount.disconnect()
            finally:
                self._mount_connected = False

    def shutdown(self) -> None:
        self.disconnect_mount()
        if self.session_id is not None:
            try:
                self.state.end_session(self.session_id)
            except Exception:  # noqa: BLE001
                logger.exception("failed to end session %d cleanly", self.session_id)


_default_ctx: Optional[ToolContext] = None


def set_default_context(ctx: ToolContext | None) -> None:
    """Install (or clear) the module-level context that tools fall back to."""
    global _default_ctx
    _default_ctx = ctx


def get_default_context() -> ToolContext:
    """Return the module-level context, building one from config on first use."""
    global _default_ctx
    if _default_ctx is None:
        _default_ctx = ToolContext.from_config()
    return _default_ctx


def _ctx(ctx: ToolContext | None) -> ToolContext:
    return ctx if ctx is not None else get_default_context()


def _speak(ctx: ToolContext, text: str) -> None:
    """Best-effort speech: silently swallow errors so a TTS hiccup never
    blocks an observation. Logged so debugging is possible.
    """
    if ctx.speaker is None or not ctx.speaker.is_configured():
        return
    try:
        ctx.speaker.speak(text, blocking=ctx.config.speech.blocking)
    except SpeechError as e:
        logger.warning("speech failed: %s", e)


def say(text: str, *, ctx: ToolContext | None = None) -> bool:
    """Speak text out loud through the configured TTS voice.

    Use this to give the user a short audible confirmation while their
    eye stays glued to the eyepiece. Keep spoken text shorter than written
    text: 5 to 12 words is plenty. Do not read out coordinates, image paths,
    or stack traces.

    Args:
        text: short utterance to synthesize and play.

    Returns:
        True if speech was attempted, False if speech is disabled or no
        API key is configured.
    """
    c = _ctx(ctx)
    if c.speaker is None or not c.speaker.is_configured():
        return False
    try:
        c.speaker.speak(text, blocking=c.config.speech.blocking)
        return True
    except SpeechError as e:
        logger.warning("speech failed: %s", e)
        return False


def get_target_coordinates(name: str, *, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Resolve a target name to apparent equatorial coordinates.

    Use this when the user asks to point at a named object (planet, Messier
    object, named star, common DSO alias). Returns coordinates valid right
    now at the configured observer location, accounting for precession,
    nutation, and aberration. Pass the result to `slew_to`.

    Args:
        name: Target name. Examples: "Jupiter", "Mars", "M31", "Andromeda",
              "Vega", "Pleiades", "Orion Nebula", "Polaris".

    Returns:
        Tuple of (ra_degrees, dec_degrees) where RA is in [0, 360) and Dec
        is in [-90, 90].

    Raises:
        NameNotFoundError: if the name is not in the catalog.
    """
    coords = _ctx(ctx).ephemeris.resolve(name)
    return coords.ra_deg, coords.dec_deg


def capture_frame(*, ctx: ToolContext | None = None) -> Path:
    """Capture a single frame from the iPhone via Continuity Camera.

    Saves a JPEG under the configured capture directory. Use this before
    `plate_solve` to get a starfield image of the current pointing.

    Returns:
        Path to the saved JPEG.

    Raises:
        CameraError: if imagesnap is missing, the camera is not visible,
            or capture fails.
    """
    return _ctx(ctx).camera.capture()


def plate_solve(
    image_path: Path | str,
    *,
    ra_hint_deg: Optional[float] = None,
    dec_hint_deg: Optional[float] = None,
    ctx: ToolContext | None = None,
) -> Optional[tuple[float, float]]:
    """Plate-solve an image to find what the telescope is pointed at.

    Runs ASTAP against the image. Optional RA/Dec hints (typically the
    mount's reported position) speed up the solve substantially.

    Args:
        image_path: path to a JPEG, PNG, or FITS image.
        ra_hint_deg: optional approximate RA in degrees.
        dec_hint_deg: optional approximate Dec in degrees.

    Returns:
        Tuple of (ra_degrees, dec_degrees) on success, or None if ASTAP
        could not find a solution.
    """
    c = _ctx(ctx)
    try:
        result = c.solver.solve(
            image_path,
            ra_hint_deg=ra_hint_deg,
            dec_hint_deg=dec_hint_deg,
        )
    except SolveFailed as e:
        logger.info("plate solve failed: %s", e)
        return None
    except SolverError as e:
        logger.error("plate solve error: %s", e)
        raise
    return result.ra_deg, result.dec_deg


def sync_mount(ra_deg: float, dec_deg: float, *, ctx: ToolContext | None = None) -> bool:
    """Tell the mount its current pointing is at the given RA/Dec.

    Call this after a successful `plate_solve` so the mount knows where it
    actually is. This is what replaces traditional star alignment: the
    plate solution overrides whatever fake alignment was used at startup.

    Args:
        ra_deg: apparent RA in degrees [0, 360).
        dec_deg: apparent Dec in degrees [-90, 90].

    Returns:
        True if the mount accepted the sync.
    """
    c = _ctx(ctx)
    c.connect_mount()
    success = c.mount.sync(ra_deg=ra_deg, dec_deg=dec_deg)
    if success:
        c.state.record_sync(
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            session_id=c.session_id,
        )
    return success


def slew_to(ra_deg: float, dec_deg: float, *, ctx: ToolContext | None = None) -> bool:
    """Command the mount to slew to the given apparent RA/Dec.

    This issues the slew and blocks until the mount reports completion or
    times out. Sync the mount first with `sync_mount` if it has not already
    been synced this session.

    Args:
        ra_deg: target RA in degrees [0, 360).
        dec_deg: target Dec in degrees [-90, 90].

    Returns:
        True if the slew completed successfully.
    """
    c = _ctx(ctx)
    c.connect_mount()
    slew_id = c.state.record_slew(
        target_name=None,
        target_ra_deg=ra_deg,
        target_dec_deg=dec_deg,
        session_id=c.session_id,
    )
    success = c.mount.slew_to(ra_deg=ra_deg, dec_deg=dec_deg)
    achieved_ra, achieved_dec = c.mount.get_position()
    c.state.update_slew_result(
        slew_id=slew_id,
        achieved_ra_deg=achieved_ra,
        achieved_dec_deg=achieved_dec,
        success=success,
    )
    return success


def get_mount_position(*, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Query the mount for its current pointing.

    Returns the mount's own belief about where it is pointing, which is
    only as good as its last sync. Use `plate_solve` if you need ground truth.

    Returns:
        Tuple of (ra_degrees, dec_degrees).
    """
    c = _ctx(ctx)
    c.connect_mount()
    return c.mount.get_position()


def wait_for_slew_complete(timeout: int = 60, *, ctx: ToolContext | None = None) -> bool:
    """Block until the mount finishes its current slew.

    Useful when slew was issued asynchronously, or to wait out tracking
    settling. `slew_to` already blocks internally; this is for cases where
    a slew was issued by other means.

    Args:
        timeout: maximum seconds to wait.

    Returns:
        True if the mount became idle within the timeout, False if it timed out.
    """
    c = _ctx(ctx)
    c.connect_mount()
    return c.mount.wait_slew_complete(timeout=float(timeout))


def get_observer_location(*, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Return the configured observer latitude and longitude in degrees.

    Used by ephemeris computations and as a sanity check for the operator.
    Configure via `observer.latitude` and `observer.longitude` in config.yaml.

    Returns:
        Tuple of (latitude_degrees, longitude_degrees). Negative latitude is
        southern hemisphere; negative longitude is west of Greenwich.
    """
    obs = _ctx(ctx).config.observer
    return obs.latitude, obs.longitude


def orient(*, ctx: ToolContext | None = None, drive_seconds: float = 12.0) -> bool:
    """Coarse mount orientation: drive the scope upward and northward until
    it is pointing roughly at Polaris.

    For users in the Northern Hemisphere, Polaris sits at altitude equal
    to your latitude (38 degrees from Louisville, KY) and stays fixed.
    Driving the mount toward it gives a known reference point even when
    the alignment is fake and coordinate-based slews keep getting
    refused by the firmware horizon guard.

    Mechanism: this fires the TELESCOPE_MOTION_NS=NORTH switch for
    `drive_seconds`, then stops. The motion switches drive the motors
    directly and bypass the coordinate-based goto, so the firmware lock
    that blocks `slew_to` calls does not apply.

    After the drive, the user typically uses `mira jog` to fine-center
    Polaris in the eyepiece, then `mira sync` to lock in a real
    coordinate frame.

    Args:
        drive_seconds: how long to drive north. Default 12s, which moves
            the scope through roughly half its travel at slew rate 5.

    Returns:
        True if the motion switch was successfully sent.
    """
    import time as _time

    c = _ctx(ctx)
    c.connect_mount()
    _speak(c, "[excited] Orienting north toward Polaris. Drive incoming.")
    try:
        c.mount.client.set_switch(
            "TELESCOPE_MOTION_NS",
            {"MOTION_NORTH": True, "MOTION_SOUTH": False},
        )
        _time.sleep(drive_seconds)
        c.mount.client.set_switch(
            "TELESCOPE_MOTION_NS",
            {"MOTION_NORTH": False, "MOTION_SOUTH": False},
        )
    except MountError as e:
        logger.error("orient: motion switch failed: %s", e)
        _speak(c, "Orient failed. Mount did not accept the motion switch.")
        return False
    _time.sleep(0.5)
    try:
        ra, dec = c.mount.get_position(timeout=3.0)
        logger.info("orient: drove %ss north; now at RA=%.4f Dec=%.4f", drive_seconds, ra, dec)
    except MountError:
        pass
    _speak(c, "[warmly] Pointing roughly north. Center Polaris with jog, then sync.")
    return True


def goto(target_name: str, *, ctx: ToolContext | None = None) -> bool:
    """Plate-solve current pointing, sync the mount, and slew to a named target.

    This is the primary headline operation. The flow is:
      1. Resolve target name to apparent RA/Dec.
      2. Capture a frame of the current sky.
      3. Plate-solve to learn true current pointing.
      4. Sync the mount to that solved position.
      5. Slew to the target.

    No traditional star alignment is required. The user does a deliberately
    bad fake alignment via the hand controller; this routine overwrites it.

    Args:
        target_name: anything `get_target_coordinates` accepts, e.g.
            "Jupiter", "M31", "Vega".

    Returns:
        True if the mount reached the target. False if any step failed.
    """
    c = _ctx(ctx)
    c.connect_mount()

    # 1. Target.
    try:
        target_ra, target_dec = get_target_coordinates(target_name, ctx=c)
    except NameNotFoundError as e:
        logger.error("goto: %s", e)
        _speak(c, f"I do not know {target_name}.")
        return False
    logger.info("goto: target %s at RA=%.4f Dec=%.4f", target_name, target_ra, target_dec)
    _speak(c, f"Slewing to {target_name}.")

    # 2. Capture for solving the current pointing.
    try:
        cur_ra_hint, cur_dec_hint = c.mount.get_position()
    except MountError:
        cur_ra_hint, cur_dec_hint = None, None
    try:
        image = capture_frame(ctx=c)
    except CameraError as e:
        logger.error("goto: capture failed: %s", e)
        return False

    # 3. Solve.
    solved = plate_solve(
        image,
        ra_hint_deg=cur_ra_hint,
        dec_hint_deg=cur_dec_hint,
        ctx=c,
    )
    if solved is None:
        logger.error("goto: plate solve failed for %s", image)
        return False
    solved_ra, solved_dec = solved
    c.state.record_sync(
        ra_deg=solved_ra,
        dec_deg=solved_dec,
        image_path=str(image),
        session_id=c.session_id,
    )

    # 4. Sync mount.
    sync_ok = c.mount.sync(ra_deg=solved_ra, dec_deg=solved_dec)
    if not sync_ok:
        logger.error("goto: mount did not accept sync")
        return False
    # Give the mount a beat to commit the sync before issuing a slew.
    time.sleep(0.5)

    # 5. Slew to target.
    slew_id = c.state.record_slew(
        target_name=target_name,
        target_ra_deg=target_ra,
        target_dec_deg=target_dec,
        session_id=c.session_id,
    )
    success = c.mount.slew_to(ra_deg=target_ra, dec_deg=target_dec)
    achieved_ra, achieved_dec = c.mount.get_position()
    c.state.update_slew_result(
        slew_id=slew_id,
        achieved_ra_deg=achieved_ra,
        achieved_dec_deg=achieved_dec,
        success=success,
    )
    if success:
        logger.info(
            "goto %s: arrived at RA=%.4f Dec=%.4f", target_name, achieved_ra, achieved_dec
        )
        _speak(c, f"{target_name} acquired.")
    else:
        logger.warning("goto %s: slew did not finish in time", target_name)
        _speak(c, f"{target_name} slew did not complete.")
    return success


# Public list of tool functions. Used by the MCP server to enumerate.
TOOLS = (
    get_target_coordinates,
    capture_frame,
    plate_solve,
    sync_mount,
    slew_to,
    get_mount_position,
    wait_for_slew_complete,
    get_observer_location,
    goto,
    orient,
    say,
)
