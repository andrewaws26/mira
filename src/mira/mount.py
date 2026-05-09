"""Celestron NexStar mount control via INDI.

Talks the INDI XML wire protocol directly over TCP. We deliberately avoid
the `pyindi-client` C extension, which is finicky to build on macOS Apple
Silicon and pins to specific INDI library versions. The wire protocol is
small enough that a clean Python implementation is easier to debug and
maintain than fighting with a binding library.

Architecture: a reader thread continuously parses incoming XML elements
from the indiserver TCP stream and updates a property cache. The main
thread reads from the cache and sends `newXxxVector` elements back to
the server through the same socket. A condition variable lets the main
thread block on property state changes (slew completion, connect ack).

Public API surface is `CelestronMount`. Construct it, call `connect()`,
then `get_position()`, `slew_to()`, `sync()`, `wait_slew_complete()`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class MountError(RuntimeError):
    """Raised on any mount control failure."""


class MountNotConnected(MountError):
    pass


class MountTimeoutError(MountError):
    pass


# INDI property states.
STATE_IDLE = "Idle"
STATE_OK = "Ok"
STATE_BUSY = "Busy"
STATE_ALERT = "Alert"


@dataclass
class IndiProperty:
    """Snapshot of a single INDI property vector."""

    device: str
    name: str
    state: str
    perm: str
    elements: dict[str, float | str | bool] = field(default_factory=dict)
    timestamp: float = 0.0

    def get_number(self, key: str) -> float:
        return float(self.elements[key])

    def get_switch(self, key: str) -> bool:
        v = self.elements.get(key, False)
        return bool(v)


class IndiClient:
    """Minimal INDI XML client. Connects to indiserver over TCP."""

    def __init__(self, host: str = "localhost", port: int = 7624, device: str = "Celestron GPS") -> None:
        self.host = host
        self.port = port
        self.device = device
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._properties: dict[str, IndiProperty] = {}
        self._connected = False

    def connect(self, timeout: float = 5.0) -> None:
        """Open TCP socket, start the reader thread, subscribe to all device properties."""
        if self._connected:
            return
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            raise MountError(
                f"could not reach INDI server at {self.host}:{self.port}: {e}. "
                "Start it with: indiserver -v indi_celestron_gps"
            ) from e
        self._sock.settimeout(None)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="indi-reader", daemon=True)
        self._reader.start()
        self._send(f"<getProperties version='1.7' device='{self.device}'/>")
        self._connected = True

    def disconnect(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=2.0)
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_property(self, name: str) -> Optional[IndiProperty]:
        with self._lock:
            return self._properties.get(name)

    def wait_for_property(self, name: str, timeout: float = 5.0) -> IndiProperty:
        deadline = time.monotonic() + timeout
        with self._cv:
            while name not in self._properties:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MountTimeoutError(
                        f"property {name!r} did not appear within {timeout}s. "
                        "Is the device connected and the driver loaded?"
                    )
                self._cv.wait(remaining)
            return self._properties[name]

    def wait_for_state(
        self,
        name: str,
        target_states: tuple[str, ...] | str = (STATE_OK,),
        timeout: float = 60.0,
    ) -> IndiProperty:
        if isinstance(target_states, str):
            target_states = (target_states,)
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                prop = self._properties.get(name)
                if prop is not None and prop.state in target_states:
                    return prop
                if prop is not None and prop.state == STATE_ALERT:
                    raise MountError(f"property {name!r} entered Alert state")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cur = prop.state if prop else "<undefined>"
                    raise MountTimeoutError(
                        f"property {name!r} did not reach {target_states} within {timeout}s "
                        f"(current state: {cur})"
                    )
                self._cv.wait(remaining)

    def set_switch(self, name: str, values: dict[str, bool]) -> None:
        if not self._connected:
            raise MountNotConnected("not connected")
        parts = [f"<newSwitchVector device='{self.device}' name='{name}'>"]
        for k, v in values.items():
            state = "On" if v else "Off"
            parts.append(f"<oneSwitch name='{k}'>{state}</oneSwitch>")
        parts.append("</newSwitchVector>")
        self._send("".join(parts))

    def set_number(self, name: str, values: dict[str, float]) -> None:
        if not self._connected:
            raise MountNotConnected("not connected")
        parts = [f"<newNumberVector device='{self.device}' name='{name}'>"]
        for k, v in values.items():
            parts.append(f"<oneNumber name='{k}'>{v}</oneNumber>")
        parts.append("</newNumberVector>")
        self._send("".join(parts))

    def _send(self, xml: str) -> None:
        if self._sock is None:
            raise MountNotConnected("socket not open")
        try:
            self._sock.sendall(xml.encode("utf-8"))
        except OSError as e:
            raise MountError(f"failed to send to INDI: {e}") from e

    def _read_loop(self) -> None:
        """Read XML elements from the socket and update the property cache.

        INDI does not send a single root element. The stream is a sequence of
        root-level XML vectors. We use ElementTree.XMLPullParser which handles
        this cleanly when fed bytes incrementally.
        """
        # XMLPullParser expects a single document. Wrap the stream by feeding
        # an opening root tag, then everything from indiserver, then closing.
        # In practice the parser accepts the close tag of each root-level
        # vector and emits an "end" event we can read.
        parser = ET.XMLPullParser(events=("end",))
        parser.feed(b"<indi>")  # synthetic root so all vectors are children
        if self._sock is None:
            return
        sock = self._sock
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(8192)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                try:
                    parser.feed(chunk)
                except ET.ParseError as e:
                    logger.warning("INDI XML parse error: %s", e)
                    continue
                for event in parser.read_events():
                    if len(event) < 2:
                        continue
                    elem = event[1]  # type: ignore[misc]
                    if not isinstance(elem, ET.Element):
                        continue
                    if elem.tag.startswith("def") or elem.tag.startswith("set"):
                        self._handle_vector(elem)
                    elem.clear()
        finally:
            try:
                parser.close()
            except ET.ParseError:
                pass

    def _handle_vector(self, elem: ET.Element) -> None:
        device = elem.get("device", "")
        if device != self.device:
            return
        name = elem.get("name", "")
        state = elem.get("state", STATE_IDLE)
        perm = elem.get("perm", "rw")
        elements: dict[str, float | str | bool] = {}
        for child in elem:
            child_name = child.get("name", "")
            text = (child.text or "").strip()
            if child.tag in ("defNumber", "oneNumber"):
                try:
                    elements[child_name] = float(text)
                except ValueError:
                    elements[child_name] = text
            elif child.tag in ("defSwitch", "oneSwitch"):
                elements[child_name] = text.lower() == "on"
            elif child.tag in ("defText", "oneText"):
                elements[child_name] = text
            elif child.tag in ("defLight", "oneLight"):
                elements[child_name] = text
        with self._cv:
            existing = self._properties.get(name)
            if existing is None or elem.tag.startswith("def"):
                self._properties[name] = IndiProperty(
                    device=device,
                    name=name,
                    state=state,
                    perm=perm,
                    elements=elements,
                    timestamp=time.monotonic(),
                )
            else:
                existing.state = state
                if elements:
                    existing.elements.update(elements)
                existing.timestamp = time.monotonic()
            self._cv.notify_all()


class CelestronMount:
    """High-level Celestron mount control on top of INDI.

    Coordinate conventions for the public API:
      - RA in degrees [0, 360)
      - Dec in degrees [-90, 90]

    Internally we convert to INDI's RA-in-hours convention.
    """

    PROP_CONNECTION = "CONNECTION"
    PROP_COORD = "EQUATORIAL_EOD_COORD"
    PROP_ON_COORD_SET = "ON_COORD_SET"
    PROP_ABORT = "TELESCOPE_ABORT_MOTION"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7624,
        device: str = "Celestron GPS",
    ) -> None:
        self._client = IndiClient(host=host, port=port, device=device)
        self.host = host
        self.port = port
        self.device = device

    @property
    def client(self) -> IndiClient:
        return self._client

    def connect(self, timeout: float = 10.0) -> None:
        """Open INDI connection and bring the driver online."""
        self._client.connect(timeout=timeout)
        # Wait for the CONNECTION property to be defined by the driver.
        self._client.wait_for_property(self.PROP_CONNECTION, timeout=timeout)
        prop = self._client.get_property(self.PROP_CONNECTION)
        assert prop is not None
        if not prop.get_switch("CONNECT"):
            self._client.set_switch(self.PROP_CONNECTION, {"CONNECT": True, "DISCONNECT": False})
            self._client.wait_for_state(self.PROP_CONNECTION, STATE_OK, timeout=timeout)
        # Wait for coord vector so we know the driver is fully alive.
        self._client.wait_for_property(self.PROP_COORD, timeout=timeout)

    def disconnect(self) -> None:
        try:
            if self._client.is_connected():
                self._client.set_switch(
                    self.PROP_CONNECTION, {"CONNECT": False, "DISCONNECT": True}
                )
        finally:
            self._client.disconnect()

    def is_connected(self) -> bool:
        if not self._client.is_connected():
            return False
        prop = self._client.get_property(self.PROP_CONNECTION)
        return prop is not None and prop.get_switch("CONNECT")

    def get_position(self) -> tuple[float, float]:
        """Return current pointing as (RA degrees, Dec degrees)."""
        prop = self._client.get_property(self.PROP_COORD)
        if prop is None:
            raise MountNotConnected("EQUATORIAL_EOD_COORD not yet defined")
        ra_hours = prop.get_number("RA")
        dec_deg = prop.get_number("DEC")
        return ra_hours * 15.0, dec_deg

    def is_slewing(self) -> bool:
        prop = self._client.get_property(self.PROP_COORD)
        if prop is None:
            return False
        return prop.state == STATE_BUSY

    def slew_to(self, ra_deg: float, dec_deg: float, timeout: float = 60.0) -> bool:
        """Slew to apparent RA/Dec. Returns True on completion. Caller should sync first."""
        self._set_coord_mode("TRACK")
        self._send_target(ra_deg, dec_deg)
        return self.wait_slew_complete(timeout=timeout)

    def sync(self, ra_deg: float, dec_deg: float, timeout: float = 10.0) -> bool:
        """Tell the mount its current pointing is the given RA/Dec."""
        self._set_coord_mode("SYNC")
        self._send_target(ra_deg, dec_deg)
        # SYNC operation should leave the property Ok almost immediately.
        try:
            self._client.wait_for_state(self.PROP_COORD, (STATE_OK,), timeout=timeout)
        except MountTimeoutError:
            return False
        # Restore default mode for any subsequent slew.
        self._set_coord_mode("TRACK")
        return True

    def wait_slew_complete(self, timeout: float = 60.0) -> bool:
        try:
            self._client.wait_for_state(self.PROP_COORD, (STATE_OK,), timeout=timeout)
            return True
        except MountTimeoutError:
            return False

    def abort(self) -> None:
        try:
            self._client.set_switch(self.PROP_ABORT, {"ABORT": True})
        except MountError as e:
            logger.warning("ABORT failed: %s", e)

    def _set_coord_mode(self, mode: str) -> None:
        """Set ON_COORD_SET to TRACK, SLEW, or SYNC."""
        if mode not in {"TRACK", "SLEW", "SYNC"}:
            raise ValueError(f"invalid coord mode: {mode}")
        switches = {"TRACK": False, "SLEW": False, "SYNC": False}
        switches[mode] = True
        self._client.set_switch(self.PROP_ON_COORD_SET, switches)

    def _send_target(self, ra_deg: float, dec_deg: float) -> None:
        if not 0.0 <= ra_deg < 360.0:
            ra_deg = ra_deg % 360.0
        if not -90.0 <= dec_deg <= 90.0:
            raise ValueError(f"dec out of range [-90, 90]: {dec_deg}")
        ra_hours = ra_deg / 15.0
        self._client.set_number(self.PROP_COORD, {"RA": ra_hours, "DEC": dec_deg})

    def __enter__(self) -> "CelestronMount":
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.disconnect()
