"""Tests for mira.config: loading, validation, defaults, ~ expansion."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mira.config import (
    CameraConfig,
    ConfigError,
    LoggingConfig,
    MountConfig,
    ObserverConfig,
    SolverConfig,
    load_config,
)


@pytest.fixture
def minimal_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "observer": {"latitude": 38.25, "longitude": -85.76},
                "mount": {"port": "/dev/tty.usbserial-AB0K3LX2"},
            }
        )
    )
    return p


@pytest.fixture
def full_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "observer": {
                    "latitude": 38.25,
                    "longitude": -85.76,
                    "elevation_m": 142,
                    "timezone": "America/Kentucky/Louisville",
                },
                "mount": {
                    "driver": "celestron_nexstar",
                    "port": "/dev/tty.usbserial-AB0K3LX2",
                    "baud": 9600,
                    "indi_host": "localhost",
                    "indi_port": 7624,
                },
                "camera": {
                    "device_name": "iPhone",
                    "capture_dir": "~/mira/captures",
                    "warmup_seconds": 1.5,
                },
                "solver": {
                    "astap_path": "/opt/homebrew/bin/astap",
                    "star_db": "H17",
                    "estimated_fov_deg": 0.5,
                    "timeout_seconds": 90,
                },
                "eyepiece": {"focal_length_mm": 25, "fov_arcmin": 30},
                "storage": {
                    "state_db": "~/mira/state.db",
                    "log_file": "~/mira/mira.log",
                },
                "logging": {"level": "DEBUG"},
            }
        )
    )
    return p


class TestLoad:
    def test_minimal_config_loads(self, minimal_yaml: Path) -> None:
        cfg = load_config(minimal_yaml)
        assert cfg.observer.latitude == 38.25
        assert cfg.observer.longitude == -85.76
        assert cfg.mount.port == "/dev/tty.usbserial-AB0K3LX2"

    def test_full_config_loads(self, full_yaml: Path) -> None:
        cfg = load_config(full_yaml)
        assert cfg.observer.elevation_m == 142
        assert cfg.solver.star_db == "H17"
        assert cfg.solver.timeout_seconds == 90
        assert cfg.logging.level == "DEBUG"

    def test_defaults_applied(self, minimal_yaml: Path) -> None:
        cfg = load_config(minimal_yaml)
        assert cfg.observer.elevation_m == 0.0
        assert cfg.observer.timezone == "UTC"
        assert cfg.mount.baud == 9600
        assert cfg.camera.device_name == "iPhone"
        assert cfg.solver.estimated_fov_deg == 0.5
        assert cfg.eyepiece.focal_length_mm == 25.0
        assert cfg.logging.level == "INFO"

    def test_path_expansion(self, full_yaml: Path) -> None:
        cfg = load_config(full_yaml)
        assert "~" not in str(cfg.camera.capture_dir)
        assert "~" not in str(cfg.storage.state_db)
        assert str(cfg.camera.capture_dir).startswith("/")

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("observer: {latitude: 38.25\n  bad: indent")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_config(p)

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("- 1\n- 2")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(p)

    def test_missing_observer(self, tmp_path: Path) -> None:
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump({"mount": {"port": "/dev/null"}}))
        with pytest.raises(ConfigError, match="observer"):
            load_config(p)

    def test_missing_observer_lat(self, tmp_path: Path) -> None:
        p = tmp_path / "c.yaml"
        p.write_text(
            yaml.safe_dump(
                {"observer": {"longitude": -85.0}, "mount": {"port": "/dev/null"}}
            )
        )
        with pytest.raises(ConfigError, match="latitude"):
            load_config(p)


class TestValidation:
    def test_latitude_out_of_range(self) -> None:
        cfg = ObserverConfig(latitude=91.0, longitude=0.0)
        with pytest.raises(ConfigError, match="latitude"):
            cfg.validate()

    def test_longitude_out_of_range(self) -> None:
        cfg = ObserverConfig(latitude=0.0, longitude=181.0)
        with pytest.raises(ConfigError, match="longitude"):
            cfg.validate()

    def test_negative_latitude_ok(self) -> None:
        ObserverConfig(latitude=-33.0, longitude=151.0).validate()

    def test_mount_requires_port(self) -> None:
        with pytest.raises(ConfigError, match="port"):
            MountConfig(port="").validate()

    def test_mount_requires_driver(self) -> None:
        with pytest.raises(ConfigError, match="driver"):
            MountConfig(driver="", port="/dev/null").validate()

    def test_camera_requires_device(self) -> None:
        with pytest.raises(ConfigError, match="device_name"):
            CameraConfig(device_name="").validate()

    def test_solver_fov_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="estimated_fov_deg"):
            SolverConfig(estimated_fov_deg=0).validate()

    def test_solver_fov_too_large(self) -> None:
        with pytest.raises(ConfigError, match="estimated_fov_deg"):
            SolverConfig(estimated_fov_deg=400).validate()

    def test_solver_timeout_positive(self) -> None:
        with pytest.raises(ConfigError, match="timeout_seconds"):
            SolverConfig(timeout_seconds=0).validate()

    def test_logging_level_validates(self) -> None:
        with pytest.raises(ConfigError, match="level"):
            LoggingConfig(level="VERBOSE").validate()

    def test_logging_level_case_insensitive(self) -> None:
        LoggingConfig(level="debug").validate()
        LoggingConfig(level="WARNING").validate()


class TestRoundTrip:
    def test_full_validate(self, full_yaml: Path) -> None:
        cfg = load_config(full_yaml)
        cfg.validate()  # must not raise

    def test_invalid_lat_in_yaml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "c.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "observer": {"latitude": 95, "longitude": 0},
                    "mount": {"port": "/dev/null"},
                }
            )
        )
        with pytest.raises(ConfigError, match="latitude"):
            load_config(p)
