"""Configuration loading and validation for Mira.

Loads ~/mira/config.yaml into typed dataclasses, expands ~ in paths, and
validates required fields. Raises ConfigError on any problem with a message
that points at the offending key.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/mira/config.yaml").expanduser()


class ConfigError(ValueError):
    """Raised when config is missing, malformed, or invalid."""


@dataclass
class ObserverConfig:
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    timezone: str = "UTC"

    def validate(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ConfigError(f"observer.latitude must be in [-90, 90], got {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ConfigError(f"observer.longitude must be in [-180, 180], got {self.longitude}")


@dataclass
class MountConfig:
    driver: str = "celestron_nexstar"
    port: str = ""
    baud: int = 9600
    indi_host: str = "localhost"
    indi_port: int = 7624

    def validate(self) -> None:
        if not self.driver:
            raise ConfigError("mount.driver is required")
        if not self.port:
            raise ConfigError("mount.port is required (e.g. /dev/tty.usbserial-XXXX)")


@dataclass
class CameraConfig:
    device_name: str = "iPhone"
    capture_dir: Path = field(default_factory=lambda: Path("~/mira/captures").expanduser())
    warmup_seconds: float = 1.0

    def validate(self) -> None:
        if not self.device_name:
            raise ConfigError("camera.device_name is required")
        if self.warmup_seconds < 0:
            raise ConfigError("camera.warmup_seconds must be non-negative")


@dataclass
class SolverConfig:
    astap_path: Path = Path("/usr/local/bin/astap")
    star_db: str = "H18"
    estimated_fov_deg: float = 0.5
    timeout_seconds: int = 60

    def validate(self) -> None:
        if self.estimated_fov_deg <= 0 or self.estimated_fov_deg > 180:
            raise ConfigError(
                f"solver.estimated_fov_deg must be in (0, 180], got {self.estimated_fov_deg}"
            )
        if self.timeout_seconds <= 0:
            raise ConfigError("solver.timeout_seconds must be positive")


@dataclass
class EyepieceConfig:
    focal_length_mm: float = 25.0
    fov_arcmin: float = 30.0


@dataclass
class StorageConfig:
    state_db: Path = field(default_factory=lambda: Path("~/mira/state.db").expanduser())
    log_file: Path = field(default_factory=lambda: Path("~/mira/mira.log").expanduser())


@dataclass
class LoggingConfig:
    level: str = "INFO"

    def validate(self) -> None:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in valid:
            raise ConfigError(f"logging.level must be one of {sorted(valid)}, got {self.level!r}")


@dataclass
class Config:
    observer: ObserverConfig
    mount: MountConfig
    camera: CameraConfig
    solver: SolverConfig
    eyepiece: EyepieceConfig = field(default_factory=EyepieceConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> None:
        self.observer.validate()
        self.mount.validate()
        self.camera.validate()
        self.solver.validate()
        self.logging.validate()


def _expand_path(value: Any) -> Path:
    """Expand ~ and environment variables, return absolute Path."""
    if value is None:
        return Path()
    p = Path(os.path.expandvars(str(value))).expanduser()
    return p


def _require(d: dict, key: str, parent: str = "") -> Any:
    if key not in d:
        full = f"{parent}.{key}" if parent else key
        raise ConfigError(f"missing required key: {full}")
    return d[key]


def load_config(path: Path | str | None = None) -> Config:
    """Load config from YAML file. If path is None, use ~/mira/config.yaml."""
    cfg_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found at {cfg_path}. "
            "Copy config.example.yaml to ~/mira/config.yaml and edit."
        )

    try:
        with cfg_path.open("r") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config file {cfg_path} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"config file {cfg_path} must contain a YAML mapping at the top level")

    try:
        return _from_dict(raw)
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"failed to parse config: {e}") from e


def _from_dict(raw: dict) -> Config:
    obs_raw = _require(raw, "observer")
    if not isinstance(obs_raw, dict):
        raise ConfigError("observer must be a mapping")
    observer = ObserverConfig(
        latitude=float(_require(obs_raw, "latitude", "observer")),
        longitude=float(_require(obs_raw, "longitude", "observer")),
        elevation_m=float(obs_raw.get("elevation_m", 0.0)),
        timezone=str(obs_raw.get("timezone", "UTC")),
    )

    mount_raw = raw.get("mount", {}) or {}
    mount = MountConfig(
        driver=str(mount_raw.get("driver", "celestron_nexstar")),
        port=str(mount_raw.get("port", "")),
        baud=int(mount_raw.get("baud", 9600)),
        indi_host=str(mount_raw.get("indi_host", "localhost")),
        indi_port=int(mount_raw.get("indi_port", 7624)),
    )

    cam_raw = raw.get("camera", {}) or {}
    camera = CameraConfig(
        device_name=str(cam_raw.get("device_name", "iPhone")),
        capture_dir=_expand_path(cam_raw.get("capture_dir", "~/mira/captures")),
        warmup_seconds=float(cam_raw.get("warmup_seconds", 1.0)),
    )

    sol_raw = raw.get("solver", {}) or {}
    solver = SolverConfig(
        astap_path=_expand_path(sol_raw.get("astap_path", "/usr/local/bin/astap")),
        star_db=str(sol_raw.get("star_db", "H18")),
        estimated_fov_deg=float(sol_raw.get("estimated_fov_deg", 0.5)),
        timeout_seconds=int(sol_raw.get("timeout_seconds", 60)),
    )

    ep_raw = raw.get("eyepiece", {}) or {}
    eyepiece = EyepieceConfig(
        focal_length_mm=float(ep_raw.get("focal_length_mm", 25.0)),
        fov_arcmin=float(ep_raw.get("fov_arcmin", 30.0)),
    )

    st_raw = raw.get("storage", {}) or {}
    storage = StorageConfig(
        state_db=_expand_path(st_raw.get("state_db", "~/mira/state.db")),
        log_file=_expand_path(st_raw.get("log_file", "~/mira/mira.log")),
    )

    log_raw = raw.get("logging", {}) or {}
    log_cfg = LoggingConfig(level=str(log_raw.get("level", "INFO")))

    cfg = Config(
        observer=observer,
        mount=mount,
        camera=camera,
        solver=solver,
        eyepiece=eyepiece,
        storage=storage,
        logging=log_cfg,
    )
    cfg.validate()
    return cfg


def setup_logging(cfg: Config) -> None:
    """Configure root logger to write to console and the configured log file."""
    cfg.storage.log_file.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(cfg.storage.log_file),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
