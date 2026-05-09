"""Tests for mira.mcp_server: server construction, tool registration, schemas."""

from __future__ import annotations

import asyncio
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
from mira.ephemeris import TargetCoords
from mira.solver import SolveResult
from mira.state import StateDB
from mira import tools as tool_layer
from mira.tools import ToolContext


@pytest.fixture
def installed_ctx(tmp_path: Path) -> ToolContext:
    """Create a ToolContext backed by mocks and install it as the default."""
    cfg = Config(
        observer=ObserverConfig(latitude=38.25, longitude=-85.76),
        mount=MountConfig(port="/dev/null"),
        camera=CameraConfig(capture_dir=tmp_path / "cap"),
        solver=SolverConfig(astap_path=tmp_path / "fake-astap"),
        eyepiece=EyepieceConfig(),
        storage=StorageConfig(state_db=tmp_path / "s.db", log_file=tmp_path / "m.log"),
        logging=LoggingConfig(),
    )
    state = StateDB(cfg.storage.state_db)
    state.init()
    mount = MagicMock()
    mount.get_position.return_value = (123.0, 45.0)
    mount.sync.return_value = True
    mount.slew_to.return_value = True
    mount.wait_slew_complete.return_value = True

    camera = MagicMock()
    img = tmp_path / "cap" / "x.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"x")
    camera.capture.return_value = img

    solver = MagicMock()
    solver.solve.return_value = SolveResult(ra_deg=120.5, dec_deg=44.5)

    ephemeris = MagicMock()
    ephemeris.resolve.return_value = TargetCoords(
        name="Vega", ra_deg=279.5, dec_deg=38.78, kind="star"
    )

    ctx = ToolContext(
        config=cfg,
        state=state,
        mount=mount,
        camera=camera,
        solver=solver,
        ephemeris=ephemeris,
    )
    tool_layer.set_default_context(ctx)
    yield ctx
    tool_layer.set_default_context(None)


class TestServerConstruction:
    def test_build_server(self) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        assert server.name == "mira"

    def test_lists_all_tools(self) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        expected = {
            "get_target_coordinates",
            "capture_frame",
            "plate_solve",
            "sync_mount",
            "slew_to",
            "get_mount_position",
            "wait_for_slew_complete",
            "get_observer_location",
            "goto",
            "list_known_targets",
        }
        assert expected.issubset(names)

    def test_every_tool_has_description(self) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        tools = asyncio.run(server.list_tools())
        for t in tools:
            assert t.description is not None and len(t.description) > 50, (
                f"tool {t.name!r} description is too short: {t.description!r}"
            )

    def test_every_tool_has_valid_input_schema(self) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        tools = asyncio.run(server.list_tools())
        for t in tools:
            schema = t.inputSchema
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            assert "properties" in schema


class TestToolInvocation:
    def test_get_target_coordinates(self, installed_ctx: ToolContext) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        result = asyncio.run(server.call_tool("get_target_coordinates", {"name": "Vega"}))
        # FastMCP returns (content, structured_content) or list[Content]; check structured.
        # call_tool returns a tuple in newer versions; structured payload is what we care about.
        if isinstance(result, tuple):
            _, structured = result
        else:
            structured = result
        assert structured["ra_deg"] == 279.5
        assert structured["dec_deg"] == 38.78

    def test_get_observer_location(self, installed_ctx: ToolContext) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        result = asyncio.run(server.call_tool("get_observer_location", {}))
        if isinstance(result, tuple):
            _, structured = result
        else:
            structured = result
        assert structured["latitude_deg"] == 38.25
        assert structured["longitude_deg"] == -85.76

    def test_get_mount_position(self, installed_ctx: ToolContext) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        result = asyncio.run(server.call_tool("get_mount_position", {}))
        if isinstance(result, tuple):
            _, structured = result
        else:
            structured = result
        assert structured["ra_deg"] == 123.0
        assert structured["dec_deg"] == 45.0

    def test_goto(self, installed_ctx: ToolContext) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        result = asyncio.run(server.call_tool("goto", {"target_name": "Vega"}))
        if isinstance(result, tuple):
            _, structured = result
        else:
            structured = result
        assert structured["result"] is True or structured is True

    def test_list_known_targets(self, installed_ctx: ToolContext) -> None:
        from mira.mcp_server import build_server

        server = build_server()
        result = asyncio.run(server.call_tool("list_known_targets", {}))
        if isinstance(result, tuple):
            _, structured = result
        else:
            structured = result
        # structured may be {"result": {...}} or the dict directly
        payload = structured.get("result", structured) if isinstance(structured, dict) else structured
        assert "messier" in payload
        assert "named_stars" in payload
        assert "M31" in payload["messier"]
