"""iPhone camera bridge over HTTP.

Talks to the MiraCam iOS app (~/miracam-mobile,
https://github.com/andrewaws26/miracam-mobile) which runs an HTTP server
on the iPhone exposing manual ISO / shutter / focus and JPEG capture.

Discovery:
    - Bonjour: looks for `_miracam._tcp` on the LAN
    - Or explicit: pass base_url to __init__()

Interface (drop-in replacement for camera.Camera where it overlaps):
    capture(out_path)          -> Path     [Camera-compatible]
    last_capture()             -> Path
    set_manual_exposure(iso, duration_ms)
    set_exposure_bias(bias)
    lock_exposure()
    reset_exposure()
    set_manual_focus(lens_position)        # 0.0 = near, 1.0 = infinity
    reset_focus()
    get_capabilities()        -> dict
    get_health()              -> dict

This replaces imagesnap for capture, AND adds manual control that
Continuity Camera can't deliver. Enable via config.yaml:
    camera:
      source: iphone_bridge
      iphone_url: "http://192.168.1.55:8080"   # or null to use Bonjour
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Optional

logger = logging.getLogger(__name__)


class IphoneCameraError(RuntimeError):
    """Base class for iPhone bridge failures."""


class IphoneCameraNotReady(IphoneCameraError):
    """The HTTP server is reachable but the camera isn't initialized yet."""


class IphoneCameraTimeout(IphoneCameraError):
    """Network timeout reaching the iPhone."""


class IphoneCameraNotFound(IphoneCameraError):
    """Bonjour discovery returned no MiraCam instance."""


@dataclass
class IphoneCameraConfig:
    """Configuration. Either pass a fixed base_url or rely on Bonjour discovery."""

    base_url: Optional[str] = None
    """e.g. 'http://192.168.1.55:8080'. None -> discover via Bonjour."""

    discovery_timeout_s: float = 5.0
    """How long to listen for Bonjour announcements before giving up."""

    request_timeout_s: float = 10.0
    """Default per-request HTTP timeout. Photo capture uses 30s."""

    capture_timeout_s: float = 30.0


