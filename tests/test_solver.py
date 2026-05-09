"""Tests for mira.solver: WCS/INI parsing, solve-result construction, error paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.solver import (
    SolveFailed,
    SolveResult,
    SolverError,
    SolverNotFoundError,
    parse_solve_result,
    parse_wcs,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseWcs:
    def test_parses_fits_card_style(self) -> None:
        text = (FIXTURES / "sample_solve.wcs").read_text()
        d = parse_wcs(text)
        assert d["PLTSOLVD"] == "T"
        assert d["CRVAL1"] == "279.234734916000"
        assert d["CRVAL2"] == "38.783689167000"
        assert d["CTYPE1"] == "RA---TAN"

    def test_parses_ini_style(self) -> None:
        text = (FIXTURES / "sample_solve.ini").read_text()
        d = parse_wcs(text)
        assert d["PLTSOLVD"] == "T"
        assert d["CRVAL1"] == "10.6847"
        assert d["CRVAL2"] == "41.2691"

    def test_strips_quotes(self) -> None:
        text = "CTYPE1  = 'RA---TAN'\nKEY     = \"value\""
        d = parse_wcs(text)
        assert d["CTYPE1"] == "RA---TAN"
        assert d["KEY"] == "value"

    def test_skips_end_and_comments(self) -> None:
        text = "# a comment\n; another\nFOO     = 1\nEND\n"
        d = parse_wcs(text)
        assert d == {"FOO": "1"}

    def test_handles_negative_numbers(self) -> None:
        text = "CDELT1  =          -0.000123\n"
        d = parse_wcs(text)
        assert d["CDELT1"] == "-0.000123"


class TestParseSolveResult:
    def test_success_from_wcs(self) -> None:
        text = (FIXTURES / "sample_solve.wcs").read_text()
        result = parse_solve_result(text)
        assert isinstance(result, SolveResult)
        assert result.ra_deg == pytest.approx(279.234734916, abs=1e-6)
        assert result.dec_deg == pytest.approx(38.783689167, abs=1e-6)
        assert result.pixel_scale_arcsec is not None
        assert result.pixel_scale_arcsec == pytest.approx(0.4444, rel=0.01)
        assert result.fov_x_deg == pytest.approx(0.4977, rel=0.01)
        assert result.rotation_deg == pytest.approx(0.5)

    def test_success_from_ini(self) -> None:
        text = (FIXTURES / "sample_solve.ini").read_text()
        result = parse_solve_result(text)
        assert result.ra_deg == pytest.approx(10.6847, abs=1e-4)
        assert result.dec_deg == pytest.approx(41.2691, abs=1e-4)
        assert result.fov_x_deg == pytest.approx(0.4977, rel=0.01)

    def test_failure_raises(self) -> None:
        text = (FIXTURES / "failed_solve.ini").read_text()
        with pytest.raises(SolveFailed, match="Too few stars"):
            parse_solve_result(text)

    def test_missing_pltsolvd_raises(self) -> None:
        with pytest.raises(SolveFailed):
            parse_solve_result("CRVAL1 = 100.0\nCRVAL2 = 20.0\n")

    def test_missing_crval_raises(self) -> None:
        with pytest.raises(SolveFailed, match="CRVAL"):
            parse_solve_result("PLTSOLVD = T\n")


class TestSolverConstruction:
    def test_construction_does_not_validate_binary(self, tmp_path: Path) -> None:
        """Constructing a Solver with a missing binary must NOT raise.

        Validation happens on first solve(). This lets ToolContext build
        on machines where ASTAP is not yet installed, so commands that
        do not need the solver (resolve, where, status) keep working.
        """
        from mira.solver import Solver

        # Should not raise.
        Solver(astap_path=tmp_path / "nope-astap")

    def test_missing_binary_raises_on_solve(self, tmp_path: Path) -> None:
        from mira.solver import Solver

        # Need a real image so we get past the image-exists check first.
        img = tmp_path / "img.jpg"
        img.write_bytes(b"\xff\xd8\xff")  # not a real JPEG but file exists
        s = Solver(astap_path=tmp_path / "nope-astap")
        with pytest.raises(SolverNotFoundError):
            s.solve(img)

    def test_solve_missing_image(self, tmp_path: Path) -> None:
        from mira.solver import Solver

        fake_bin = tmp_path / "astap"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)

        s = Solver(astap_path=fake_bin)
        with pytest.raises(SolverError, match="image file not found"):
            s.solve(tmp_path / "nope.jpg")
