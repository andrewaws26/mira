#!/usr/bin/env python3
"""Smoke test: run ASTAP against an image and report the solution.

Pass an image path, or use the default fixture if available.

PASS criteria: ASTAP returns a solution with finite RA/Dec.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mira.config import ConfigError, load_config  # noqa: E402
from mira.solver import (  # noqa: E402
    SolveFailed,
    Solver,
    SolverError,
    SolverNotFoundError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ASTAP on an image")
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        help="path to image file (jpg/png/fits). "
        "If omitted, uses tests/fixtures/sample_starfield.jpg.",
    )
    parser.add_argument("--ra", type=float, help="hint: approximate RA in degrees")
    parser.add_argument("--dec", type=float, help="hint: approximate Dec in degrees")
    args = parser.parse_args()

    print("[*] loading config")
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2

    image = args.image
    if image is None:
        repo_root = Path(__file__).parent.parent
        image = repo_root / "tests" / "fixtures" / "sample_starfield.jpg"
        if not image.exists():
            print(
                f"[SKIP] no image specified and {image} does not exist. "
                "Drop a starfield JPG there or pass one as the first argument."
            )
            return 0

    print(f"[*] solving {image}")
    try:
        solver = Solver(
            astap_path=cfg.solver.astap_path,
            estimated_fov_deg=cfg.solver.estimated_fov_deg,
            timeout_seconds=cfg.solver.timeout_seconds,
            star_db=cfg.solver.star_db,
        )
    except SolverNotFoundError as e:
        print(f"[FAIL] {e}")
        return 1
    try:
        result = solver.solve(image, ra_hint_deg=args.ra, dec_hint_deg=args.dec)
    except SolveFailed as e:
        print(f"[FAIL] {e}")
        return 1
    except SolverError as e:
        print(f"[FAIL] {e}")
        return 1

    print(
        f"[PASS] solved at RA={result.ra_deg:.4f} deg, "
        f"Dec={result.dec_deg:.4f} deg, "
        f"FOV={result.fov_x_deg:.3f} x {result.fov_y_deg:.3f} deg"
        if (result.fov_x_deg and result.fov_y_deg)
        else f"[PASS] solved at RA={result.ra_deg:.4f} deg, Dec={result.dec_deg:.4f} deg"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
