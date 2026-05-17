# CLAUDE.md

Project-specific context for working in this repo. README.md is for users; this is for whoever (Claude or human) is editing the code.

## Persona

Mira's voice lives in `MIRA_PERSONA` at the top of `src/mira/mcp_server.py`. It is the `instructions` field of the FastMCP server, which Claude Code reads when operating any of Mira's tools. The shape: quiet, brief, patient, knowledgeable without lecturing, never purple. Banned phrases ("behold", "celestial wonders", etc.) are listed there.

The bilingual register is Spanglish, not English-with-decorative-Spanish. Code-switch on discourse markers and direction words (mira, ahi, bueno, listo, vamos, el cielo, la luna, el sur). Technical terms stay English (RA, Dec, plate solve, INDI). The default TTS voice (Vega, when on Starter plan) speaks Spanish-accented English; the persona has to match that or the seam shows.

If you change the persona, keep it short and keep the cadence calm; the user is dark-adapted under a real sky when this is in use.

## Hard rules

- **No em dashes (U+2014) anywhere.** Code, comments, docstrings, README, commit messages, log output. Use periods, commas, or short sentences. Verify with `grep -rPn '\xe2\x80\x94' .` (matches the UTF-8 byte sequence for U+2014) before committing. This is a strict, project-wide style choice.
- Coordinate convention is **degrees** throughout the public API. RA in [0, 360), Dec in [-90, 90]. All positions are **apparent of-date** at the topocentric observer (precession + nutation + aberration applied via Skyfield). Returning J2000 catalog values directly produces a consistent pointing offset that looks like a calibration bug but is actually a math bug.
- Mount is INDI-only. Do not substitute pyindi-client (its C extension is brittle on Apple Silicon) or INDIGO (different wire protocol).

## Gotchas that have bitten us

