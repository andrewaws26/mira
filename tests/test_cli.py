"""Tests for mira.cli: argument parsing and clean error reporting."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from mira.cli import (
    COMMANDS,
    _format_radec,
    _format_radec_sexagesimal,
    build_parser,
    run,
)


class TestParser:
    def test_help_runs(self, capsys: pytest.CaptureFixture) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        captured = capsys.readouterr()
        for cmd in ["goto", "sync", "where", "capture", "solve", "status"]:
            assert cmd in captured.out

    def test_version(self, capsys: pytest.CaptureFixture) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--version"])
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert "mira " in captured.out

    def test_goto_requires_target(self, capsys: pytest.CaptureFixture) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["goto"])
        captured = capsys.readouterr()
        assert "target" in captured.err.lower()

    def test_goto_with_target(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["goto", "Jupiter"])
        assert args.cmd == "goto"
        assert args.target == "Jupiter"

    def test_solve_requires_image(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["solve"])

    def test_solve_with_hints(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["solve", "/tmp/x.jpg", "--ra", "100.5", "--dec", "20.5"]
        )
        assert args.image == Path("/tmp/x.jpg")
        assert args.ra == 100.5
        assert args.dec == 20.5
        assert args.fov is None  # default

    def test_solve_with_fov(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["solve", "/tmp/x.jpg", "--fov", "1.5"])
        assert args.fov == 1.5

    def test_capture_optional_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["capture", "--output", "/tmp/out.jpg"])
        assert args.output == Path("/tmp/out.jpg")

    def test_capture_default_no_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["capture"])
        assert args.output is None

    def test_resolve_with_target(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["resolve", "Vega"])
        assert args.cmd == "resolve"
        assert args.target == "Vega"

    def test_global_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--config", "/tmp/c.yaml", "status"])
        assert args.config == Path("/tmp/c.yaml")

    def test_global_verbose(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-v", "where"])
        assert args.verbose is True

    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture) -> None:
        rc = run([])
        captured = capsys.readouterr()
        assert rc != 0
        assert "usage" in captured.out.lower() or "usage" in captured.err.lower()


class TestFormatters:
    def test_format_decimal(self) -> None:
        s = _format_radec(123.4567, -5.4321)
        assert "RA=123.4567" in s
        assert "Dec=-5.4321" in s

    def test_format_sexagesimal(self) -> None:
        # Vega: RA ~279.234 deg = 18h36m56s, Dec ~38.78 deg
        s = _format_radec_sexagesimal(279.234735, 38.783689)
        assert s.startswith("18h")
        assert "+38d" in s

    def test_negative_dec(self) -> None:
        s = _format_radec_sexagesimal(0.0, -45.5)
        assert "-45d" in s

    def test_ra_wraps(self) -> None:
        s = _format_radec_sexagesimal(360.0, 0.0)
        assert s.startswith("00h")


class TestCleanErrors:
    def test_missing_config_clean_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = run(["--config", str(tmp_path / "nope.yaml"), "where"])
        captured = capsys.readouterr()
        # Should be a clean error string, not a Python traceback.
        assert rc != 0
        assert "Traceback" not in captured.err
        assert "config" in captured.err.lower()

    def test_solve_missing_image_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # We need a valid config for the solve subcommand to even get to
        # the solver-not-found path. Synthesize one.
        import yaml

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "observer": {"latitude": 38.0, "longitude": -85.0},
                    "mount": {"port": "/dev/null"},
                    "solver": {"astap_path": str(tmp_path / "fake-astap")},
                }
            )
        )
        rc = run(["--config", str(cfg), "solve", str(tmp_path / "no.jpg")])
        captured = capsys.readouterr()
        assert rc != 0
        assert "Traceback" not in captured.err


class TestCommandsTable:
    def test_all_subcommands_have_handlers(self) -> None:
        expected = {"goto", "sync", "where", "capture", "solve", "status", "devices", "resolve"}
        assert set(COMMANDS.keys()) == expected

    def test_handlers_callable(self) -> None:
        for fn in COMMANDS.values():
            assert callable(fn)

    def test_each_subparser_has_help(self) -> None:
        parser = build_parser()
        subparsers_action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        for name, sp in subparsers_action.choices.items():
            assert sp.description, f"subcommand {name!r} missing description"
