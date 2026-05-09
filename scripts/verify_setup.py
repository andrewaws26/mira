#!/usr/bin/env python3
"""End-to-end prerequisite check for Mira.

Runs every check we can without needing the telescope to be powered on.
Prints PASS/FAIL per check with a fix line on every failure. Exits 0 if
all checks pass, non-zero otherwise.

Run this once after install, then again any time something is acting up.
"""

from __future__ import annotations

import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# allow running as `python scripts/verify_setup.py` from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.config import ConfigError, load_config  # noqa: E402

PASS_MARK = "[PASS]"
FAIL_MARK = "[FAIL]"
WARN_MARK = "[WARN]"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    fix: str = ""
    warning: bool = False  # treat as soft fail; counts but doesn't gate

    def line(self) -> str:
        mark = WARN_MARK if self.warning else (PASS_MARK if self.passed else FAIL_MARK)
        body = f"{mark} {self.name}"
        if self.detail:
            body += f": {self.detail}"
        if not self.passed and self.fix:
            body += f"\n    Fix: {self.fix}"
        return body


# ----- individual checks -----


def check_python_version() -> CheckResult:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return CheckResult(
        name=f"Python {major}.{minor}",
        passed=ok,
        detail=platform.python_implementation(),
        fix="Install Python 3.11+ via Homebrew: brew install python@3.13",
    )


def check_macos() -> CheckResult:
    is_macos = platform.system() == "Darwin"
    return CheckResult(
        name="macOS",
        passed=is_macos,
        detail=platform.platform() if is_macos else f"running on {platform.system()}",
        fix="Mira is macOS-only. Continuity Camera is an Apple feature.",
        warning=not is_macos,
    )


def check_imagesnap() -> CheckResult:
    binary = shutil.which("imagesnap")
    if binary is None:
        return CheckResult(
            name="imagesnap",
            passed=False,
            fix="brew install imagesnap",
        )
    return CheckResult(name="imagesnap", passed=True, detail=binary)


def check_camera_visible(device_name: str) -> CheckResult:
    if shutil.which("imagesnap") is None:
        return CheckResult(
            name=f"camera '{device_name}' visible",
            passed=False,
            detail="imagesnap missing",
            fix="brew install imagesnap, then re-run this check",
        )
    try:
        from mira.camera import list_devices

        devices = list_devices()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name=f"camera '{device_name}' visible",
            passed=False,
            detail=str(e),
            fix="Restart imagesnap or re-pair Continuity Camera",
        )
    found = any(device_name.lower() in d.lower() for d in devices)
    detail = f"available: {devices}" if devices else "no devices reported"
    return CheckResult(
        name=f"camera '{device_name}' visible",
        passed=found,
        detail=detail,
        fix=(
            "Unlock the iPhone, ensure it is near the Mac, signed into the same "
            "Apple ID, and that Continuity Camera is enabled in Settings -> "
            "General -> AirPlay & Continuity."
        ),
    )


def check_astap(astap_path: Path) -> CheckResult:
    p = Path(astap_path).expanduser()
    if p.exists():
        return CheckResult(name="ASTAP binary", passed=True, detail=str(p))
    found = shutil.which(p.name)
    if found:
        return CheckResult(
            name="ASTAP binary",
            passed=True,
            detail=f"found on PATH at {found} (config points to {p})",
            warning=True,
        )
    return CheckResult(
        name="ASTAP binary",
        passed=False,
        detail=f"not at {p}",
        fix=(
            "Download from https://www.hnsky.org/astap.htm (macOS .pkg under "
            "'macOS installer'), then `sudo installer -pkg astap.pkg -target /`. "
            "Update solver.astap_path in config.yaml if your install path differs."
        ),
    )


def check_astap_star_db(astap_path: Path, db_name: str) -> CheckResult:
    """ASTAP installs the binary alongside or near the star database. We look
    in common spots and report the first match. Failure is a warning because
    the user may store the DB elsewhere."""
    p = Path(astap_path).expanduser()
    candidates = [
        p.parent,
        Path("/Applications/ASTAP.app/Contents/MacOS"),
        Path("~/Library/Application Support/astap").expanduser(),
        Path.home(),
        Path("/usr/local/share/astap"),
        Path("/opt/homebrew/share/astap"),
    ]
    suffix = db_name.lower()
    for d in candidates:
        if not d.exists():
            continue
        for child in d.iterdir():
            if child.is_dir() and child.name.lower().startswith(suffix):
                return CheckResult(
                    name=f"ASTAP star database {db_name}",
                    passed=True,
                    detail=str(child),
                )
            if child.is_file() and suffix in child.name.lower():
                return CheckResult(
                    name=f"ASTAP star database {db_name}",
                    passed=True,
                    detail=str(child),
                )
    return CheckResult(
        name=f"ASTAP star database {db_name}",
        passed=False,
        detail="not found in common locations",
        fix=(
            "Download d20_star_database.pkg (or d50, d80) from "
            "https://sourceforge.net/projects/astap-program/files/star_databases/ "
            "and `sudo installer -pkg <pkg> -target /`."
        ),
        warning=True,
    )