- `Solver.__init__` deliberately does NOT validate the ASTAP binary. Validation happens on first `solve()`. This is intentional: `ToolContext.from_config()` constructs a Solver eagerly, and tools that do not need ASTAP (`resolve`, `where`, `status`) must keep working when ASTAP is not installed yet. Test guard: `tests/test_solver.py::TestSolverConstruction::test_construction_does_not_validate_binary`.
- INDI's Celestron driver is `indi_celestron_gps` (binary name) and advertises device `"Celestron GPS"`. Despite the name it covers all current NexStar mounts including the non-GPS 130SLT. `mount.CelestronMount` defaults to `device="Celestron GPS"`. The earlier name `indi_celestron_nexstar_telescope` is gone in INDI 2.x.
- `mount.port` in config.yaml MUST be the `cu.` (callout) form on macOS, not the `tty.` (callin) form. The driver auto-saves `cu.usbserial-XXX` and pushing the `tty.` variant via DEVICE_PORT changes the open semantics in a way the Celestron driver can't handle. Symptom if wrong: CONNECT switch goes Ok but the mount handshake silently fails and EQUATORIAL_EOD_COORD never moves off 0/0.
- `IndiClient._read_loop` only processes top-level `*Vector` elements (def/set), not their inner singular elements (`defSwitch`, `oneNumber`, etc.). XMLPullParser fires "end" events for every element, so calling `elem.clear()` on the inner elements wipes their attributes before the outer wrapper is processed. The filter is `tag.startswith("def"/"set") AND tag.endswith("Vector")`. Get this wrong and every property's elements collapse to a single empty-string key.
- `CelestronMount.connect()` blocks until the driver has pushed at least one *update* of EQUATORIAL_EOD_COORD (not just the def). Without that wait, `get_position()` returns the def's 0/0 placeholders and a caller that immediately syncs would hand the mount a totally wrong reference frame. Implemented in `_wait_for_coord_poll`.
- `wait_slew_complete` is two-phase: first waits up to 3s for the COORD state to flip to Busy (slew started), then waits the rest of the timeout for Ok (slew settled). Without the Busy wait, EQUATORIAL_EOD_COORD's pre-slew `Ok` state makes the function return immediately and `slew_to` reports success without any motion. Edge: a no-op slew (target very close to current) never sees Busy; we handle that by inspecting current state when the Busy timeout fires. Internally, `_wait_slew_complete_outcome` returns a richer `WaitOutcome` (SETTLED / NOT_STARTED / TIMED_OUT / ABORTED); `wait_slew_complete` collapses that to bool for back-compat.
- `slew_to` returns bool but is a thin wrapper over `slew_to_with_outcome`, which returns a `SlewOutcome` enum (ARRIVED / NOOP / REFUSED / PARTIAL / TIMED_OUT / ABORTED). Use the outcome variant when you need to distinguish failure modes (e.g., retry on TIMED_OUT, escalate on REFUSED). The default timeout is 180s, sized for worst-case alt-az slews on a 130SLT with bad alignment where the firmware has to drive a long motor-axis path. The earlier 60s default was surfacing genuine in-progress slews as `False` indistinguishable from firmware refusals; now TIMED_OUT logs explicitly say "mount continues in background" and REFUSED logs say "moved 0 percent of requested" so you do not have to grep to tell them apart.
- `set_observer_info` (GEOGRAPHIC_COORD + TIME_UTC) is best-effort. Once the hand controller has completed an alignment, the mount **silently rejects** location/time pushes — GEOGRAPHIC_COORD goes to `state=Alert` while TIME_UTC keeps its saved-config value. This is by design in Celestron firmware, not a Mira bug. Mira's plate-solve / sync workflow does not depend on the mount's own time or location, so the rejection is mostly cosmetic. To force fresh values, undo the alignment via the hand controller first.
- Building INDI from source on macOS: pass `-DCMAKE_INSTALL_RPATH=/opt/homebrew/lib -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON` to cmake, otherwise installed binaries fail at runtime with `Library not loaded ... no LC_RPATH's found`. GSL is a required brew dep; cmake fails without it.
- Continuity Camera shows up under the iPhone's user-set name (e.g. "Andrew Camera"), NOT the literal string "iPhone". `imagesnap` on this build prints `Video Devices:` as the header (with colon, no "found"). `camera.list_devices` filters on `lower("video device")` prefix to handle both variants and strips `=> ` and `* ` line prefixes.
- Spoken output goes through `src/mira/speech.py`. Hot path: stream PCM 22.05kHz mono from `/v1/text-to-speech/{voice_id}/stream` straight into `ffplay` stdin (lower latency than MP3 + afplay). Falls back to MP3 + afplay when ffplay is missing. The API key is read from `os.environ["ELEVENLABS_API_KEY"]` first, then `~/mira/.env`. Never put the key in `config.yaml`. The example config defaults to Sarah (EXAVITQu4vr4xnSDxMaL), free-tier accessible; the local `~/mira/config.yaml` is configured for Vega (`pTX8uGyVgHCWLj6IkcbC`, Spanish-accented narrator) which requires the Starter plan. `mira voices` to browse.
- Default model is `eleven_v3` (most expressive; supports inline audio tags `[excited]`, `[curious]`, `[warmly]`, `[softly]`). Auto-fallback to `eleven_turbo_v2_5` on HTTP error. Watch out: v3 rejects `optimize_streaming_latency` with HTTP 400 "unsupported_model"; that param is conditionally attached only for v2-family models.
- The `say` tool in `tools.py` is best-effort: speech failures (network, quota, rate-limit) are logged but never raise, so a TTS hiccup cannot block an observation.
- `mira preview` shells out to `ffplay` from ffmpeg. ffmpeg is an optional dep, only needed for that one subcommand. Device name resolution is a case-insensitive substring match against `ffmpeg -f avfoundation -list_devices true` output, indexed by AVFoundation index, not name (ffplay's `-i` argument).
- ASTAP `d05`, `d20`, `d50`, `d80` star DBs install to `/usr/local/opt/astap/`, not the obvious places. The `verify_setup.py` candidate list includes that path. Old ASTAP docs reference H17/H18; those are gone.
- Repo root and runtime root are the same directory (`~/mira/`). `.gitignore` covers `config.yaml`, `state.db`, `mira.log`, `captures/`, the Skyfield ephemeris cache, and ASTAP DB files, so personal config stays out of git. Be careful adding new untracked files at the repo root.
- **Newtonian image is inverted 180°.** `CameraConfig.flip_180` defaults True: every saved capture is post-processed through `ffmpeg -vf hflip,vflip` and the live preview window adds the same filter. ASTAP plate-solving works in either orientation; the flip is purely visual. `jog.py` arrow mapping is already eyepiece-relative (up-key moves star up in eyepiece view) for the same reason.
- **`TELESCOPE_SLEW_RATE` element names are driver-specific.** The Celestron INDI driver exposes `1x`, `2x`, ... `9x`; generic INDI exposes `SLEW_1`, `SLEW_2`. `jog._set_slew_rate` tries `<n>x` first, falls back to `SLEW_<n>`. If a future driver uses something else, expand the candidates list.
- **Goto refuses when the mount is not aligned.** Without a `mire sync`, `slew_to(ra, dec)` returns immediately with `success=False` and a "mount moved 0.000 deg of N deg requested" log. The fix is the alignment workflow: `mira orient` -> `mira jog` to center Polaris in the eyepiece -> `mira sync`. After that, `mira goto <target>` works. Direct INDI motion (`TELESCOPE_MOTION_NS` / `TELESCOPE_MOTION_WE`) bypasses goto entirely and works without alignment — that's what `jog.py` uses.
- **Indiserver socket can drop mid-session** ("BrokenPipeError: [Errno 32]"). Symptom: every operation errors with "failed to send to INDI." Recovery: `mira down && mira up` cycles indiserver and reconnects cleanly. The mount's physical pointing is preserved.
- **Plate-solving needs real stars in the frame.** Indoors against a wall: ASTAP returns "Not enough stars" and the capture pipeline can verify camera + ffmpeg + ASTAP install, but cannot exercise sync. Verify the full path on first clear night, not on dry-run.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/                  # full suite, must stay green (currently 152 tests)
python -m pytest tests/test_solver.py    # single file
python -m pytest -k "goto"               # by name pattern
```

Hardware smoke tests live in `scripts/`, are not part of `pytest`, and require a real telescope. Run them in this order before claiming the system works: `verify_setup.py`, `test_camera.py`, `test_mount_connect.py`, `test_solver.py`, `test_mount_slew.py`, `test_full_loop.py`. The last two prompt for `yes` before moving the mount.

## Architecture (one-liner)

Three layers feeding off one `ToolContext`: tool functions in `tools.py`, argparse CLI in `cli.py`, MCP server in `mcp_server.py`. Hardware modules (`mount.py` INDI, `camera.py` imagesnap, `iphone_camera.py` HTTP bridge, `solver.py` ASTAP) are isolated so each can be mocked in tests. When adding a tool: write it in `tools.py` with a long docstring (becomes the MCP description), wire it into the CLI, register it in `mcp_server.build_server`, append it to the `TOOLS` tuple.

## Smart capture pipeline (iPhone bridge + target-aware imaging)

Mira can use one of two camera backends, selected by `config.yaml`:

```yaml
camera:
  source: imagesnap     # legacy: Continuity Camera + imagesnap (auto-exposure only)
  # OR
  source: iphone_bridge # HTTP to the MiraCam iOS app -- true manual ISO/shutter
  iphone_url: "http://192.168.1.55:8080"  # or null to use Bonjour
```

The iPhone bridge talks to the **MiraCam iOS app** (`~/miracam-mobile`, https://github.com/andrewaws26/miracam-mobile, separate repo). MiraCam runs a tiny HTTP server on the iPhone itself, where AVCaptureDevice's manual exposure surface is unrestricted -- Continuity Camera locks it down on macOS. Bridge endpoints:
- `GET /preview.jpg` -- single JPEG capture (~140ms turnaround, ~7 fps burst rate)
- `POST /exposure {iso, duration_ms}` -- absolute manual exposure via custom Expo Module wrapping `AVCaptureDevice.setExposureModeCustomWithDuration`
- `POST /exposure {bias}` -- EV bias fallback via vision-camera v4
- `POST /focus {lens_position}` -- manual focus 0.0 (near) .. 1.0 (infinity)
- `POST /exposure/lock`, `POST /exposure/reset`
- `GET /capabilities` -- ISO range, shutter range, device flags, current state

`IphoneCamera` in `iphone_camera.py` is the Python client. It implements `.capture(filename=None)` so it's a drop-in for `camera.Camera`. Discovery via Bonjour (`_miracam._tcp`) or explicit URL.

### Capture pipeline modules

Built on top of the iPhone bridge:

- `imaging.py` -- pure-function image primitives: `luminance_stats` (mean / median / p1 / p99 / clip fractions), `count_stars` (adaptive threshold via MAD-derived sigma + connected components), `sharpness` (Laplacian variance for lucky-imaging frame ranking), `analyze()` for a one-pass report.

- `exposure_tuning.py` -- per-target preset table (`PRESETS` dict for moon / planet / cluster / nebula / galaxy / star / default) tuned for iPhone 16 Plus main camera afocal through a 130mm Newtonian. `tune_for_target(cam, category)` is the closed-loop tuner: capture -> analyze -> double / halve exposure -> repeat until mean luminance hits target or device caps out. Captures land in `/tmp/mira-tune-{N}.jpg` for inspection.

- `stacking.py` -- `capture_burst()` rapid-fires N captures, `align_to_reference()` (ECC translation/euclidean/affine) and `align_phase_correlation()` (FFT-based, faster), `stack_mean()` and `stack_sum_normalized()`. Two headline ops: `lucky_image(cam, n, keep_pct)` for planets (burst -> sharpness rank -> align top % -> mean stack), and `live_stack(cam, n, pause_s)` for deep-sky (capture intervals -> phase-correlate -> sum + renormalize for SNR gain).

- `moon_processing.py` -- single-frame `process_moon_frame()`: percentile-based histogram stretch + unsharp mask + gamma on the luminance channel only (preserves color cast via YCrCb).

- `target_type.py` -- `classify_with_confidence(name)` returns `(category, reason)`. Knows solar-system planets, named bright stars, M1-M110 with type table, DSO aliases via `ephemeris.DSO_ALIASES` (so "Pleiades" -> M45 -> cluster). Falls back to "default" rather than raising.

- `tools.smart_capture(target_name)` -- the orchestrator. Classifies target -> picks pipeline (moon / lucky / live / single) -> resets pipeline_state -> runs the right flow. Called automatically by `goto()` when `auto_capture=True` (the default).

### How the pieces fit

```
mira goto Saturn
  └─> classify_target("Saturn") -> ("planet", "lucky")
  └─> slew + plate-solve + sync (existing goto flow)
  └─> smart_capture("Saturn")
        ├─> reset pipeline_state
        ├─> tune_for_target(cam, "planet")  -- iterate ISO/shutter
        └─> lucky_image(cam, n=30)          -- burst + rank + stack
```

Pipeline -> CLI surface mapping:
- `mira goto X` -- full flow with auto_capture (slew + smart pipeline)
- `mira goto X --no-capture` -- just slew
- `mira capture --target X` -- smart pipeline without slewing
- `mira capture --target X --pipeline {lucky,live,moon,single}` -- override routing
- `mira capture --target X --n-frames 60` -- override default 30

Pipeline -> MCP tool mapping (Claude can introspect / decide):
- `classify_target(name)` -- returns category + reason + pipeline + preset, BEFORE acting
- `smart_capture(target_name, pipeline?, n_frames?, out_path?)` -- pipeline only, no slew
- `goto(target_name, auto_capture?, capture_out?)` -- slew + pipeline

### Live preview UI (`mira watch`)

`preview_server.py` is a stdlib `http.server` that serves a single dark-themed HTML page polling pipeline state + the latest frame from disk. Two-process design: the capture pipeline writes to `~/mira/captures/current/{state.json, frame.jpg, stack.jpg}` via `pipeline_state.py`; the watch server reads them. No shared memory; either side can crash without taking the other down.

```
mira watch                       # view only
mira watch --jog                 # view + arrow-key mount control
mira watch --port 9090           # different port
mira watch --iphone-url URL      # override config.camera.iphone_url
```

The page shows: live latest captured frame, in-progress stack rebuilt as frames are aligned (the "target emerges from noise" effect), iPhone live feed proxied through `/live.jpg`, current target / category / pipeline / phase / exposure / mean luminance / progress bar, and (with `--jog`) keyboard mount controls. Same URL works on the Mac OR the iPhone over WiFi -- one tool, both devices.

`pipeline_state.py` provides the IPC primitives:
- `state_dir()` -> `~/mira/captures/current/`
- `PipelineState` dataclass + `write_state()` (atomic file replace so the server never reads a half-written JSON) + `patch_state(**fields)` (read-modify-write)
- `publish_frame(src)` / `publish_stack(src)` (copy latest into watched location)
- `reset()` (clear at the top of a new capture session)

Jog endpoints over HTTP:
- `GET /jog/info` -> `{enabled: bool}` (only true when `mira watch --jog`)
- `POST /jog {direction: N|S|E|W, action: start|stop}` -- flips `TELESCOPE_MOTION_NS/WE` via the same `mount.client.set_switch` path the curses jog uses
- `POST /jog/rate {rate: 1..9}` -- uses `jog._set_slew_rate` for driver-specific naming
- `POST /jog/stop-all {}`

Browser JS wires `keydown`/`keyup` on arrow keys to these endpoints with hold-to-move semantics. Behavior is identical to the desktop `mira jog`; the surface is just web instead of curses, so you can drive the mount from an iPhone Safari tab while standing at the scope.

### Pipeline gotchas

- **The MiraCam iOS app is a separate repo and a hard prerequisite for `source: iphone_bridge`.** Without it the bridge returns connection errors. See `~/miracam-mobile/CLAUDE.md` for that side.
- **MiraCam HTTP parser needs Content-Length on POSTs.** Python urllib doesn't always set it automatically; `iphone_camera._post_json` sets it explicitly + has one retry on the 400 "must provide" error that surfaces when the iPhone parser dispatches before the body arrives in a second TCP segment.
- **iPhone backgrounds the app.** AVCaptureSession pauses, `cameraRef` clears, `/preview.jpg` starts returning 503. Recovery: bring MiraCam to foreground; the camera re-initializes. Long-running stacks may want a heartbeat that detects this and auto-aborts.
- **The "planet" preset is intentionally dark.** `target_mean_lum=12` because the use case is "tiny bright disk on black sky", not "indoor scene". Indoor smart_capture against `planet` will produce near-black frames -- that's correct.
- **Star-count short-circuit is per-preset.** Only sky-field targets (cluster / nebula / galaxy) use it; moon / planet / star do NOT, because indoor scenes return false-positive bright blobs that would trigger early convergence.
- **`expo prebuild --clean` wipes the React Native Core artifact** (`~/miracam-mobile/ios/Pods/ReactNativeCore-artifacts/reactnative-core-0.81.5-debug.tar.gz`, ~80MB). Re-downloading on a slow connection is painful. Either skip `--clean` when possible, or copy from another RN 0.81.5 project (`Viam-Staubli-Apera-PLC-Mobile-POC/mobile/ios/Pods/ReactNativeCore-artifacts/`) before running pod install.
- **Live preview frame.jpg is overwritten by each capture.** That's intentional (always shows latest), but if you want to inspect intermediate frames after a run, copy them out of `~/mira/captures/current/` before the next session starts.

## Audio creation pipeline (narration.py + sfx.py)

`narration.py` and `sfx.py` are the audio-creation half of Mira: they generate spoken pieces and sound effects via ElevenLabs APIs and save mp3s to `~/mira/captures/narrations/` and `~/mira/captures/sfx/`. They share `_post_json` from narration.py for ElevenLabs HTTP calls; sfx.py imports from narration.py at module load. To avoid circular imports, narration.py only imports sfx late (inside `compose()` when bookend SFX is requested).

- `narration.compose()` is the headline: TTS via `/v1/text-to-speech` (model locked to `eleven_v3` in `NARRATION_MODEL_ID`), music via `/v1/music`, optional bookend SFX via `/v1/sound-generation`, all mixed in one ffmpeg `filter_complex` pass. Returns a `CompositionResult` with the final path and intermediates.
- `sfx.generate()` is a thin wrapper around `/v1/sound-generation`. Caps duration to [0.5, 22] seconds per the API contract (`SFX_MIN_DURATION_S`, `SFX_MAX_DURATION_S`).
- Voice auto-tune (`tune_voice_settings`) reads v3 audio tags (`[warmly]`, `[softly]`, `[excited]`, etc.) from the script and adjusts stability/style toward the script's emotional energy. Baseline is performative, never documentary-flat: stability=0.35, style=0.70 at neutral. Caller's `voice_settings` layers on top of the tuned dict so an explicit `--stability 0.6` selectively pins one knob.
- The Music API rejects prompts that name copyrighted works (song titles, character names) and returns a `prompt_suggestion` in the error body. `_post_json` parses it and surfaces it in `CompositionError`. Caller can retry with the sanitized version.
- Music length is clamped to [10000, 300000] ms (`MUSIC_MIN_LENGTH_MS`, `MUSIC_MAX_LENGTH_MS`). Pieces longer than 5 minutes will run with the music tail silent for any overflow.
- For longer scripts, the synthesize_voice timeout defaults to 300s (we hit a 120s read timeout on a ~3:45 piece in May 2026). Don't drop it back without reason.
- ffmpeg + ffprobe become hard deps for narration / sfx. The `mira preview` subcommand was already optional-on-ffmpeg. Document this in any new feature that touches audio.
- **v3 prosody is per-call.** ElevenLabs explicitly documents that splitting text across separate TTS calls produces "abrupt changes in prosody from one chunk to another" (see their Request Stitching guide). The documented workaround (Request Stitching) is `eleven_v3`-incompatible per their own docs. So per-sentence synthesis trades prosody arc for fine-grained SFX timing. Mitigate via voice + script choice (see Voice selection rules below); don't try to fix it with stability/style knobs.
- **Music API is non-deterministic.** Same prompt, fresh call, different track. No seed parameter. If you regenerate to match a new length, you may lose a track the user liked. Recovery path: `compose()` keeps its music intermediate at `/var/folders/.../mira-narration-XXXXX/music.mp3` via `tempfile.mkdtemp(prefix="mira-narration-")`. Not auto-deleted. Recoverable for hours after the run. Cache the good ones into your stitcher build dir as soon as the user blesses them.

Multi-voice cinematic pieces (multiple speakers, inline SFX, music bed) AND single-narrator pieces with mid-piece SFX events are both out of scope for `compose()`. The pattern that worked: a one-off Python script in `mira-experiments/` that imports `synthesize_voice`, `synthesize_sfx`, `synthesize_music` directly, computes voice-line timestamps via ffprobe, and builds a single ffmpeg `filter_complex` with `adelay` per stream and `amix=normalize=0` plus `alimiter`. Cache intermediates so re-runs only re-synthesize what changed.
- `~/mira-experiments/build_alien_visit.py` — multi-voice Text-to-Dialogue cinematic.
- `~/mira-experiments/build_judy_birthday.py` — single-narrator stitcher with mid-piece SFX, layering ElevenLabs-generated SFX with Apple iLife/iMovie foley.

**Apple ships a usable foley library for free.** `/Applications/iMovie.app/Contents/Resources/iLife Sound Effects/` has ~420 categorized `.caf` clips (Ambience/Animals/Booms/Foley/People/Transportation/Textures); `/Applications/iMovie.app/Contents/Resources/iMovie Sound Effects/` adds ~95 more `.mp3` clips (Can Open, Bottle Pour, Crickets, City Night Crowd, etc.). ffmpeg ingests both formats directly. Useful when ElevenLabs SFX synthesis is too generic or eats credits unnecessarily (the build_judy_birthday Atlas launch + can pop + radio static + ocean ambience all came from here). Run `find "/Applications/iMovie.app/Contents/Resources/iLife Sound Effects" -iname "*<keyword>*"` to browse.

## Voice selection rules (compose_narration)

- Default to voices that support `eleven_v3`. v3 supports inline audio tags ([warmly], [softly], [whispers], [excited], [confidently]) and gives the most expressive delivery.
- Voices in the shared library list `verified_languages` per voice; presence of an `eleven_v3` entry is a strong signal of support, but NOT a hard constraint. Some shared voices that lack the v3 verification still synthesize fine on v3 (Joseph - Deep Southern Narrator and Grace - Honey-Smooth Southern Drawl, both used on v3 in the alien-visit cinematic). Pattern: try v3, fall back to `eleven_multilingual_v2` with audio tags stripped if v3 errors.
- Hard skip: voices with strong non-English accents on `eleven_multilingual_v2`. We tested Koro (Maori, NZ) on v2 in May 2026 and the user could not understand the narration. Strip the voice from the library when this happens so it doesn't get picked again.
- **Storyteller voices vs Narrator voices are different archetypes.** When the brief is "feels like she's telling a story" (not "reads written prose well"), filter the library on `search=storyteller` or descriptions mentioning "storyteller / grandmother / wise / intimate"; skip voices whose description says "narrator / narrative / narration / advertisements." Library categories matter more than stability/style for the read/tell distinction. Margot - Storyteller (`wnTuHlYPeBBHc2J3vMbA`) is a confirmed-good American storyteller. Diana - Friendly Narration (`LgkUduUnNELIv0NBU2rV`) is a confirmed-good narrator but reads like reading. Storyteller voice + storyteller-cadence script (discourse markers, repeated phrases, rhetorical questions, direct address, sentence fragments) is the combination that works; either alone is not enough.
- **Pin voice_settings after audition; do NOT use per-line auto-tune on hand-crafted pieces.** `tune_voice_settings` remaps stability/style based on the audio tags IN EACH CALL, so per-line synthesis with single tags collapses [softly]-only lines to stability=0.50/style=0.55 (documentary-flat) and jumps [excited]-only lines to 0.20/0.85. Same narrator, different voice character per sentence. Once the audition lands, pin one dict and apply to every line. Auto-tune is a default for one-shot scripts where the caller hasn't picked numbers; on iterated pieces it overrides a known-good answer.

## Commit pattern

- Push to `main` directly. No PR workflow for v0.1.x.
- Commit messages name the change in the title, explain the *why* in the body. Phase commits (`Phase 3: tool layer ...`) for batched work; targeted commits (`verify_setup: add /usr/local/opt/astap as d-series database lookup path`) for single-issue fixes.
- Co-author trailer is required: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Run `python -m pytest tests/` before every commit. If it does not stay 100% green, do not push.
