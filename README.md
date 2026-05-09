# Mira

Conversational telescope control for the Celestron NexStar 130SLT.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)]()
[![Platform: macOS](https://img.shields.io/badge/Platform-macOS-lightgrey.svg)]()

## What Mira is

Mira is a system that lets you point a Celestron NexStar 130SLT at any patch of sky, say something like "Mira, show me Jupiter," and have the telescope plate-solve the current pointing then slew to the target. No traditional star alignment is required. The bad alignment you do at startup gets overwritten the moment your first image solves.

Mira is named for the Spanish word "look" and the variable star Mira in Cetus. The bilingual name fits the household it was built in.

The core idea: an iPhone afocally mounted over the eyepiece is a wide-enough field that ASTAP can plate-solve the captured frame, then we sync the mount to the solved coordinates and slew anywhere we want.

The system runs entirely on the Mac. There is no cloud component. There is no LLM in the data path. Claude Code talks to a local MCP server that calls Python tool functions on the same machine.

## Hardware

- MacBook Air (Apple Silicon, macOS 14 or newer).
- Celestron NexStar 130SLT telescope on the standard SLT alt-az mount.
- Celestron NexStar+ hand controller.
- FTDI USB-to-RJ12 serial cable to connect the hand controller to the Mac. Off-the-shelf USB-to-RJ12 cables work; the genuine Celestron PC cable also works.
- iPhone 16 Plus (or any modern iPhone supporting Continuity Camera) on the same Apple ID as the Mac.
- Celestron NexYZ DX adapter to mount the iPhone afocally over the eyepiece.
- 25mm Plossl eyepiece. Approximately 30 arcminutes true field with the 130SLT.

Other Celestron NexStar mounts that present the same hand-controller serial protocol should work; the INDI driver name in the config is the only piece that may need adjustment.

## Software prerequisites

- macOS 14 (Sonoma) or newer.
- Python 3.11 or newer.
- Homebrew. https://brew.sh
- INDI (libindi + Celestron NexStar driver). On macOS this comes via Homebrew.
- ASTAP for plate solving. https://www.hnsky.org/astap.htm
- ASTAP star database (H17 or H18). One-time download, several gigabytes.
- imagesnap for camera capture. https://github.com/rharder/imagesnap

## Installation

These steps are ordered. Run the verification command after each step before moving on.

### 1. Clone the repo

```bash
git clone https://github.com/andrewaws26/mira.git
cd mira
```

Verify:

```bash
ls README.md pyproject.toml src/mira
```

### 2. Install imagesnap (Homebrew)

```bash
brew install imagesnap
```

imagesnap is the only one of Mira's three external tools that is in Homebrew core. ASTAP and INDI need direct downloads or a source build, see steps 2a and 2b.

Verify:

```bash
imagesnap -l
```

This lists video devices. If the iPhone is paired and Continuity Camera is enabled, you will see "iPhone" in the list.

### 2a. Install ASTAP

ASTAP is not in Homebrew. Download the macOS installer from SourceForge and run it:

```bash
curl -L -o /tmp/astap.pkg "https://sourceforge.net/projects/astap-program/files/macOS%20installer/astap.pkg/download"
sudo installer -pkg /tmp/astap.pkg -target /
```

The installer puts ASTAP at `/Applications/ASTAP.app`, which exposes the CLI at `/Applications/ASTAP.app/Contents/MacOS/astap`. The default `~/mira/config.yaml` already points at that path.

Verify:

```bash
/Applications/ASTAP.app/Contents/MacOS/astap -h | head -5
```

### 2b. Install INDI (build from source)

INDI is not in Homebrew core and the upstream repo does not publish macOS binaries, so we build from source. This takes about 20 minutes on Apple Silicon.

```bash
brew install cmake libnova zlib gphoto2 libusb cfitsio fftw curl theora libev pkg-config gsl
git clone --depth 1 https://github.com/indilib/indi.git ~/src/indi
mkdir -p ~/src/indi/build && cd ~/src/indi/build
cmake -DCMAKE_INSTALL_PREFIX=/opt/homebrew \
      -DINDI_BUILD_SERVER=ON \
      -DINDI_BUILD_DRIVERS=ON \
      ..
make -j$(sysctl -n hw.ncpu)
sudo make install
```

INDI's Celestron NexStar driver lives at `/opt/homebrew/bin/indi_celestron_nexstar_telescope` after install.

Verify:

```bash
indiserver -h
which indi_celestron_nexstar_telescope
```

If you would rather not build INDI yourself, the alternative is INDIGO Server (https://www.indigo-astronomy.org). INDIGO ships a macOS .pkg but uses a different wire protocol; Mira's `mount.py` would need to swap from INDI XML to INDIGO. Stick with INDI for now.

### 3. Download the ASTAP star database

ASTAP's solver needs a star catalog on disk. Recent ASTAP uses `d05`, `d20`, `d50`, and `d80` databases (covering progressively dimmer stars). For the iPhone afocal pipeline at ~0.5 degree FOV, `d20` (434 MB compressed) is the right balance. `d50` (936 MB) is overkill but fine if you have the disk. `d05` (137 MB) is too sparse for reliable solves at this FOV.

```bash
curl -L -o /tmp/d20.pkg "https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.pkg/download"
sudo installer -pkg /tmp/d20.pkg -target /
```

The default `~/mira/config.yaml` is set to `star_db: d50`. If you installed `d20` instead, change `solver.star_db` to `d20`.

Verify by listing the ASTAP application support directory:

```bash
ls ~/Library/Application\ Support/astap/ 2>/dev/null
ls /Applications/ASTAP.app/Contents/MacOS 2>/dev/null
```

### 4. Set up a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

Verify:

```bash
python --version
```

This must report Python 3.11 or newer.

### 5. Install Mira

```bash
pip install -e ".[mcp,dev]"
```

The `mcp` extra installs the MCP SDK; the `dev` extra installs pytest and ruff.

Verify:

```bash
mira --version
mira --help
python -m pytest tests/
```

The unit suite should report 125 passing tests.

### 6. Configure Continuity Camera

On the Mac, open System Settings, then General, then AirPlay & Continuity. Make sure "Continuity Camera" is on. On the iPhone, open Settings, then General, then AirPlay & Continuity, and confirm the same.

Bring the iPhone within Bluetooth range of the Mac. Unlock it. Run `imagesnap -l` again. The iPhone must appear by name in the device list.

### 7. Find your serial port

Plug the FTDI cable into a USB port on the Mac and into the bottom of the NexStar+ hand controller. Power on the mount.

```bash
ls /dev/tty.usbserial-*
```

Note the exact path. The suffix is unique per cable.

### 8. Create your config

```bash
mkdir -p ~/mira
cp config.example.yaml ~/mira/config.yaml
$EDITOR ~/mira/config.yaml
```

Edit at minimum:

- `observer.latitude` and `observer.longitude` for your location
- `mount.port` to the path you found in step 7
- `solver.astap_path` if ASTAP is not at `/usr/local/bin/astap`. On Apple Silicon Homebrew it is usually at `/opt/homebrew/bin/astap`.

Verify the config loads:

```bash
mira --config ~/mira/config.yaml status
```

This will show observer info and a "mount: NOT connected" line. That is correct; the mount becomes reachable only after step 9.

### 9. Start the INDI server

In a dedicated terminal window, start the INDI server with the Celestron driver:

```bash
indiserver -v indi_celestron_nexstar_telescope
```

Leave this running for the entire observing session. Open a second terminal for everything else.

Verify in the second terminal:

```bash
nc -z localhost 7624 && echo "INDI reachable"
```

## First-time setup

### Pair the iPhone

Done in step 6 above. Tested with `imagesnap -l`.

### Configure INDI

The INDI driver is `indi_celestron_nexstar_telescope`. The mount appears as device "Celestron NexStar". Mira's config defaults to that name. If your INDI build uses a different driver name, run `indiserver -v <other_driver>` and adjust accordingly. The protocol is identical.

### Find the right serial port

Done in step 7. If multiple USB-serial devices are connected, the suffix tells you which is which: unplug the FTDI cable, run `ls /dev/tty.usbserial-*`, plug it back in, and run again. The new entry is the one you want.

## Verification workflow

After installation, run the full prerequisite check:

```bash
python scripts/verify_setup.py
```

You should see PASS lines for Python, macOS, imagesnap, ASTAP, the star database, your config file, the state database, and the INDI server. Any FAIL line includes a Fix line that tells you what to do.

Then run the smoke tests, in order, before your first observing session:

```bash
# 1. Camera. Captures a still frame from the iPhone.
python scripts/test_camera.py

# 2. Mount connection. No movement.
python scripts/test_mount_connect.py

# 3. ASTAP solve. Pass any starfield image you have, or use the fixture.
python scripts/test_solver.py path/to/starfield.jpg

# 4. Mount slew. MOVES THE TELESCOPE 1 degree, then back.
python scripts/test_mount_slew.py

# 5. Full pipeline. MOVES THE TELESCOPE. Capture, solve, sync, slew, verify.
python scripts/test_full_loop.py
```

Steps 4 and 5 prompt for confirmation before moving the OTA. Make sure the tube is clear of obstructions before answering "yes".

## Pre-session checklist

Run through this every observing session:

1. Power on the mount. Battery or AC; the SLT brain needs ~12V.
2. Use the hand controller to do a deliberately bad alignment. "Solar System Align" pointed at any bright object will do. The 130SLT refuses Goto/sync commands until it thinks it has been aligned, so this fake alignment is just to flip that flag. Mira's first sync will overwrite whatever data this step recorded.
3. Connect the FTDI cable to the hand controller and the Mac.
4. Mount the iPhone in the NexYZ DX. Center a bright star in the eyepiece, focus the eyepiece, then center the iPhone camera over the eyepiece via the NexYZ adjusters until the same star is centered in the iPhone's camera preview.
5. Start the INDI server in its own terminal: `indiserver -v indi_celestron_nexstar_telescope`.
6. In another terminal, activate the venv: `source .venv/bin/activate`.
7. Confirm the camera and mount are alive: `python scripts/test_camera.py && python scripts/test_mount_connect.py`.
8. You are ready to observe. Use the CLI or talk to Claude Code.

## Usage

### CLI

```bash
# Resolve a target name without moving the mount.
mira resolve Jupiter

# Capture a frame and save it.
mira capture --output /tmp/test.jpg

# Solve a saved image.
mira solve /tmp/test.jpg

# Read the mount's current position.
mira where

# Capture, solve, and sync (no slew).
mira sync

# The headline operation: capture, solve, sync, slew to a named target.
mira goto Jupiter
mira goto M31
mira goto Vega
mira goto "Orion Nebula"

# Show mount status, last sync, last slew.
mira status
```

Run `mira <command> --help` for per-subcommand options.

### Claude Code via MCP

Register Mira as an MCP server in your Claude Code settings. Open Claude Code's settings file (typically `~/.claude/settings.json` or per-project `.claude/mcp.json`) and add:

```json
{
  "mcpServers": {
    "mira": {
      "command": "/path/to/mira/.venv/bin/mira-mcp"
    }
  }
}
```

Restart Claude Code. You can now ask things like:

- "Mira, where is the telescope pointed right now?"
- "Mira, show me Jupiter."
- "Mira, take a frame and tell me what stars are in it."
- "Mira, point at M31 then move 30 arcminutes east."

Claude picks the right tool from the descriptions Mira ships in the MCP schema.

## Architecture

Three layers:

1. **Tool functions** (`mira/tools.py`): standalone Python functions that do the actual work. Nine of them: `get_target_coordinates`, `capture_frame`, `plate_solve`, `sync_mount`, `slew_to`, `get_mount_position`, `wait_for_slew_complete`, `get_observer_location`, and `goto`. Every one has type hints and docstrings, and is unit-tested with mocked hardware.

2. **CLI** (`mira/cli.py`): an argparse wrapper around the tool layer. Subcommands `goto`, `sync`, `where`, `capture`, `solve`, `status`, `devices`, `resolve`. Designed to work without an LLM, including offline.

3. **MCP server** (`mira/mcp_server.py`): exposes the same tool functions over the Model Context Protocol so Claude Code can call them. Uses the official MCP Python SDK with FastMCP. Each tool's docstring becomes the MCP description; type hints become the JSON schema.

Supporting modules:

- `config.py`: typed YAML loader for `~/mira/config.yaml`.
- `state.py`: SQLite database tracking sync history, slew history, and sessions.
- `ephemeris.py`: Skyfield wrapper that resolves names (planets, M1 to M110, named stars, common DSO aliases) to apparent RA/Dec at the configured observer location.
- `mount.py`: pure-Python INDI XML wire-protocol client. Reader thread parses incoming property updates; main thread sends commands. Avoids the C extension build pain of pyindi-client on Apple Silicon. Wraps INDI's Celestron driver, which is the supported way to talk to NexStar mounts.
- `camera.py`: imagesnap subprocess wrapper. Continuity Camera shows up as a standard AVFoundation device.
- `solver.py`: ASTAP subprocess wrapper. Parses both .wcs (FITS-card) and .ini ASTAP outputs.

Coordinate convention everywhere in the public API: RA in degrees [0, 360), Dec in degrees [-90, 90], "apparent of date" position from the topocentric observer (precession, nutation, and aberration accounted for). The mount expects of-date coordinates; using J2000 catalog values directly produces a consistent pointing offset that looks like a calibration bug but is actually a math bug.

## Troubleshooting

### Continuity Camera not appearing

Run `imagesnap -l`. If the iPhone is not in the list:

- Unlock the iPhone.
- Bring it within ~30 ft of the Mac.
- Confirm both devices are signed into the same Apple ID.
- Confirm Continuity Camera is enabled in System Settings on both devices.
- Toggle Bluetooth off and on.
- If it still does not appear, restart the iPhone. Continuity Camera initialization can wedge.

### Serial port permission issues

```
PermissionError: [Errno 13] Permission denied: '/dev/tty.usbserial-AB0K3LX2'
```

This is rare on macOS but can happen if some other process has the port open. List processes holding the port:

```bash
lsof /dev/tty.usbserial-AB0K3LX2
```

Kill the offender, or close the application that holds it.

### ASTAP solve failures

ASTAP failing with "too few stars" or "no solution" is usually one of:

- FOV mismatch. Tune `solver.estimated_fov_deg` in config. The iPhone over a 25mm Plossl in the 130SLT is roughly 0.5 degrees, but afocal alignment shifts this. Try 0.3 to 0.8 degrees.
- Image too dim. Run `mira capture` and inspect the JPEG. If you see fewer than ~15 stars, increase the iPhone exposure (manual mode in the camera app, or use Halide / Adobe Lightroom for finer control).
- Star database too coarse. H17 covers down to magnitude 17; H18 to magnitude 18. Use H18 if the field is sparse.
- Wrong RA/Dec hint. If you pass `--ra` / `--dec` hints far from the actual pointing, ASTAP gives up. Try without hints first.

### Mount not accepting sync commands

The 130SLT firmware refuses sync, slew, or goto until it believes it has been aligned. If the first command after powering on fails with an Alert state, do the fake alignment via the hand controller (any align mode pointed at anything) and retry. Mira will overwrite the bad alignment with a real one on the first sync.

### "INDI server not running"

Start it explicitly in a terminal:

```bash
indiserver -v indi_celestron_nexstar_telescope
```

If you get "command not found", finish step 2b (build INDI from source). The Celestron NexStar driver is part of `INDI_BUILD_DRIVERS=ON` in that build.

### Plate solves succeed but slew lands far from the target

This is almost always a coordinate-frame bug. Mira computes apparent of-date coordinates and sends them to INDI's `EQUATORIAL_EOD_COORD` property, which is the right pairing. If you have modified the code to use J2000 coordinates instead, you will see a consistent ~0.5 degree offset for all targets that grows over decades.

### MCP server starts but Claude Code does not see Mira's tools

- Confirm the `command` path in the Claude Code MCP config points at the venv's `mira-mcp` binary, not a system Python.
- Restart Claude Code completely after editing the MCP config.
- Run `mira-mcp` manually and observe stderr for any Python tracebacks at startup.

## The fake alignment quirk

Celestron's NexStar firmware has a hard interlock: the mount refuses Goto, Sync, and slew commands until it thinks it has been aligned. This is by design; an unaligned alt-az mount has no idea where it is or what direction is up.

For Mira's workflow, the alignment is not actually needed. The first plate solve plus sync gives the mount a perfect alignment. But the firmware does not know that yet, so we have to lie to it.

The fake alignment ritual:

1. Power on the mount.
2. Use the hand controller to start "Solar System Align" or "One Star Align".
3. When the controller asks you to center an alignment object, point at anything bright. Press Enter to "confirm".
4. The hand controller now flips its internal "aligned" flag. Goto commands will work.

The pointing accuracy from this alignment is intentionally bad; it does not matter. Mira's first sync will overwrite the alignment with the plate-solved truth.

## License

MIT. See [LICENSE](LICENSE).