class IphoneCamera:
    """HTTP client for the MiraCam iOS app."""

    def __init__(self, config: IphoneCameraConfig) -> None:
        self.config = config
        self._base_url: Optional[str] = config.base_url
        self._last_capture: Optional[Path] = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, *, timeout_s: float = 5.0) -> "IphoneCamera":
        """Find a MiraCam instance on the LAN via Bonjour (mDNS)."""
        url = _bonjour_discover_miracam(timeout_s=timeout_s)
        if url is None:
            raise IphoneCameraNotFound(
                "no MiraCam instance found on the LAN within "
                f"{timeout_s}s. Make sure the MiraCam app is running on "
                "the iPhone, both devices are on the same WiFi network, "
                "and the iPhone screen is awake."
            )
        return cls(IphoneCameraConfig(base_url=url))

    def base_url(self) -> str:
        if self._base_url is None:
            url = _bonjour_discover_miracam(timeout_s=self.config.discovery_timeout_s)
            if url is None:
                raise IphoneCameraNotFound(
                    "Bonjour discovery failed and no explicit base_url set"
                )
            self._base_url = url
        return self._base_url

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture(self, out_path: Path | str) -> Path:
        """Capture a JPEG, write to out_path, return the Path.

        Camera-compatible interface so this can drop in for camera.Camera.
        """
        out_path = Path(out_path).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        jpeg_bytes = self._get("/preview.jpg", timeout=self.config.capture_timeout_s, raw=True)
        out_path.write_bytes(jpeg_bytes)
        self._last_capture = out_path
        logger.info("iphone capture -> %s (%d bytes)", out_path, len(jpeg_bytes))
        return out_path

    def last_capture(self) -> Optional[Path]:
        return self._last_capture

    # ------------------------------------------------------------------
    # Manual exposure (via the native MiracamExposure module)
    # ------------------------------------------------------------------

    def set_manual_exposure(self, iso: float, duration_ms: float) -> dict[str, Any]:
        """Set absolute ISO + shutter duration. iPhone clamps to device limits."""
        return self._post_json("/exposure", {"iso": iso, "duration_ms": duration_ms})

    def set_exposure_bias(self, bias: float) -> dict[str, Any]:
        """Set EV bias. Returns the camera to continuous auto-exposure mode."""
        return self._post_json("/exposure", {"bias": bias})

    def lock_exposure(self) -> dict[str, Any]:
        """Freeze exposure at current auto-metered values."""
        return self._post_json("/exposure/lock", {})

    def reset_exposure(self) -> dict[str, Any]:
        """Return to continuous auto-exposure with bias 0."""
        return self._post_json("/exposure/reset", {})

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def set_manual_focus(self, lens_position: float) -> dict[str, Any]:
        """Lock focus at lens_position in [0, 1]. 0=near, 1=infinity."""
        if not 0.0 <= lens_position <= 1.0:
            raise ValueError(f"lens_position must be in [0, 1], got {lens_position}")
        return self._post_json("/focus", {"lens_position": lens_position})

    def set_focus_point(self, x: float, y: float) -> dict[str, Any]:
        """Auto-focus to a screen-space point on the iPhone preview."""
        return self._post_json("/focus", {"x": x, "y": y})

    def reset_focus(self) -> dict[str, Any]:
        """Return to continuous auto-focus."""
        return self._post_json("/focus", {"auto": True})

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_capabilities(self) -> dict[str, Any]:
        """Returns device info, active format, manual exposure ranges, current settings."""
        return self._get_json("/capabilities")

    def get_health(self) -> dict[str, Any]:
        return self._get_json("/health")

    def get_identity(self) -> dict[str, Any]:
        return self._get_json("/")

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str, *, timeout: float, raw: bool = False) -> bytes:
        url = f"{self.base_url()}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            self._raise_http_error(e, path, "GET")
        except urllib.error.URLError as e:
            raise IphoneCameraTimeout(f"GET {path} failed: {e.reason}") from e
        except TimeoutError as e:
            raise IphoneCameraTimeout(f"GET {path} timed out after {timeout}s") from e
        return body

    def _get_json(self, path: str) -> dict[str, Any]:
        body = self._get(path, timeout=self.config.request_timeout_s)
        return json.loads(body)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url()}{path}"
        data = json.dumps(payload).encode("utf-8")
        # Explicit Content-Length: Python's urllib doesn't always set it
        # automatically, and the MiraCam server uses it to know when the
        # body has finished arriving across TCP segments.
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
        }
        # One retry on transient body-parse failures. The iOS HTTP parser
        # occasionally dispatches before the body has fully arrived in a
        # second TCP segment, and the server returns 400 "must provide".
        # The retry happens fresh (new connection, new send) and usually
        # succeeds. Cheaper than a stronger handshake.
        last_err: Optional[Exception] = None
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.config.request_timeout_s) as resp:
                    body = resp.read()
                return json.loads(body)
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode(errors="replace")
                except Exception:
                    pass
                if e.code == 400 and "must provide" in err_body and attempt == 0:
                    logger.debug("POST %s: 400 'must provide' on attempt %d, retrying", path, attempt)
                    time.sleep(0.15)
                    continue
                # Reconstruct the error and raise via the normal path
                if hasattr(e, "fp") and e.fp is not None:
                    pass
                e._mira_body = err_body  # type: ignore[attr-defined]
                self._raise_http_error_with_body(e, err_body, path, "POST")
            except urllib.error.URLError as e:
                last_err = e
                raise IphoneCameraTimeout(f"POST {path} failed: {e.reason}") from e
            except TimeoutError as e:
                last_err = e
                raise IphoneCameraTimeout(
                    f"POST {path} timed out after {self.config.request_timeout_s}s"
                ) from e
        # Should not reach here; the retry loop either returns or raises.
        raise IphoneCameraError(f"POST {path}: exhausted retries: {last_err}")

    @staticmethod
    def _raise_http_error(e: urllib.error.HTTPError, path: str, method: str) -> NoReturn:
        """Map HTTP error responses to specific exception classes."""
        try:
            detail = json.loads(e.read())
        except Exception:
            detail = {"error": "unknown", "status": e.code}
        IphoneCamera._raise_http_error_with_body(e, str(detail), path, method)

    @staticmethod
    def _raise_http_error_with_body(
        e: urllib.error.HTTPError, body: str, path: str, method: str,
    ) -> NoReturn:
        """Same as _raise_http_error but body was already read by caller."""
        try:
            detail = json.loads(body)
        except Exception:
            detail = {"error": "unknown", "status": e.code, "body": body[:200]}
        msg = f"{method} {path} -> HTTP {e.code}: {detail}"
        if e.code == 503:
            raise IphoneCameraNotReady(msg) from e
        if e.code == 400:
            raise IphoneCameraError(msg) from e
        raise IphoneCameraError(msg) from e


# --------------------------------------------------------------------------
# Bonjour discovery (uses zeroconf, a pure-Python mDNS implementation)
# --------------------------------------------------------------------------

def _bonjour_discover_miracam(*, timeout_s: float) -> Optional[str]:
    """Block up to timeout_s for a _miracam._tcp announcement.

    Returns the http://host:port/ URL of the first MiraCam found, or None
    if nothing answered. Uses zeroconf (pure-Python mDNS) so no external
    deps beyond the pip package.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as e:
        raise IphoneCameraError(
            "zeroconf package required for Bonjour discovery. "
            "Install with: pip install zeroconf"
        ) from e

    zc = Zeroconf()
    found: dict[str, str] = {}

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info is None:
                return
            addr = info.parsed_addresses()
            if not addr:
                return
            url = f"http://{addr[0]}:{info.port}"
            found[name] = url
            logger.info("Bonjour: discovered MiraCam at %s (%s)", url, name)

        def remove_service(self, zc, type_, name):
            found.pop(name, None)

        def update_service(self, zc, type_, name):
            pass

    try:
        ServiceBrowser(zc, "_miracam._tcp.local.", Listener())
        deadline = time.time() + timeout_s
        while time.time() < deadline and not found:
            time.sleep(0.1)
    finally:
        zc.close()

    if not found:
        return None
    # Return the first URL deterministically (sorted by service name).
    return found[sorted(found)[0]]
