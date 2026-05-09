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

import enum
import logging
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class MountError(RuntimeError):
    """Raised on any mount control failure."""


class MountNotConnected(MountError):
    pass


class MountTimeoutError(MountError):
    pass


class SlewOutcome(enum.Enum):
    """Why a slew_to call ended. Distinguishes "still moving when we gave
    up" from "firmware silently refused the command" from "landed off
    target" so callers and logs do not collapse three different failure
    modes into a single False.
    """

    ARRIVED = "arrived"  # mount reached target within tolerance
    NOOP = "noop"  # requested distance was sub-arcminute and we did not move
    REFUSED = "refused"  # mount accepted command but moved < 50% of requested
    PARTIAL = "partial"  # moved most of the way but landed > tolerance off
    TIMED_OUT = "timed_out"  # wait_slew_complete timed out (still slewing in firmware)
    ABORTED = "aborted"  # abort() was called between Busy and Ok

    @property
    def succeeded(self) -> bool:
        return self in (SlewOutcome.ARRIVED, SlewOutcome.NOOP)


class WaitOutcome(enum.Enum):
    """Result of wait_slew_complete. SETTLED means Busy -> Ok seen.
    NOT_STARTED means we never observed Busy (likely a noop). TIMED_OUT
    means Busy was seen but Ok never arrived within the timeout, i.e.
    the mount is still slewing when we returned.
    """

    SETTLED = "settled"
    NOT_STARTED = "not_started"
    TIMED_OUT = "timed_out"
    ABORTED = "aborted"


# INDI property states.
STATE_IDLE = "Idle"
STATE_OK = "Ok"
STATE_BUSY = "Busy"
STATE_ALERT = "Alert"


