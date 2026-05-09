# Mira: future work

Out of scope for v0.1.0. Listed in rough priority order, but feel free to pull from anywhere.

## Done since v0.1.0 (kept here as a reminder of scope evolution)

- [x] ElevenLabs TTS integration with eleven_v3, audio tags, streaming PCM via ffplay.
- [x] Stargazing-tuned persona (Spanglish register, excited-teacher inflection) baked into MCP server `instructions`.
- [x] `mira preview` live iPhone feed via ffplay for eyepiece alignment.
- [x] `mira jog` curses TUI for keyboard-driven mount nudging.
- [x] `mira gps-push` to feed observer location and UTC into the mount (best-effort; Celestron firmware locks this post-alignment).
- [x] Auto-pushing observer info on every `mount.connect()` via ToolContext.
- [x] MCP server registered as a user-scope Claude Code MCP via `claude mcp add`.

## Capability

- [ ] Local LLM fallback via Ollama for fully offline conversational use. v0.1.0 routes all conversational use through Claude Code; a portable backup matters at dark sky sites with no signal.
- [ ] Motorized focuser support (Celestron Focus Motor or third-party). Wire it up via INDI's `indi_celestron_focuser`.
- [ ] Real astro camera support via INDI (ZWO ASI series, QHY, etc.). The Continuity Camera afocal rig is a cost-free starting point; deep-sky imaging really wants a cooled CMOS at prime focus.
- [ ] Auto-focus loop using HFR or FWHM minimization on a captured frame.
- [ ] Tracking quality monitoring. Periodically capture, solve, and log the drift rate. Surface this in `mira status`.
- [ ] Per-slew re-syncing for high precision. After slew, capture and solve at the target, then sync and re-slew so center-of-frame matches the requested coordinates within arcseconds.
- [ ] Multi-target tour scripts. "Show me the planets visible tonight"; "M-objects in Sagittarius from the last 30 minutes of dusk".
- [ ] Image stacking for deep-sky targets. Multiple frames per target, registered and stacked locally.
- [ ] Plate-solve preflight. Estimate a likely solve outcome from frame statistics before running ASTAP, and warn if the field looks too sparse.

## Safety and ergonomics

- [ ] Goto safety limits. Reject targets below the horizon, near the meridian-flip danger zone, or in directions that hit tripod legs.
- [ ] Park position support. `mira park` slews to a configured safe stow attitude.
- [ ] Cooldown sequence on shutdown. Cap, wait for thermal equilibrium with ambient, then power down.
- [ ] Weather and cloud integration. Skip targets behind known clouds via a sky-quality meter or webcam analysis. Local National Weather Service feeds work.
- [ ] Auto-detect observer location via macOS CoreLocation (PyObjC), so the user does not have to hand-edit lat/lon in config when traveling to a dark site.

## Interface

- [ ] Web UI for remote control from a phone. Read-only first, with a confirmation step for movement commands.
- [ ] Voice activation directly on the Mac (without going through Claude Code).
- [ ] Auto-pull of recent capture thumbnails to a local web gallery.
- [ ] Live-stack preview during exposure runs.
- [ ] In `mira jog`, hold-to-slew via TELESCOPE_MOTION_NS / MOTION_WE switches instead of step-per-keypress, for smoother feel during fine centering.

## Catalog

- [ ] NGC and IC catalogs for resolving objects not in the Messier list.
- [ ] Hipparcos / Tycho-2 ingest so any Bayer/Flamsteed designation resolves.
- [ ] Comet ephemeris loading from JPL Horizons.
- [ ] Asteroid orbital element ingest from MPC.
- [ ] Custom user-defined targets in config (named asterisms, observing-list shortcuts).

## Plate solving

- [ ] Astrometry.net fallback. Local solver works offline; web solver is a reliable backup when ASTAP fails.
- [ ] Live solve on a video stream rather than discrete captures, for faster goto-and-confirm iteration.
- [ ] Cached solve results. Skip the ASTAP run if we already solved this frame in the current session.

## Mount drivers

- [ ] AltAz mounts beyond Celestron NexStar (iOptron, Sky-Watcher, Vixen). The architecture isolates the mount in `mount.py`; another driver class plus a config switch is enough.
- [ ] Equatorial mount support. Field rotation handling, polar align assist via plate solving.
- [ ] PEC and PEMPro-style periodic error correction (less relevant on the 130SLT alt-az).

## Reliability and operations

- [ ] Automatic reconnection if the INDI server restarts mid-session.
- [ ] Health-check daemon that watches the INDI socket and the mount's last-heard-from time.
- [ ] Better mount.py XML parser. The current code is small and works; eventually swap for a streaming parser that handles BLOB binary types and multi-device subscriptions cleanly.
- [ ] Rate-limiting on rapid-fire slew commands. The 130SLT's firmware can lock up if you send a new slew before the previous one acks.
- [ ] Structured-logging mode (JSON Lines) so a future supervisor can ingest mira.log into Loki/Grafana without parsing.

## Developer experience

- [ ] CI pipeline (GitHub Actions): lint, type-check, run unit tests on every push. The full hardware loop cannot run in CI but everything else can.
- [ ] mypy strict mode, not just pyright surface check.
- [ ] Tag-based release automation. Cut wheel and source dist on `v*` tags.
- [ ] Hardware emulator. A fake INDI server that behaves like a NexStar so the full test suite runs without the telescope.
