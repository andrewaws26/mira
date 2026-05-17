"""HTTP server for the live preview page (`mira watch`).

Serves a single dark-themed HTML page that polls the pipeline state +
the latest frame from disk. Designed to be opened on the Mac in a
browser tab, or on the iPhone over the LAN, while a smart-capture
runs in another terminal.

Two-process design for the read path: the capture pipeline writes
~/mira/captures/current/{state.json, frame.jpg, stack.jpg}; this server
reads them. No shared memory needed.

Optional jog mode: pass a connected ToolContext via serve(ctx=...) and
the server gains POST /jog and POST /jog/rate endpoints that drive the
mount's TELESCOPE_MOTION switches. The HTML page wires arrow keys to
these endpoints so hold-arrow-to-move "just works" over the LAN. This
makes the preview the unified field UI: live frames + jog control in
one tab on the Mac or the iPhone.

Optional iPhone proxy: pass iphone_url and /live.jpg proxies to
{iphone_url}/preview.jpg so the page can show the iPhone's current
sensor feed alongside Mira's most-recent processed frame.
"""
from __future__ import annotations

import inspect
import json
import logging
import socket
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .pipeline_state import state_dir, read_state

logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent / "ui"
INDEX_HTML_PATH = UI_DIR / "index.html"


# ----------------------------------------------------------------------
# Request handler
# ----------------------------------------------------------------------

