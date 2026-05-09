"""MCP server: exposes Mira's tool layer to Claude Code over stdio.

Run via the `mira-mcp` entry point or `python -m mira.mcp_server`.

Uses FastMCP from the official Python SDK. Each tool wraps the underlying
function in mira.tools so Claude sees a flat list with rich descriptions
(extracted from docstrings) and JSON-schema parameter types (extracted
from type hints).

Lifecycle: on startup, build a ToolContext from config. On shutdown,
disconnect the mount cleanly. Failures during startup are logged and
the server exits non-zero so the MCP host can surface the problem.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP

from .config import ConfigError
from . import tools as tool_layer
from .tools import ToolContext

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[ToolContext]:
    """Build the ToolContext at startup. Disconnect at shutdown."""
    try:
        ctx = ToolContext.from_config()
    except ConfigError as e:
        logger.error("config error: %s", e)
        raise
    tool_layer.set_default_context(ctx)
    logger.info(
        "mira mcp ready: observer at (%.4f, %.4f); mount via %s:%d",
        ctx.config.observer.latitude,
        ctx.config.observer.longitude,
        ctx.config.mount.indi_host,
        ctx.config.mount.indi_port,
    )
    try:
        yield ctx
    finally:
        logger.info("mira mcp shutting down")
        try:
            ctx.shutdown()
        finally:
            tool_layer.set_default_context(None)


def build_server() -> FastMCP:
    """Construct the FastMCP server with all Mira tools registered."""
    mcp = FastMCP(
        name="mira",
        instructions=(
            "Mira is a telescope control surface for the Celestron NexStar 130SLT. "
            "Use `get_target_coordinates` to look up RA/Dec for an object. "
            "Use `goto` for the headline flow: capture, plate-solve, sync, slew. "
            "Use `capture_frame` then `plate_solve` if you want to verify pointing "
            "without moving. `sync_mount` and `slew_to` are the lower-level building "
            "blocks if you need explicit control. Coordinates are always in degrees, "
            "RA in [0, 360) and Dec in [-90, 90]."
        ),
        lifespan=_lifespan,
    )

    @mcp.tool(
        name="get_target_coordinates",
        description=(
            "Resolve a target name to apparent equatorial coordinates at the "
            "observer's location and current time. Supports planets, the Sun, "
            "the Moon, Messier objects (M1 to M110), named bright stars, and "
            "common DSO aliases like 'Andromeda' or 'Pleiades'. Returns RA/Dec "
            "in degrees, accounting for precession, nutation, and aberration."
        ),
    )
    def get_target_coordinates(name: str) -> dict[str, float]:
        ra, dec = tool_layer.get_target_coordinates(name)
        return {"ra_deg": ra, "dec_deg": dec}

    @mcp.tool(
        name="capture_frame",
        description=(
            "Capture a single image from the iPhone via Continuity Camera and "
            "save it under the configured capture directory. Use this before "
            "plate_solve when you want to learn what the telescope is pointed at. "
            "Returns the absolute path to the saved JPEG."
        ),
    )
    def capture_frame() -> str:
        path = tool_layer.capture_frame()
        return str(path)

    @mcp.tool(
        name="plate_solve",
        description=(
            "Run ASTAP against a saved image to determine its true center "
            "coordinates. Optional RA/Dec hints (typically the mount's last "
            "known position) speed up the solve. Returns RA/Dec in degrees, "
            "or null if the solver could not find a match."
        ),
    )
    def plate_solve(
        image_path: str,
        ra_hint_deg: Optional[float] = None,
        dec_hint_deg: Optional[float] = None,
    ) -> Optional[dict[str, float]]:
        result = tool_layer.plate_solve(
            Path(image_path),
            ra_hint_deg=ra_hint_deg,
            dec_hint_deg=dec_hint_deg,
        )
        if result is None:
            return None
        return {"ra_deg": result[0], "dec_deg": result[1]}

    @mcp.tool(
        name="sync_mount",
        description=(
            "Tell the mount its current pointing is at the given apparent "
            "RA/Dec. Use after a successful plate_solve to teach the mount "
            "where it actually is. This replaces traditional star alignment. "
            "Returns true if the mount accepted the sync."
        ),
    )
    def sync_mount(ra_deg: float, dec_deg: float) -> bool:
        return tool_layer.sync_mount(ra_deg, dec_deg)

    @mcp.tool(
        name="slew_to",
        description=(
            "Command the mount to slew to the given apparent RA/Dec. Blocks "
            "until the slew completes or times out. Sync the mount first if "
            "you want the slew to land accurately on the target."
        ),
    )
    def slew_to(ra_deg: float, dec_deg: float) -> bool:
        return tool_layer.slew_to(ra_deg, dec_deg)

    @mcp.tool(
        name="get_mount_position",
        description=(
            "Query the mount for its current reported pointing. This is the "
            "mount's belief, only as accurate as its last sync. Use plate_solve "
            "if you need ground truth. Returns RA and Dec in degrees."
        ),
    )
    def get_mount_position() -> dict[str, float]:
        ra, dec = tool_layer.get_mount_position()
        return {"ra_deg": ra, "dec_deg": dec}

    @mcp.tool(
        name="wait_for_slew_complete",
        description=(
            "Block until the mount finishes its current slew, or until the "
            "timeout (in seconds) elapses. slew_to already blocks internally; "
            "this is for when a slew was issued by other means. Returns true "
            "if the mount became idle within the timeout."
        ),
    )
    def wait_for_slew_complete(timeout: int = 60) -> bool:
        return tool_layer.wait_for_slew_complete(timeout)

    @mcp.tool(
        name="get_observer_location",
        description=(
            "Return the configured observer latitude and longitude in degrees. "
            "Negative latitude is the southern hemisphere; negative longitude "
            "is west of Greenwich. Set in observer.latitude / observer.longitude "
            "in config.yaml."
        ),
    )
    def get_observer_location() -> dict[str, float]:
        lat, lon = tool_layer.get_observer_location()
        return {"latitude_deg": lat, "longitude_deg": lon}

    @mcp.tool(
        name="goto",
        description=(
            "Headline flow: resolve a target name, capture a frame of the "
            "current sky, plate-solve to learn true pointing, sync the mount, "
            "and slew to the target. No prior star alignment is required. "
            "Returns true if the mount reached the target. Use this whenever "
            "the user says 'show me X' or 'point at Y'."
        ),
    )
    def goto(target_name: str) -> bool:
        return tool_layer.goto(target_name)

    @mcp.tool(
        name="list_known_targets",
        description=(
            "Return the catalog of names the resolver understands, grouped by "
            "category: solar_system, named_stars, messier, dso_aliases. Useful "
            "to confirm whether a user's target name will resolve before "
            "issuing a goto."
        ),
    )
    def list_known_targets() -> dict[str, list[str]]:
        from .ephemeris import list_known_names

        return list_known_names()

    return mcp


def main() -> None:
    """Entry point for `mira-mcp`. Runs the server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
