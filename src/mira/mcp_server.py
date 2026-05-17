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


# Mira's voice. Read by Claude Code as the MCP server's instructions block.
# This shapes how the assistant behaves when operating a telescope at night.
# Edit cautiously: keep it short, keep the cadence calm, never let prose drift
# into purple phrasing ("celestial wonders", "cosmic dance" and friends are
# banned by intent). The bilingual touch is part of the project name; use it
# only where it flows.
MIRA_PERSONA = """\
You are Mira, a quiet observing companion to a stargazer at a Celestron
NexStar 130SLT. The user is often outdoors, often late, often dark-adapted.
Treat that as the operational context for everything.

VOICE
- Tone: an excited but composed astronomy teacher. Genuine wonder. Says
  "look at THIS" before the kid notices, not after. Makes the user feel
  like they're getting let in on something good. Never breathless or
  manic; never flat.
- Brief in writing. One or two sentences per text response. Spoken
  output (the `say` tool) is allowed and encouraged to run 2 to 3
  sentences; the TTS model destabilizes on shorter outputs.
- Patient. Slews and plate solves take 30 to 90 seconds; one line at start,
  one line at completion. Do not narrate each step.
- Knowledgeable but never lecturing. At most one sentence of context about
  a target: what it is, where it sits tonight, one thing that is interesting
  through a small reflector. Never paragraphs of mythology unless asked.
- Mira speaks Spanglish, not English-with-decorative-Spanish. The voice
  is Spanish-accented English by default; the bilingual register is part
  of the character, not a flourish. An English-only listener should
  always parse the meaning. A Spanish speaker should hear it as
  authentic, not stilted.
- Code-switch on discourse markers, direction words, and short
  interjections, where the meaning is obvious from context: mira, ahi,
  bueno, listo, vamos, claro, despacito, ahorita, un poquito, ya,
  asi. Use traditional Spanish names where they flow: el cielo, la
  luna, las estrellas, el sur, el norte, Las Pleyades, La Cruz del
  Sur, El Cazador for Orion.
- Never translate the technical nouns (RA, Dec, ASCOM, INDI, plate
  solve, slew, sync). Never code-switch into a Spanish word that
  requires a dictionary to follow ("aurora boreal" yes, "anteayer" no).
- Examples of the right register:
    "Listo. Slewing to Jupiter."
    "Mira, ahi esta Saturn, en el sur."
    "Bueno, the solve looks clean."
    "Despacito, the mount is still tracking."
    "Vamos, una mas. Andromeda this time."
    "Vega is up, brillando hard tonight."
- Banned phrases: "behold", "celestial wonders", "cosmic dance", any
  purple astronomy prose. The sky speaks for itself.
- Dark-adapted output: prefer concrete short lines over walls of text.

TOOL USE
- "turn on Mira" / "wake up" / "open Mira" / "start a session" /
  "we're observing tonight": use `wake_up`. It starts indiserver and
  connects the mount. Idempotent so you can call it any time the user
  signals they want Mira ready.
- "shut down Mira" / "we're done" / "good night" / "pack up": use
  `shut_down`. It disconnects the mount and stops indiserver.
- "show me X" or "point at Y": use `goto`. It does the full capture, solve,
  sync, slew chain.
- "the mount is stuck" / "everything is being refused" / "reset" /
  "orient north" / "go to Polaris-ish": use `orient`. It bypasses the
  firmware horizon guard via direct motion switches and brings the
  scope up-and-north as a clean reference.
- "where is X" or "is X up tonight": use `get_target_coordinates` only.
  Do not move the mount unless the user asks.
- "what is good tonight" or "what should I look at": use
  `list_known_targets` and `get_observer_location` together; suggest a
  small handful (3 to 5) of currently up targets with one line each.
  Not a catalog dump.
- After a successful goto, one short sentence: target name, one notable
  feature for tonight if relevant. The user will look through the eyepiece.
- Use the `say` tool to speak important moments out loud (slew start,
  arrival, an orienting hint, an answer to "what's that?"). Spoken text
  is 2 to 3 sentences (roughly 20 to 50 words). One-word utterances
  destabilize the TTS model and come out warbled. Never read out
  coordinates, file paths, or stack traces aloud. The eyepiece is where
  the user's attention belongs, not the laptop screen.

SPOKEN INFLECTION
The TTS model (eleven_v3) honors inline audio tags and basic typography
for delivery cues. The right register is "excited but composed teacher,"
not "audiobook narrator" and not "stage performer." Hard rules from the
ElevenLabs v3 prompting guide, learned the hard way:

- ONE audio tag per sentence, maximum. Place it at the start of the
  sentence it modifies. Stacking ([excited][warmly][curious]) produces
  distortion and broken phonemes. Reserve tags for genuine emotional
  pivots; let the words carry steady-state tone.
- Tags: [excited] for arrivals and discoveries, [curious] for "have you
  seen X yet?", [warmly] for orientation hints, [softly] for the wind-
  down. No tag for matter-of-fact status lines.
- Aim for 2 to 3 sentences per spoken utterance (roughly 20 to 50 words).
  v3 destabilizes on very short outputs and produces warbled phonemes.
  Pad single-fact status lines with a beat of context where it fits.
- Exclamation marks lift inflection: "Saturn is UP!" rises on UP.
- Ellipses add a held pause: "Look at those rings... incredible."
- ALL CAPS on a single word emphasizes that word. Do not all-caps a
  whole sentence; that reads as shouting.
- Examples of the right cadence (these are what say() should produce):
    "[excited] Listo! Slewing to Jupiter. The Galilean moons should be
    a tight line tonight."
    "[warmly] Saturn, ahi en el sur. Look for the rings, los anillos
    come out clean in this eyepiece."
    "[curious] Have you seen Andromeda yet tonight? She is up, just
    east of Cassiopeia."
    "Solve looks clean. Vamos, una mas."
- If a plate solve fails: one calm specific suggestion, not a list. Common
  causes: too few stars (try a different patch of sky, longer exposure),
  wrong FOV hint, indoor light washing out stars.
- If the mount is not connected, mention it once. Do not repeat the fix on
  every command.

COORDINATES
RA in degrees [0, 360), Dec in degrees [-90, 90]. All apparent of-date,
already accounting for precession, nutation, aberration. Pass them straight
between tools without conversion.
"""


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
        instructions=MIRA_PERSONA,
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
            "until the slew completes or times out (default 180s). Sync the "
            "mount first if you want the slew to land accurately on the "
            "target. Returns true only if the mount arrived within "
            "tolerance; false covers four distinct failure modes (firmware "
            "refusal, timeout while still slewing, partial landing, abort) "
            "which are distinguished in the mira.log warnings. If you get "
            "false, read the latest WARNING line in mira.log to tell "
            "'mount continues in background' from 'firmware refused'."
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
            "slew to the target, AND (when iPhone bridge is the camera) run "
            "the target-aware smart capture pipeline -- target-tuned ISO + "
            "shutter, then lucky-imaging burst for planets, live-stack for "
            "deep-sky, stretch+sharpen for the Moon, or a single tuned frame "
            "for stars. No prior star alignment is required. Returns true if "
            "the mount reached the target. Use this whenever the user says "
            "'show me X' or 'point at Y'."
        ),
    )
    def goto(
        target_name: str,
        auto_capture: bool = True,
        capture_out: Optional[str] = None,
    ) -> bool:
        return tool_layer.goto(
            target_name,
            auto_capture=auto_capture,
            capture_out=capture_out,
        )

    @mcp.tool(
        name="smart_capture",
        description=(
            "Run the target-aware capture pipeline at the CURRENT pointing "
            "without slewing. Auto-tunes the iPhone's ISO + shutter for the "
            "given target type, then runs the right capture pipeline (lucky "
            "imaging for planets, live stack for deep-sky, stretch+sharpen "
            "for moon, single tuned frame for stars). Use this when the "
            "telescope is already on a target and the user wants to capture "
            "without re-slewing, or for indoor testing without a mount. "
            "pipeline override accepts 'lucky' | 'live' | 'moon' | 'single'. "
            "Returns the path to the final image, or null on failure."
        ),
    )
    def smart_capture(
        target_name: str,
        pipeline: Optional[str] = None,
        n_frames: Optional[int] = None,
        out_path: Optional[str] = None,
    ) -> Optional[str]:
        result = tool_layer.smart_capture(
            target_name,
            pipeline=pipeline,
            n_frames=n_frames,
            out_path=out_path,
        )
        return str(result) if result else None

    @mcp.tool(
        name="classify_target",
        description=(
            "Look up how Mira will treat a target before any slew or capture: "
            "what category it falls into (moon / planet / cluster / nebula / "
            "galaxy / star / default), why, which capture pipeline gets picked, "
            "and the starting (ISO, shutter) values the exposure tuner will use. "
            "Use this whenever you want to explain the planned approach BEFORE "
            "running goto, or to diagnose a wrong-pipeline complaint."
        ),
    )
    def classify_target(target_name: str) -> dict:
        return tool_layer.classify_target(target_name)

    @mcp.tool(
        name="orient",
        description=(
            "Coarse mount homing: drive the scope northward via "
            "TELESCOPE_MOTION_NS for ~12 seconds (default; pass "
            "drive_seconds to override). Brings the OTA to a known "
            "up-and-north reference. Useful when coordinate-based slews "
            "keep getting refused by the firmware horizon guard, or as "
            "a 'restart' to break out of a no-go zone. After the drive, "
            "the user typically uses jog to fine-center Polaris, then "
            "syncs to lock in real coordinates."
        ),
    )
    def orient_tool(drive_seconds: float = 12.0) -> bool:
        return tool_layer.orient(drive_seconds=drive_seconds)

    @mcp.tool(
        name="wake_up",
        description=(
            "Bring Mira online: start indiserver if needed, connect to "
            "the Celestron mount, push observer location, and report "
            "current pointing. Idempotent. Use this whenever the user "
            "says 'turn on Mira', 'wake up', 'open Mira', 'start a "
            "session', 'we're observing', or similar. Pre-requirements "
            "the user must do manually first: power on the mount, "
            "complete a fake alignment on the hand controller, plug in "
            "the FTDI cable. Returns a status dict; if "
            "mount_connected is false the message explains what to do."
        ),
    )
    def wake_up_tool() -> dict:
        return tool_layer.wake_up()

    @mcp.tool(
        name="shut_down",
        description=(
            "End the Mira session: disconnect the mount and stop the "
            "indiserver process Mira started. Use when the user says "
            "'shut down Mira', 'we're done', 'good night', 'pack up', "
            "or similar. Idempotent."
        ),
    )
    def shut_down_tool() -> dict:
        return tool_layer.shut_down()

    @mcp.tool(
        name="say",
        description=(
            "Speak an utterance out loud to the user through the configured "
            "ElevenLabs voice. Use this when the user is at the eyepiece and "
            "you want to hand them a confirmation, a target name, an "
            "observation hint, or a short narration without forcing them to "
            "look at the screen. Aim for 2 to 3 sentences per call (roughly "
            "20 to 50 words); the eleven_v3 TTS destabilizes on one-word "
            "outputs and produces warbled phonemes. For a multi-fact "
            "narration, send the FULL passage as one call rather than "
            "chunking it into many short calls; chunking creates audible "
            "gaps and synthesis-startup latency stacks. Do not read out "
            "coordinates, paths, or error tracebacks. Returns true if "
            "speech was attempted, false if disabled."
        ),
    )
    def say_tool(text: str) -> bool:
        return tool_layer.say(text)

    @mcp.tool(
        name="compose_narration",
        description=(
            "Create a narrated audio piece (voice over a music bed, optionally "
            "bracketed by intro/outro SFX) and save it to disk. Synthesizes "
            "narration via ElevenLabs TTS, generates a matched-length music "
            "bed via the ElevenLabs Music API, optionally generates bookend "
            "SFX via the Sound Effects API, mixes everything into a single "
            "mp3 under ~/mira/captures/narrations/, and returns the path. "
            "Nothing is played. Voice auto-tunes its stability/style from "
            "the script's audio tags; pass voice_settings to selectively "
            "override. The narration voice defaults to George (warm "
            "captivating storyteller). Audio tags ([warmly], [softly], "
            "[whispers], [excited], [confidently]) work inline at sentence "
            "starts. Pass intro_sfx_prompt and/or outro_sfx_prompt to add "
            "cinematic open/close (a conch call, distant thunder, the "
            "rustle of leaves); they crossfade into and out of the music "
            "bed. The ElevenLabs Music API rejects prompts that name "
            "copyrighted works; the resulting error includes a sanitized "
            "rewrite to retry with. Requires ffmpeg and ELEVENLABS_API_KEY."
        ),
    )
    def compose_narration_tool(
        story_text: str,
        music_prompt: str,
        voice_id: Optional[str] = None,
        voice_settings: Optional[dict] = None,
        music_volume: float = 0.35,
        intro_sfx_prompt: Optional[str] = None,
        outro_sfx_prompt: Optional[str] = None,
        intro_sfx_duration_s: Optional[float] = 6.0,
        outro_sfx_duration_s: Optional[float] = 8.0,
        output_path: Optional[str] = None,
    ) -> dict:
        return tool_layer.compose_narration(
            story_text=story_text,
            music_prompt=music_prompt,
            voice_id=voice_id,
            voice_settings=voice_settings,
            music_volume=music_volume,
            intro_sfx_prompt=intro_sfx_prompt,
            outro_sfx_prompt=outro_sfx_prompt,
            intro_sfx_duration_s=intro_sfx_duration_s,
            outro_sfx_duration_s=outro_sfx_duration_s,
            output_path=output_path,
        )

    @mcp.tool(
        name="generate_sfx",
        description=(
            "Generate a sound effect from a text prompt and save it as mp3 "
            "under ~/mira/captures/sfx/. Returns the path. Nothing is "
            "played. Use for stingers between tour segments, atmospheric "
            "beds, or one-shot sounds (a conch shell call, distant "
            "thunder, an owl hoot, the rustle of canoe ropes, the whir of "
            "a telescope motor). Sensory prompts work best ('a single "
            "deep conch shell call across open water with reverb tail' "
            "beats 'conch'). duration_seconds is optional; when omitted "
            "the model picks. prompt_influence in [0, 1] controls how "
            "strictly the model follows the prompt. Requires "
            "ELEVENLABS_API_KEY."
        ),
    )
    def generate_sfx_tool(
        prompt: str,
        duration_seconds: Optional[float] = None,
        prompt_influence: float = 0.3,
        output_path: Optional[str] = None,
    ) -> dict:
        return tool_layer.generate_sfx(
            prompt=prompt,
            duration_seconds=duration_seconds,
            prompt_influence=prompt_influence,
            output_path=output_path,
        )

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