def check_serial_port(port_path: str) -> CheckResult:
    p = Path(port_path)
    if not p.exists():
        ports = sorted(Path("/dev").glob("tty.usbserial*"))
        ports += sorted(Path("/dev").glob("tty.usbmodem*"))
        ports += sorted(Path("/dev").glob("cu.usbserial*"))
        avail = ", ".join(str(x) for x in ports) if ports else "none"
        return CheckResult(
            name=f"serial port {port_path}",
            passed=False,
            detail=f"available: {avail}",
            fix=(
                "Plug in the FTDI cable, run `ls /dev/tty.usbserial-*` to see the "
                "exact suffix, and update mount.port in config.yaml"
            ),
        )
    return CheckResult(name=f"serial port {port_path}", passed=True, detail="present")


def check_indi_server(host: str, port: int) -> CheckResult:
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (ConnectionRefusedError, socket.gaierror, socket.timeout, OSError):
        return CheckResult(
            name=f"INDI server at {host}:{port}",
            passed=False,
            detail="not reachable",
            fix="In another terminal: indiserver -v indi_celestron_nexstar_telescope",
        )
    return CheckResult(name=f"INDI server at {host}:{port}", passed=True, detail="reachable")


def check_skyfield_ephemeris() -> CheckResult:
    cache = Path("~/mira/ephemeris").expanduser()
    de421 = cache / "de421.bsp"
    if de421.exists():
        return CheckResult(
            name="Skyfield de421.bsp",
            passed=True,
            detail=f"{de421.stat().st_size:,} bytes",
        )
    return CheckResult(
        name="Skyfield de421.bsp",
        passed=False,
        detail="not yet downloaded",
        fix="Will be downloaded automatically on first use. Internet required for first run.",
        warning=True,
    )


def check_config_file() -> CheckResult:
    try:
        cfg = load_config()
    except ConfigError as e:
        return CheckResult(
            name="config file",
            passed=False,
            detail=str(e),
            fix="cp config.example.yaml ~/mira/config.yaml; edit observer + mount.port",
        )
    return CheckResult(
        name="config file",
        passed=True,
        detail=f"observer ({cfg.observer.latitude:.3f}, {cfg.observer.longitude:.3f})",
    )


def check_state_db_init(state_db_path: Path) -> CheckResult:
    try:
        from mira.state import StateDB

        s = StateDB(state_db_path)
        s.init()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="state database",
            passed=False,
            detail=str(e),
            fix="Ensure the parent directory is writable",
        )
    return CheckResult(name="state database", passed=True, detail=str(state_db_path))


# ----- driver -----


def run_all() -> int:
    checks: list[Callable[[], CheckResult] | CheckResult] = []
    checks.append(check_python_version())
    checks.append(check_macos())
    checks.append(check_imagesnap())

    # Config check happens early so we can use values from it.
    cfg_result = check_config_file()
    print(cfg_result.line())
    cfg = None
    if cfg_result.passed:
        try:
            cfg = load_config()
        except ConfigError:
            cfg = None

    if cfg is None:
        # Don't try downstream checks that depend on config.
        for c in checks:
            if isinstance(c, CheckResult):
                print(c.line())
        print("\nResult: config file invalid; fix and re-run")
        return 2

    results: list[CheckResult] = [c if isinstance(c, CheckResult) else c() for c in checks]
    results.append(check_camera_visible(cfg.camera.device_name))
    results.append(check_astap(cfg.solver.astap_path))
    results.append(check_astap_star_db(cfg.solver.astap_path, cfg.solver.star_db))
    results.append(check_serial_port(cfg.mount.port))
    results.append(check_indi_server(cfg.mount.indi_host, cfg.mount.indi_port))
    results.append(check_skyfield_ephemeris())
    results.append(check_state_db_init(cfg.storage.state_db))

    for r in results:
        print(r.line())

    hard_fails = [r for r in results if not r.passed and not r.warning]
    soft_fails = [r for r in results if not r.passed and r.warning]
    print(
        f"\nResult: {len(results) - len(hard_fails) - len(soft_fails)} passed, "
        f"{len(soft_fails)} warnings, {len(hard_fails)} failed."
    )
    if hard_fails:
        print("Fix the FAIL items above before first session.")
    return 0 if not hard_fails else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
