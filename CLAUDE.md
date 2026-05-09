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
- Building INDI from source on macOS: pass `-DCMAKE_INSTALL_RPATH=/opt/homebrew/lib -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON` to cmake, otherwise installed binaries fail at runtime with `Library not loaded ... no LC_RPATH's found`. GSL is a required brew dep; cmake fails without it.
- Continuity Camera shows up under the iPhone's user-set name (e.g. "Andrew Camera"), NOT the literal string "iPhone". `imagesnap` on this build prints `Video Devices:` as the header (with colon, no "found"). `camera.list_devices` filters on `lower("video device")` prefix to handle both variants and strips `=> ` and `* ` line prefixes.
- Spoken output goes through `src/mira/speech.py`, which calls ElevenLabs over HTTPS and pipes the MP3 to macOS `afplay`. The API key is read from `os.environ["ELEVENLABS_API_KEY"]` first, then `~/mira/.env`. Never put the key in `config.yaml`. Default voice is Sarah (EXAVITQu4vr4xnSDxMaL); change `speech.voice_id` in config or use `mira voices` to browse.
- The `say` tool in `tools.py` is best-effort: speech failures (network, quota, rate-limit) are logged but never raise, so a TTS hiccup cannot block an observation.
- `mira preview` shells out to `ffplay` from ffmpeg. ffmpeg is an optional dep, only needed for that one subcommand. Device name resolution is a case-insensitive substring match against `ffmpeg -f avfoundation -list_devices true` output, indexed by AVFoundation index, not name (ffplay's `-i` argument).
- ASTAP `d05`, `d20`, `d50`, `d80` star DBs install to `/usr/local/opt/astap/`, not the obvious places. The `verify_setup.py` candidate list includes that path. Old ASTAP docs reference H17/H18; those are gone.
- Repo root and runtime root are the same directory (`~/mira/`). `.gitignore` covers `config.yaml`, `state.db`, `mira.log`, `captures/`, the Skyfield ephemeris cache, and ASTAP DB files, so personal config stays out of git. Be careful adding new untracked files at the repo root.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/                  # full suite, must stay green (currently 126 tests)
python -m pytest tests/test_solver.py    # single file
python -m pytest -k "goto"               # by name pattern
```

Hardware smoke tests live in `scripts/`, are not part of `pytest`, and require a real telescope. Run them in this order before claiming the system works: `verify_setup.py`, `test_camera.py`, `test_mount_connect.py`, `test_solver.py`, `test_mount_slew.py`, `test_full_loop.py`. The last two prompt for `yes` before moving the mount.

## Architecture (one-liner)

Three layers feeding off one `ToolContext`: tool functions in `tools.py`, argparse CLI in `cli.py`, MCP server in `mcp_server.py`. Hardware modules (`mount.py` INDI, `camera.py` imagesnap, `solver.py` ASTAP) are isolated so each can be mocked in tests. When adding a tool: write it in `tools.py` with a long docstring (becomes the MCP description), wire it into the CLI, register it in `mcp_server.build_server`, append it to the `TOOLS` tuple.

## Commit pattern

- Push to `main` directly. No PR workflow for v0.1.x.
- Commit messages name the change in the title, explain the *why* in the body. Phase commits (`Phase 3: tool layer ...`) for batched work; targeted commits (`verify_setup: add /usr/local/opt/astap as d-series database lookup path`) for single-issue fixes.
- Co-author trailer is required: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Run `python -m pytest tests/` before every commit. If it does not stay 100% green, do not push.
