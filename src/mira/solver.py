"""Plate solving via ASTAP.

Wraps the ASTAP CLI as a subprocess and parses its WCS output. ASTAP writes
a `<image>.wcs` text file (FITS-keyword style) with the solution; we read
CRVAL1 (RA) and CRVAL2 (Dec) in degrees and the PLTSOLVD flag.

Hint inputs (estimated FOV, RA/Dec near pointing) drastically reduce solve
time. The mount's reported position is the right hint when available.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SolverError(RuntimeError):
    """Raised on any plate-solving failure."""


class SolverNotFoundError(SolverError):
    """Raised when the ASTAP binary cannot be located."""


class SolveFailed(SolverError):
    """Raised when ASTAP runs but does not produce a solution."""


@dataclass(frozen=True)
class SolveResult:
    """Plate solution summary. RA/Dec are in degrees."""

    ra_deg: float
    dec_deg: float
    pixel_scale_arcsec: Optional[float] = None
    rotation_deg: Optional[float] = None
    fov_x_deg: Optional[float] = None
    fov_y_deg: Optional[float] = None
    image_path: Optional[Path] = None
    wcs_path: Optional[Path] = None


def _resolve_binary(astap_path: Path | str) -> Path:
    p = Path(astap_path).expanduser()
    if p.exists() and p.is_file():
        return p
    found = shutil.which(str(p))
    if found:
        return Path(found)
    raise SolverNotFoundError(
        f"ASTAP not found at {astap_path}. "
        "Download the macOS .pkg from https://www.hnsky.org/astap.htm "
        "and `sudo installer -pkg astap.pkg -target /`."
    )


# A FITS-keyword line is 8-char keyword, '=', value, optional '/' comment.
# ASTAP's WCS output uses one keyword per line, ASCII only, no card padding.
_WCS_LINE_RE = re.compile(
    r"^\s*([A-Z0-9_\-]{1,8})\s*=\s*([^/]+?)(?:\s*/\s*.*)?$"
)


def parse_wcs(text: str) -> dict[str, str]:
    """Parse an ASTAP-style WCS text block into a key -> raw-value-string dict.

    Accepts FITS-card-style (KEY = VALUE / comment) and INI-style (KEY=VALUE)
    lines. Handles both .wcs and .ini outputs ASTAP can produce.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        # skip END card and pure-comment lines
        stripped = line.strip()
        if not stripped or stripped.upper() == "END":
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        m = _WCS_LINE_RE.match(line)
        if not m:
            # try INI fallback (no value-comment separator)
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip().upper()
                v = v.strip().strip("'\"")
                if k:
                    out[k] = v
            continue
        key = m.group(1).upper()
        val = m.group(2).strip().strip("'\"")
        out[key] = val
    return out


def _to_float(d: dict[str, str], key: str) -> Optional[float]:
    raw = d.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_solve_result(text: str, image_path: Path | None = None, wcs_path: Path | None = None) -> SolveResult:
    """Build a SolveResult from raw ASTAP WCS/INI text. Raises SolveFailed if not solved."""
    fields = parse_wcs(text)
    solved = fields.get("PLTSOLVD", "F").upper()
    if solved not in ("T", "TRUE", "1"):
        warn = fields.get("WARNING") or fields.get("ERROR") or "PLTSOLVD not true"
        raise SolveFailed(f"ASTAP did not solve: {warn}")

    ra = _to_float(fields, "CRVAL1")
    dec = _to_float(fields, "CRVAL2")
    if ra is None or dec is None:
        raise SolveFailed("solve output missing CRVAL1/CRVAL2")

    pix_scale = None
    cdelt2 = _to_float(fields, "CDELT2")
    if cdelt2 is not None:
        pix_scale = abs(cdelt2) * 3600.0  # deg/pixel -> arcsec/pixel
    rotation = _to_float(fields, "CROTA2")

    fov_x = fov_y = None
    naxis1 = _to_float(fields, "NAXIS1")
    naxis2 = _to_float(fields, "NAXIS2")
    cdelt1 = _to_float(fields, "CDELT1")
    if naxis1 is not None and cdelt1 is not None:
        fov_x = abs(naxis1 * cdelt1)
    if naxis2 is not None and cdelt2 is not None:
        fov_y = abs(naxis2 * cdelt2)

    return SolveResult(
        ra_deg=ra,
        dec_deg=dec,
        pixel_scale_arcsec=pix_scale,
        rotation_deg=rotation,
        fov_x_deg=fov_x,
        fov_y_deg=fov_y,
        image_path=image_path,
        wcs_path=wcs_path,
    )


class Solver:
    """Wrapper around the ASTAP CLI.

    The ASTAP binary is not validated at construction time so that the
    rest of Mira continues to work when ASTAP is not yet installed. The
    binary is resolved on first call to `solve`, which raises
    SolverNotFoundError if it cannot be found.
    """

    def __init__(
        self,
        astap_path: Path | str = "/usr/local/bin/astap",
        estimated_fov_deg: float = 0.5,
        timeout_seconds: int = 60,
        star_db: str = "d50",
    ) -> None:
        self.astap_path = Path(astap_path).expanduser()
        self.estimated_fov_deg = estimated_fov_deg
        self.timeout_seconds = timeout_seconds
        self.star_db = star_db
        self._resolved_binary: Optional[Path] = None

    def _binary(self) -> Path:
        if self._resolved_binary is None:
            self._resolved_binary = _resolve_binary(self.astap_path)
        return self._resolved_binary

    def solve(
        self,
        image_path: Path | str,
        ra_hint_deg: Optional[float] = None,
        dec_hint_deg: Optional[float] = None,
        fov_deg: Optional[float] = None,
    ) -> SolveResult:
        """Plate-solve an image. Returns RA/Dec in degrees on success.

        Args:
            image_path: path to image file (jpg/png/fits/etc).
            ra_hint_deg, dec_hint_deg: known approximate pointing in degrees.
                Big speedup when present. Both must be passed together.
            fov_deg: optional FOV override; defaults to the configured estimate.

        Raises:
            SolverError if ASTAP fails to launch.
            SolveFailed if ASTAP runs but cannot find a solution.
        """
        img = Path(image_path).expanduser().resolve()
        if not img.exists():
            raise SolverError(f"image file not found: {img}")

        binary = self._binary()
        fov = fov_deg if fov_deg is not None else self.estimated_fov_deg
        cmd: list[str] = [str(binary), "-f", str(img), "-wcs", "-fov", f"{fov:.4f}"]
        if ra_hint_deg is not None and dec_hint_deg is not None:
            ra_hours = ra_hint_deg / 15.0
            spd = dec_hint_deg + 90.0
            cmd += ["-ra", f"{ra_hours:.6f}", "-spd", f"{spd:.6f}"]

        logger.debug("astap cmd: %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise SolveFailed(
                f"ASTAP timed out after {self.timeout_seconds}s on {img.name}"
            ) from e
        except FileNotFoundError as e:
            raise SolverNotFoundError(f"ASTAP binary not executable: {binary}") from e

        wcs_path = img.with_suffix(".wcs")
        ini_path = img.with_suffix(".ini")

        text = ""
        if wcs_path.exists():
            text = wcs_path.read_text()
        elif ini_path.exists():
            text = ini_path.read_text()
        else:
            raise SolveFailed(
                f"ASTAP exited {result.returncode} with no .wcs/.ini output. "
                f"stderr: {result.stderr.strip()[:400]}"
            )

        return parse_solve_result(text, image_path=img, wcs_path=wcs_path if wcs_path.exists() else ini_path)