@dataclass
class ObserverInfo:
    """Observer location and UTC offset to push to the mount on connect."""
    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0
    utc_offset_hours: float = 0.0


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

    def set_text(self, name: str, values: dict[str, str]) -> None:
        if not self._connected:
            raise MountNotConnected("not connected")
        parts = [f"<newTextVector device='{self.device}' name='{name}'>"]
        for k, v in values.items():
            # XML-escape the value: tail-end ', <, &, > can break the wire
            v_esc = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;")
            parts.append(f"<oneText name='{k}'>{v_esc}</oneText>")
        parts.append("</newTextVector>")
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
                    # XMLPullParser fires "end" for every element, including
                    # the inner <defSwitch>, <defNumber>, ... children. We
                    # only want the outer *Vector wrappers; clearing inner
                    # elements would discard their attributes before the
                    # outer end event arrives. Top-level vectors all end in
                    # "Vector"; <message> and <getProperties> we ignore.
                    is_vector = (
                        (elem.tag.startswith("def") or elem.tag.startswith("set"))
                        and elem.tag.endswith("Vector")
                    )
                    if not is_vector:
                        continue
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
    PROP_DEVICE_PORT = "DEVICE_PORT"
    PROP_GEOGRAPHIC_COORD = "GEOGRAPHIC_COORD"
    PROP_TIME_UTC = "TIME_UTC"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7624,
        device: str = "Celestron GPS",
        serial_port: str | None = None,
        observer: Optional["ObserverInfo"] = None,
    ) -> None:
        self._client = IndiClient(host=host, port=port, device=device)
        self.host = host
        self.port = port
        self.device = device
        self.serial_port = serial_port
        self.observer = observer
        self._abort_pending = False

    @property
    def client(self) -> IndiClient:
        return self._client

    def connect(self, timeout: float = 10.0) -> None:
        """Open INDI connection and bring the driver online.

        If `serial_port` was passed at construction (or via config), push it
        to the driver's DEVICE_PORT property before issuing CONNECT. Without
        this the Celestron driver tries its compiled-in default
        (/dev/cu.usbserial) which almost never matches a real FTDI cable's
        device path (/dev/tty.usbserial-XXXXXXXX).
        """
        self._client.connect(timeout=timeout)
        # Wait for the CONNECTION property to be defined by the driver.
        self._client.wait_for_property(self.PROP_CONNECTION, timeout=timeout)
        # Push the configured serial port before connecting.
        if self.serial_port:
            try:
                self._client.wait_for_property(self.PROP_DEVICE_PORT, timeout=timeout)
                self._client.set_text(self.PROP_DEVICE_PORT, {"PORT": self.serial_port})
            except MountTimeoutError:
                logger.warning(
                    "DEVICE_PORT property not advertised by driver; using its default"
                )
        prop = self._client.get_property(self.PROP_CONNECTION)
        assert prop is not None
        if not prop.get_switch("CONNECT"):
            self._client.set_switch(self.PROP_CONNECTION, {"CONNECT": True, "DISCONNECT": False})
            self._client.wait_for_state(self.PROP_CONNECTION, STATE_OK, timeout=timeout)
        # Wait for the coord vector to be defined, then wait for its first
        # real update from the mount poll. Without this, the def's initial
        # RA=0/DEC=0 placeholders are what get_position returns to a caller
        # that runs immediately after connect().
        self._client.wait_for_property(self.PROP_COORD, timeout=timeout)
        self._wait_for_coord_poll(timeout=timeout)
        # Push observer location and time so the mount's own goto / horizon
        # / tracking math has correct inputs.
        if self.observer is not None:
            self.set_observer_info(
                lat_deg=self.observer.latitude_deg,
                lon_deg=self.observer.longitude_deg,
                elev_m=self.observer.elevation_m,
                utc_offset_hours=self.observer.utc_offset_hours,
            )

    def _wait_for_coord_poll(self, timeout: float = 10.0) -> None:
        """Block until the driver has pushed at least one set of EQUATORIAL_EOD_COORD
        from the mount (as opposed to the def's initial 0/0 placeholder).

        The driver polls position roughly once per second after CONNECTION
        goes Ok. Returns once we observe a setNumberVector update.
        """
        prop = self._client.get_property(self.PROP_COORD)
        baseline_ts = prop.timestamp if prop is not None else 0.0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            prop = self._client.get_property(self.PROP_COORD)
            if prop is None:
                continue
            if prop.timestamp > baseline_ts:
                return
        logger.warning(
            "EQUATORIAL_EOD_COORD did not see an update within %.1fs; "
            "position may be stale until next poll", timeout
        )

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

    def get_position(self, timeout: float = 5.0) -> tuple[float, float]:
        """Return current pointing as (RA degrees, Dec degrees).

        Waits up to `timeout` seconds for the driver to populate RA/DEC
        elements on EQUATORIAL_EOD_COORD. The property is defined as soon as
        the driver loads but the actual values arrive after the first
        handshake roundtrip with the mount, which can be several hundred
        milliseconds.
        """
        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            prop = self._client.get_property(self.PROP_COORD)
            if prop is None:
                time.sleep(0.1)
                continue
            try:
                ra_hours = prop.get_number("RA")
                dec_deg = prop.get_number("DEC")
                return ra_hours * 15.0, dec_deg
            except KeyError as e:
                last_err = e
                time.sleep(0.1)
        if last_err is not None:
            raise MountTimeoutError(
                f"EQUATORIAL_EOD_COORD did not populate RA/DEC within {timeout}s; "
                f"is the mount powered and aligned?"
            )
        raise MountNotConnected("EQUATORIAL_EOD_COORD not yet defined")

    def is_slewing(self) -> bool:
        prop = self._client.get_property(self.PROP_COORD)
        if prop is None:
            return False
        return prop.state == STATE_BUSY

    SLEW_ARRIVAL_TOLERANCE_DEG = 1.0
    # 180s default covers worst-case alt-az slews on a 130SLT with bad
    # alignment, which can take 60s+ when the firmware translates the
    # target RA/Dec into a long motor-axis travel. Older 60s default
    # surfaced these as "timed out" while the mount kept slewing in
    # the background; callers had no way to tell that apart from a
    # firmware refusal.
    SLEW_DEFAULT_TIMEOUT_S = 180.0

    def slew_to(
        self, ra_deg: float, dec_deg: float, timeout: Optional[float] = None
    ) -> bool:
        """Slew to apparent RA/Dec. Returns True only if the mount arrived
        at the target (within SLEW_ARRIVAL_TOLERANCE_DEG).

        Thin wrapper over `slew_to_with_outcome`. Returns False for any
        non-arrival outcome (refused, timed out, partial, aborted) so the
        existing bool-based callers stay compatible. Use
        `slew_to_with_outcome` if you need to distinguish the failure
        modes (e.g., retry on TIMED_OUT but bail on REFUSED).
        """
        outcome = self.slew_to_with_outcome(ra_deg, dec_deg, timeout=timeout)
        return outcome.succeeded

    def slew_to_with_outcome(
        self, ra_deg: float, dec_deg: float, timeout: Optional[float] = None
    ) -> SlewOutcome:
        """Slew to apparent RA/Dec and return a categorized outcome.

        Outcomes:
          ARRIVED   mount landed within SLEW_ARRIVAL_TOLERANCE_DEG of target.
          NOOP      requested distance was sub-arcminute; nothing to do.
          REFUSED   mount moved < 50 percent of requested distance. Common
                    cause: firmware horizon / cable-wrap / slew-limit
                    refusal, or alignment so far off that the target maps
                    to a no-go zone in the mount's internal model.
          PARTIAL   mount moved most of the way but landed > tolerance off.
                    Tracking drift, late-stage refusal, or simply bad
                    alignment leaving the model offset.
          TIMED_OUT EQUATORIAL_EOD_COORD did not return to Ok within the
                    timeout. The mount is likely still slewing when this
                    returns. Caller should poll `wait_slew_complete` or
                    bump the timeout.
          ABORTED   abort() was called between Busy and Ok.
        """
        if timeout is None:
            timeout = self.SLEW_DEFAULT_TIMEOUT_S
        self._abort_pending = False
        try:
            ra_start, dec_start = self.get_position(timeout=3.0)
        except MountError:
            ra_start = ra_deg
            dec_start = dec_deg
        requested_dist = _angular_separation(ra_start, dec_start, ra_deg, dec_deg)

        self._set_coord_mode("TRACK")
        self._send_target(ra_deg, dec_deg)
        wait_outcome = self._wait_slew_complete_outcome(timeout=timeout)

        if wait_outcome is WaitOutcome.ABORTED:
            logger.warning(
                "slew_to(%g, %g) aborted between Busy and Ok.",
                ra_deg, dec_deg,
            )
            return SlewOutcome.ABORTED
        if wait_outcome is WaitOutcome.TIMED_OUT:
            try:
                ra_now, dec_now = self.get_position(timeout=3.0)
                moved = _angular_separation(ra_start, dec_start, ra_now, dec_now)
                logger.warning(
                    "slew_to(%g, %g) timed out after %.0fs while mount was "
                    "still slewing (moved %.3f deg of %.3f deg so far; "
                    "current %g, %g). Mount continues in background; bump "
                    "the timeout, poll wait_slew_complete, or abort().",
                    ra_deg, dec_deg, timeout, moved, requested_dist,
                    ra_now, dec_now,
                )
            except MountError:
                logger.warning(
                    "slew_to(%g, %g) timed out after %.0fs while mount was "
                    "still slewing; current position read failed.",
                    ra_deg, dec_deg, timeout,
                )
            return SlewOutcome.TIMED_OUT

        # Driver polls position roughly once per second; give it a cycle.
        time.sleep(1.0)
        try:
            ra_now, dec_now = self.get_position(timeout=3.0)
        except MountError:
            logger.warning(
                "slew_to(%g, %g) settled but post-slew position read "
                "failed; treating as timed out.",
                ra_deg, dec_deg,
            )
            return SlewOutcome.TIMED_OUT
        moved = _angular_separation(ra_start, dec_start, ra_now, dec_now)
        err_to_target = _angular_separation(ra_now, dec_now, ra_deg, dec_deg)

        if requested_dist < 0.02 and err_to_target < self.SLEW_ARRIVAL_TOLERANCE_DEG:
            return SlewOutcome.NOOP

        if moved < 0.5 * requested_dist:
            logger.warning(
                "slew_to(%g, %g) refused by firmware: mount moved only "
                "%.3f deg of %.3f deg requested (current %g, %g; err to "
                "target %.3f deg). Common causes: horizon / zenith / "
                "cable-wrap limit, parked state, slew-limit menu setting, "
                "or alignment so off the target maps to a no-go zone.",
                ra_deg, dec_deg, moved, requested_dist, ra_now, dec_now, err_to_target,
            )
            return SlewOutcome.REFUSED
        if err_to_target > self.SLEW_ARRIVAL_TOLERANCE_DEG:
            logger.warning(
                "slew_to(%g, %g) partial: landed %.3f deg from target "
                "after moving %.3f deg of %.3f deg requested (current "
                "%g, %g). Tracking drift or alignment offset.",
                ra_deg, dec_deg, err_to_target, moved, requested_dist,
                ra_now, dec_now,
            )
            return SlewOutcome.PARTIAL
        return SlewOutcome.ARRIVED

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
        """Block until a slew completes.

        EQUATORIAL_EOD_COORD is `Ok` while idle/tracking and `Busy` during
        a slew. We must wait for the Busy edge first (otherwise we observe
        the pre-slew Ok and return immediately), then wait for the Ok
        edge (slew settled).

        Thin wrapper over `_wait_slew_complete_outcome` that flattens the
        result to bool (True only when SETTLED or NOT_STARTED-and-Ok).
        Use `_wait_slew_complete_outcome` to distinguish "still slewing"
        from "never started" without grepping logs.
        """
        outcome = self._wait_slew_complete_outcome(timeout=timeout)
        return outcome in (WaitOutcome.SETTLED, WaitOutcome.NOT_STARTED)

    def _wait_slew_complete_outcome(self, timeout: float) -> WaitOutcome:
        deadline = time.monotonic() + timeout
        # Phase 1: wait for the slew to actually start. Give the driver up
        # to ~3 seconds to flip Coord state to Busy after our newNumberVector.
        busy_deadline = min(deadline, time.monotonic() + 3.0)
        try:
            self._client.wait_for_state(
                self.PROP_COORD,
                (STATE_BUSY,),
                timeout=max(0.1, busy_deadline - time.monotonic()),
            )
        except MountTimeoutError:
            # No Busy ever seen. Could be a tiny slew that finished within
            # one poll, or the driver dropped the request. Treat as a
            # noop slew and check the current state.
            prop = self._client.get_property(self.PROP_COORD)
            if prop and prop.state == STATE_OK:
                return WaitOutcome.NOT_STARTED
            return WaitOutcome.TIMED_OUT
        # Phase 2: wait for slew to settle.
        try:
            self._client.wait_for_state(
                self.PROP_COORD,
                (STATE_OK,),
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except MountTimeoutError:
            return WaitOutcome.TIMED_OUT
        # If abort was raised between Busy and Ok, the mount stopped
        # wherever it was, not at the requested target.
        if self._abort_pending:
            return WaitOutcome.ABORTED
        return WaitOutcome.SETTLED

    def abort(self) -> None:
        self._abort_pending = True
        try:
            self._client.set_switch(self.PROP_ABORT, {"ABORT": True})
        except MountError as e:
            logger.warning("ABORT failed: %s", e)

    def set_observer_info(
        self,
        lat_deg: float,
        lon_deg: float,
        elev_m: float = 0.0,
        when: Optional[datetime] = None,
        utc_offset_hours: float = 0.0,
    ) -> bool:
        """Push observer location and current UTC to the mount.

        The Celestron driver exposes GEOGRAPHIC_COORD and TIME_UTC after
        CONNECTION reaches Ok. Pushing accurate values lets the mount's
        own firmware compute meridian flips, horizon limits, and
        tracking rates correctly. Mira's plate-solve workflow does not
        require this; it is a quality-of-life nudge for the hand
        controller's standalone goto.

        Important Celestron-firmware quirk: once the hand controller has
        completed an alignment, it locks its internal location/time and
        the driver's GEOGRAPHIC_COORD vector goes to state=Alert when
        you try to override. The push is sent and accepted at the INDI
        protocol layer, but the mount silently rejects it. This is not
        a Mira bug; it is by design in Celestron's firmware. To force
        new values, undo alignment via the hand controller first.

        Returns True on best-effort protocol success; the mount may still
        ignore the values.
        """
        ok = True
        try:
            self._client.set_number(
                self.PROP_GEOGRAPHIC_COORD,
                {"LAT": float(lat_deg), "LONG": float(lon_deg), "ELEV": float(elev_m)},
            )
        except MountError as e:
            logger.warning("GEOGRAPHIC_COORD push failed: %s", e)
            ok = False
        moment = when if when is not None else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        try:
            self._client.set_text(
                self.PROP_TIME_UTC,
                {
                    "UTC": moment.strftime("%Y-%m-%dT%H:%M:%S"),
                    "OFFSET": f"{float(utc_offset_hours):.2f}",
                },
            )
        except MountError as e:
            logger.warning("TIME_UTC push failed: %s", e)
            ok = False
        return ok

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


def _angular_separation(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Spherical angular separation in degrees, stable for small and large arcs."""
    import math

    a1 = math.radians(ra1_deg)
    a2 = math.radians(ra2_deg)
    d1 = math.radians(dec1_deg)
    d2 = math.radians(dec2_deg)
    cos_sep = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    cos_sep = max(-1.0, min(1.0, cos_sep))
    return math.degrees(math.acos(cos_sep))