class _PreviewHandler(BaseHTTPRequestHandler):
    iphone_url: Optional[str] = None  # set by serve()
    mount_ctx: Optional[Any] = None   # ToolContext; jog enabled iff non-None

    def log_message(self, format, *args):
        # Quiet by default; uncomment for debugging
        # super().log_message(format, *args)
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send_index()
        elif path == "/state.json":
            self._send_state()
        elif path == "/status.json":
            self._handle_status()
        elif path == "/tools":
            self._handle_tools_list()
        elif path == "/frame.jpg":
            self._send_file(state_dir() / "frame.jpg", "image/jpeg")
        elif path == "/stack.jpg":
            self._send_file(state_dir() / "stack.jpg", "image/jpeg")
        elif path == "/live.jpg":
            self._proxy_iphone_preview()
        elif path == "/jog/info":
            self._send_json({"enabled": self.mount_ctx is not None})
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/jog":
            self._handle_jog()
        elif path == "/jog/rate":
            self._handle_jog_rate()
        elif path == "/jog/stop-all":
            self._handle_jog_stop_all()
        elif path == "/run-tool":
            self._handle_run_tool()
        elif path == "/chat":
            self._handle_chat()
        elif path == "/align/up":
            self._handle_align("up")
        elif path == "/align/down":
            self._handle_align("down")
        elif path == "/align/orient":
            self._handle_align("orient")
        elif path == "/align/center-ready":
            self._handle_align("center-ready")
        elif path == "/align/sync":
            self._handle_align("sync")
        else:
            self.send_error(404)

    def _send_index(self) -> None:
        # Read on every request so iteration doesn't require restart.
        try:
            html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            html = "<html><body>UI template missing</body></html>"
        self._send_html(html)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            return json.loads(body)
        except Exception:
            return {}

    def _handle_jog(self) -> None:
        if self.mount_ctx is None:
            self.send_error(503, "jog not enabled (start mira watch with --jog)")
            return
        req = self._read_json()
        direction = req.get("direction")
        action = req.get("action")
        if direction not in {"north", "south", "east", "west"} or action not in {"start", "stop"}:
            self.send_error(400, "expected {direction: N|S|E|W, action: start|stop}")
            return
        try:
            self.mount_ctx.connect_mount()
            _jog_motion(self.mount_ctx.mount, direction, action == "start")
            self._send_json({"ok": True, "direction": direction, "action": action})
        except Exception as e:
            logger.exception("jog failed")
            self.send_error(500, f"jog failed: {e}")

    def _handle_jog_rate(self) -> None:
        if self.mount_ctx is None:
            self.send_error(503, "jog not enabled")
            return
        req = self._read_json()
        rate = req.get("rate")
        if not isinstance(rate, int) or not 1 <= rate <= 9:
            self.send_error(400, "expected {rate: 1..9}")
            return
        try:
            self.mount_ctx.connect_mount()
            from .jog import _set_slew_rate  # late import to avoid circular
            err = _set_slew_rate(self.mount_ctx.mount, rate)
            if err:
                self.send_error(500, err)
                return
            self._send_json({"ok": True, "rate": rate})
        except Exception as e:
            logger.exception("rate change failed")
            self.send_error(500, f"rate change failed: {e}")

    def _handle_jog_stop_all(self) -> None:
        if self.mount_ctx is None:
            self.send_error(503, "jog not enabled")
            return
        try:
            self.mount_ctx.connect_mount()
            from .jog import _stop_all
            _stop_all(self.mount_ctx.mount)
            self._send_json({"ok": True})
        except Exception as e:
            self.send_error(500, str(e))

    def _send_json(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_state(self) -> None:
        p = state_dir() / "state.json"
        if p.exists():
            try:
                body = p.read_bytes()
            except Exception:
                body = b"{}"
        else:
            body = b'{"phase":"idle","message":"no active capture"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        try:
            data = path.read_bytes()
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # /status.json  - overall mount/camera/observer/pipeline health
    # ------------------------------------------------------------------
    def _handle_status(self) -> None:
        status: dict[str, Any] = {
            "version": "0.4.0",
            "jog_enabled": self.mount_ctx is not None,
            "mount": {"connected": False, "ra_deg": None, "dec_deg": None, "slewing": False},
            "camera": {"source": None, "reachable": False, "last_capture": None},
            "observer": None,
            "captures_total": None,
        }

        if self.mount_ctx is not None:
            cfg = self.mount_ctx.config
            mount = self.mount_ctx.mount
            try:
                status["mount"]["connected"] = mount.is_connected()
            except Exception:
                pass
            if status["mount"]["connected"]:
                try:
                    ra, dec = mount.get_position(timeout=1.5)
                    status["mount"]["ra_deg"] = ra
                    status["mount"]["dec_deg"] = dec
                except Exception:
                    pass
                try:
                    status["mount"]["slewing"] = mount.is_slewing()
                except Exception:
                    pass

            status["camera"]["source"] = cfg.camera.source
            status["camera"]["reachable"] = _camera_reachable(cfg)
            try:
                status["observer"] = {
                    "latitude_deg": cfg.observer.latitude,
                    "longitude_deg": cfg.observer.longitude,
                    "utc_offset_hours": _local_utc_offset_hours(),
                }
            except Exception:
                pass

        # Pipeline summary from the state file (works without mount)
        pstate = read_state()
        if pstate is not None:
            status["pipeline_phase"] = pstate.phase
            status["pipeline_target"] = pstate.target

        self._send_json(status)

    # ------------------------------------------------------------------
    # /tools  - list available MCP tools with descriptions
    # ------------------------------------------------------------------
    def _handle_tools_list(self) -> None:
        from . import tools as tool_layer
        out = []
        for fn in tool_layer.TOOLS:
            sig = inspect.signature(fn)
            params = [p for p in sig.parameters.values() if p.name != "ctx"]
            first_param = params[0].name if params else None
            doc = (fn.__doc__ or "").strip().split("\n", 1)[0]
            out.append({
                "name": fn.__name__,
                "description": doc,
                "first_param": first_param,
                "params": [
                    {"name": p.name, "default": (None if p.default is inspect.Parameter.empty else _safe_default(p.default))}
                    for p in params
                ],
            })
        self._send_json(out)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # /run-tool  - invoke a tool by name with args
    # ------------------------------------------------------------------
    def _handle_run_tool(self) -> None:
        if self.mount_ctx is None:
            self.send_error(503, "tool invocation requires a ToolContext (mira watch always provides one; this should not happen)")
            return
        body = self._read_json()
        tool_name = body.get("tool")
        args = body.get("args") or {}
        if not isinstance(tool_name, str):
            self.send_error(400, "expected {tool: str, args: object}")
            return
        from . import tools as tool_layer
        fn = next((t for t in tool_layer.TOOLS if t.__name__ == tool_name), None)
        if fn is None:
            self._send_json({"error": f"unknown tool: {tool_name}"})
            return
        try:
            result = fn(**args, ctx=self.mount_ctx)
            self._send_json({"result": _jsonable(result)})
        except TypeError as e:
            self._send_json({"error": f"bad arguments: {e}"})
        except Exception as e:
            logger.exception("run-tool %s failed", tool_name)
            self._send_json({
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc().splitlines()[-6:],
            })

    # ------------------------------------------------------------------
    # /chat  - LLM-backed natural language interface to tools
    # ------------------------------------------------------------------
    def _handle_chat(self) -> None:
        if self.mount_ctx is None:
            self.send_error(503, "chat requires a ToolContext")
            return
        body = self._read_json()
        message = body.get("message")
        history = body.get("history") or []
        if not isinstance(message, str) or not message.strip():
            self.send_error(400, "expected {message: str, history?: [...]}")
            return
        from . import mira_chat
        from . import tools as tool_layer
        try:
            result = mira_chat.run_chat(message, history, tool_layer, self.mount_ctx)
            self._send_json(result)
        except Exception as e:
            logger.exception("chat failed")
            self._send_json({"error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------------------
    # /align/*  - step-by-step alignment wizard
    # ------------------------------------------------------------------
    def _handle_align(self, step: str) -> None:
        if self.mount_ctx is None:
            self._send_json({
                "ok": False,
                "troubleshoot": "watch must be started with --jog to drive the mount.",
            })
            return

        c = self.mount_ctx
        try:
            if step == "up":
                from . import tools as tool_layer
                result = tool_layer.wake_up(ctx=c)
                if result.get("indi_listening"):
                    self._send_json({"ok": True, "message": "indiserver listening on :7624"})
                else:
                    self._send_json({
                        "ok": False,
                        "troubleshoot": (
                            "indiserver did not start. Common causes: "
                            "(1) mount unplugged from FTDI cable, "
                            "(2) wrong mount.port in config.yaml (must be cu.* not tty.*), "
                            "(3) another mira process already holds the connection."
                        ),
                    })
                return

            if step == "down":
                from . import tools as tool_layer
                tool_layer.shut_down(ctx=c)
                self._send_json({"ok": True, "message": "indiserver stopped"})
                return

            if step == "orient":
                from . import tools as tool_layer
                ok = tool_layer.orient(ctx=c)
                if ok:
                    self._send_json({"ok": True, "message": "drove ~12s north toward Polaris"})
                else:
                    self._send_json({
                        "ok": False,
                        "troubleshoot": (
                            "Motion switch refused. Check that indiserver is up and the mount is connected. "
                            "Try cycling: stop -> start."
                        ),
                    })
                return

            if step == "center-ready":
                # No-op on the server; just acknowledges so the wizard advances.
                self._send_json({"ok": True, "message": "ready to plate-solve"})
                return

            if step == "sync":
                from . import tools as tool_layer
                # capture, solve, sync
                try:
                    img = tool_layer.capture_frame(ctx=c)
                except Exception as e:
                    self._send_json({
                        "ok": False,
                        "troubleshoot": f"capture failed: {e}. Is the camera backend up? (Check Status panel.)",
                    })
                    return
                ra_hint, dec_hint = None, None
                try:
                    ra_hint, dec_hint = c.mount.get_position()
                except Exception:
                    pass
                solved = tool_layer.plate_solve(img, ra_hint_deg=ra_hint, dec_hint_deg=dec_hint, ctx=c)
                if solved is None:
                    self._send_json({
                        "ok": False,
                        "troubleshoot": (
                            "Plate solve failed. Common causes: "
                            "(1) not enough stars in the frame (point higher / longer exposure), "
                            "(2) iPhone not centered over the eyepiece, "
                            "(3) wrong ASTAP star database for this FOV. "
                            f"Frame at: {img}"
                        ),
                    })
                    return
                ra, dec = solved
                sync_ok = tool_layer.sync_mount(ra, dec, ctx=c)
                if not sync_ok:
                    self._send_json({
                        "ok": False,
                        "troubleshoot": "Mount rejected sync. Try restarting indiserver (stop / start).",
                    })
                    return
                self._send_json({"ok": True, "message": f"synced at RA={ra:.3f}° Dec={dec:.3f}°"})
                return

            self.send_error(404, f"unknown align step: {step}")
        except Exception as e:
            logger.exception("align step %s failed", step)
            self._send_json({"ok": False, "troubleshoot": f"unexpected error: {e}"})

    def _proxy_iphone_preview(self) -> None:
        url = self.iphone_url
        if not url:
            self.send_error(503, "no iPhone URL configured")
            return
        try:
            req = urllib.request.Request(f"{url.rstrip('/')}/preview.jpg")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            self.send_error(502, f"iPhone unreachable: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ----------------------------------------------------------------------
# Server entry point
# ----------------------------------------------------------------------

def serve(
    *,
    port: int = 8090,
    iphone_url: Optional[str] = None,
    mount_ctx: Optional[Any] = None,
) -> None:
    """Start the preview server. Blocks until Ctrl+C.

    iphone_url: if given, /live.jpg proxies to {iphone_url}/preview.jpg
                so the page can show the iPhone's current sensor feed
                alongside Mira's most-recent processed frame.

    mount_ctx:  if given (a connected ToolContext), enables the /jog
                endpoints so arrow keys on the page drive mount motion.
    """
    _PreviewHandler.iphone_url = iphone_url
    _PreviewHandler.mount_ctx = mount_ctx

    server = ThreadingHTTPServer(("0.0.0.0", port), _PreviewHandler)
    bind_host = _lan_ip() or "localhost"
    print(f"mira watch: serving at http://{bind_host}:{port}  (Ctrl+C to stop)")
    print(f"            local:   http://localhost:{port}")
    if iphone_url:
        print(f"            iphone proxy: {iphone_url}")
    if mount_ctx is not None:
        print(f"            jog: enabled (arrow keys move the mount)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()


def _camera_reachable(cfg) -> bool:
    """Best-effort camera health check. Doesn't capture, just probes."""
    try:
        if cfg.camera.source == "iphone_bridge" and cfg.camera.iphone_url:
            req = urllib.request.Request(f"{cfg.camera.iphone_url.rstrip('/')}/health")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                return r.status == 200
        # imagesnap path: assume reachable, no cheap health check
        return True
    except Exception:
        return False


def _jsonable(v: Any) -> Any:
    """Coerce common tool return types into JSON-encodable values."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(v) for k, v in v.items()}
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    # Dataclasses, anything with __dict__
    try:
        return str(v)
    except Exception:
        return repr(v)


def _safe_default(v: Any) -> Any:
    """Default values for tool params, made JSON-safe."""
    try:
        return _jsonable(v)
    except Exception:
        return None


def _local_utc_offset_hours() -> float:
    """Mirror tools._local_utc_offset_hours without importing the heavy module."""
    now = datetime.now().astimezone()
    off = now.utcoffset()
    return 0.0 if off is None else off.total_seconds() / 3600.0


def _jog_motion(mount, direction: str, start: bool) -> None:
    """Apply a single direction motion switch to the mount.

    Mirrors jog.py's _start_motion / _stop_motion but takes an explicit
    direction string from the web request. Stopping a direction zeroes
    both ends of the relevant axis to be safe even if the user releases
    a key after switching directions mid-press.
    """
    if direction in ("north", "south"):
        mount.client.set_switch("TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": start and direction == "north",
            "MOTION_SOUTH": start and direction == "south",
        })
    elif direction in ("east", "west"):
        mount.client.set_switch("TELESCOPE_MOTION_WE", {
            "MOTION_EAST": start and direction == "east",
            "MOTION_WEST": start and direction == "west",
        })


def _lan_ip() -> Optional[str]:
    """Best-effort LAN IP detection for the friendly URL in the boot message."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None
