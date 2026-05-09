"""Tests for mira.tools using mocked mount, camera, solver, and ephemeris."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mira.config import (
    CameraConfig,
    Config,
    EyepieceConfig,
    LoggingConfig,
    MountConfig,
    ObserverConfig,
    SolverConfig,
    StorageConfig,
)
from mira.ephemeris import NameNotFoundError, TargetCoords
from mira.solver import SolveFailed, SolveResult
from mira.state import StateDB
from mira.tools import (
    TOOLS,
    ToolContext,
    capture_frame,
    get_mount_position,
    get_observer_location,
    get_target_coordinates,
    goto,
    plate_solve,
    slew_to,
    sync_mount,
    wait_for_slew_complete,
)


@pytest.fixture
def fake_config(tmp_path: Path) -> Config:
    return Config(
        observer=ObserverConfig(latitude=38.25, longitude=-85.76, elevation_m=142),
        mount=MountConfig(port="/dev/null"),
        camera=CameraConfig(capture_dir=tmp_path / "captures"),
        solver=SolverConfig(astap_path=tmp_path / "fake-astap"),
        eyepiece=EyepieceConfig(),
        storage=StorageConfig(
            state_db=tmp_path / "state.db",
            log_file=tmp_path / "mira.log",
        ),
        logging=LoggingConfig(),
    )


@pytest.fixture
def state(fake_config: Config) -> StateDB:
    s = StateDB(fake_config.storage.state_db)
    s.init()
    return s


@pytest.fixture
def ctx(fake_config: Config, state: StateDB, tmp_path: Path) -> ToolContext:
    mount = MagicMock()
    mount.get_position.return_value = (100.0, 20.0)
    mount.sync.return_value = True
    mount.slew_to.return_value = True
    mount.wait_slew_complete.return_value = True

    camera = MagicMock()
    img = tmp_path / "captures" / "fake.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"fake")
    camera.capture.return_value = img

    solver = MagicMock()
    solver.solve.return_value = SolveResult(ra_deg=99.5, dec_deg=19.8)

    ephemeris = MagicMock()
    ephemeris.resolve.return_value = TargetCoords(
        name="Jupiter", ra_deg=120.0, dec_deg=15.0, kind="solar_system"
    )

    return ToolContext(
        config=fake_config,
        state=state,
        mount=mount,
        camera=camera,
        solver=solver,
        ephemeris=ephemeris,
    )


class TestGetTargetCoordinates:
    def test_resolves(self, ctx: ToolContext) -> None:
        ra, dec = get_target_coordinates("Jupiter", ctx=ctx)
        assert ra == 120.0
        assert dec == 15.0

    def test_unknown_raises(self, ctx: ToolContext) -> None:
        ctx.ephemeris.resolve.side_effect = NameNotFoundError("nope")  # type: ignore[attr-defined]
        with pytest.raises(NameNotFoundError):
            get_target_coordinates("Garbage", ctx=ctx)


class TestCaptureFrame:
    def test_returns_path(self, ctx: ToolContext) -> None:
        p = capture_frame(ctx=ctx)
        assert p.exists()
        ctx.camera.capture.assert_called_once()  # type: ignore[attr-defined]


class TestPlateSolve:
    def test_solve_success(self, ctx: ToolContext, tmp_path: Path) -> None:
        result = plate_solve(tmp_path / "fake.jpg", ctx=ctx)
        assert result == (99.5, 19.8)

    def test_solve_passes_hints(self, ctx: ToolContext, tmp_path: Path) -> None:
        plate_solve(tmp_path / "fake.jpg", ra_hint_deg=100.0, dec_hint_deg=20.0, ctx=ctx)
        kwargs = ctx.solver.solve.call_args.kwargs  # type: ignore[attr-defined]
        assert kwargs["ra_hint_deg"] == 100.0
        assert kwargs["dec_hint_deg"] == 20.0

    def test_solve_failure_returns_none(self, ctx: ToolContext, tmp_path: Path) -> None:
        ctx.solver.solve.side_effect = SolveFailed("too few stars")  # type: ignore[attr-defined]
        result = plate_solve(tmp_path / "fake.jpg", ctx=ctx)
        assert result is None


class TestSyncMount:
    def test_sync_records(self, ctx: ToolContext, state: StateDB) -> None:
        ok = sync_mount(279.5, 38.78, ctx=ctx)
        assert ok is True
        latest = state.latest_sync()
        assert latest is not None
        assert latest.ra_deg == pytest.approx(279.5)
        assert latest.dec_deg == pytest.approx(38.78)

    def test_sync_failure_does_not_record(self, ctx: ToolContext, state: StateDB) -> None:
        ctx.mount.sync.return_value = False  # type: ignore[attr-defined]
        ok = sync_mount(1.0, 2.0, ctx=ctx)
        assert ok is False
        assert state.latest_sync() is None


class TestSlewTo:
    def test_slew_records(self, ctx: ToolContext, state: StateDB) -> None:
        ok = slew_to(120.0, 15.0, ctx=ctx)
        assert ok is True
        latest = state.latest_slew()
        assert latest is not None
        assert latest.target_ra_deg == 120.0
        assert latest.success is True
        assert latest.achieved_ra_deg == pytest.approx(100.0)

    def test_slew_failure(self, ctx: ToolContext, state: StateDB) -> None:
        ctx.mount.slew_to.return_value = False  # type: ignore[attr-defined]
        ok = slew_to(1.0, 2.0, ctx=ctx)
        assert ok is False
        latest = state.latest_slew()
        assert latest is not None
        assert latest.success is False


class TestGetMountPosition:
    def test_returns_position(self, ctx: ToolContext) -> None:
        ra, dec = get_mount_position(ctx=ctx)
        assert ra == 100.0
        assert dec == 20.0


class TestWaitForSlewComplete:
    def test_calls_mount(self, ctx: ToolContext) -> None:
        ok = wait_for_slew_complete(timeout=10, ctx=ctx)
        assert ok is True
        ctx.mount.wait_slew_complete.assert_called_once()  # type: ignore[attr-defined]


class TestGetObserverLocation:
    def test_returns_lat_lon(self, ctx: ToolContext) -> None:
        lat, lon = get_observer_location(ctx=ctx)
        assert lat == 38.25
        assert lon == -85.76


class TestGoto:
    def test_full_flow(self, ctx: ToolContext, state: StateDB) -> None:
        ok = goto("Jupiter", ctx=ctx)
        assert ok is True
        # sync recorded with image path
        sync = state.latest_sync()
        assert sync is not None
        assert sync.image_path is not None
        # slew recorded with target name
        slew = state.latest_slew()
        assert slew is not None
        assert slew.target_name == "Jupiter"
        assert slew.success is True

    def test_unknown_target(self, ctx: ToolContext) -> None:
        ctx.ephemeris.resolve.side_effect = NameNotFoundError("not found")  # type: ignore[attr-defined]
        ok = goto("Whatever", ctx=ctx)
        assert ok is False
        ctx.mount.slew_to.assert_not_called()  # type: ignore[attr-defined]

    def test_solve_failure(self, ctx: ToolContext) -> None:
        ctx.solver.solve.side_effect = SolveFailed("nope")  # type: ignore[attr-defined]
        ok = goto("Jupiter", ctx=ctx)
        assert ok is False
        ctx.mount.sync.assert_not_called()  # type: ignore[attr-defined]
        ctx.mount.slew_to.assert_not_called()  # type: ignore[attr-defined]

    def test_capture_failure(self, ctx: ToolContext) -> None:
        from mira.camera import CameraError

        ctx.camera.capture.side_effect = CameraError("no camera")  # type: ignore[attr-defined]
        ok = goto("Jupiter", ctx=ctx)
        assert ok is False
        ctx.mount.slew_to.assert_not_called()  # type: ignore[attr-defined]


class TestModuleSurface:
    def test_tools_listing_complete(self) -> None:
        names = {fn.__name__ for fn in TOOLS}
        assert "get_target_coordinates" in names
        assert "capture_frame" in names
        assert "plate_solve" in names
        assert "sync_mount" in names
        assert "slew_to" in names
        assert "get_mount_position" in names
        assert "wait_for_slew_complete" in names
        assert "get_observer_location" in names
        assert "goto" in names

    def test_all_tools_have_docstrings(self) -> None:
        for fn in TOOLS:
            assert fn.__doc__ is not None and len(fn.__doc__) > 50, (
                f"{fn.__name__} needs a longer docstring (MCP description)"
            )

    def test_all_tools_have_type_hints(self) -> None:
        import inspect

        for fn in TOOLS:
            sig = inspect.signature(fn)
            assert sig.return_annotation is not inspect.Signature.empty, (
                f"{fn.__name__} is missing a return type annotation"
            )
            for pname, param in sig.parameters.items():
                if pname.startswith("_") or pname == "ctx":
                    continue
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                assert param.annotation is not inspect.Parameter.empty, (
                    f"{fn.__name__}.{pname} is missing a type annotation"
                )
