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

import json
import logging
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .pipeline_state import state_dir

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# HTML page (self-contained: no external deps, no JS framework)
# ----------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mira live</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #000; color: #ff3b30; font-family: Menlo, monospace;
    min-height: 100vh; display: flex; flex-direction: column;
  }
  header {
    padding: 12px 18px; border-bottom: 1px solid #5a1410;
    display: flex; align-items: baseline; gap: 18px;
  }
  h1 { font-size: 18px; font-weight: 700; }
  .sub { color: #a02520; font-size: 11px; }
  .spacer { flex: 1; }
  .pill {
    border: 1px solid #5a1410; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; color: #ff3b30;
  }
  .pill.live { color: #ff8080; border-color: #ff8080; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.4 } }

  main {
    flex: 1; display: grid; grid-template-columns: 1fr 280px;
    gap: 0; min-height: 0;
  }
  .imgcol {
    background: #000; display: flex; justify-content: center;
    align-items: center; padding: 12px; min-height: 0; overflow: hidden;
  }
  .imgcol img {
    max-width: 100%; max-height: 100%; object-fit: contain;
    border: 1px solid #5a1410;
  }
  .imgcol.empty {
    color: #5a1410; font-style: italic; font-size: 12px;
  }
  aside {
    border-left: 1px solid #5a1410; padding: 14px 18px;
    display: flex; flex-direction: column; gap: 10px; overflow-y: auto;
  }
  .row { display: flex; justify-content: space-between; font-size: 12px; }
  .row .k { color: #a02520; }
  .row .v { color: #ff3b30; }
  .progress {
    height: 8px; background: #1a0808; border: 1px solid #5a1410; border-radius: 2px;
    overflow: hidden;
  }
  .progress .bar { height: 100%; background: #ff3b30; transition: width .3s; }
  .section { margin-top: 8px; color: #a02520; font-size: 10px; letter-spacing: 1px; }
  .msg { color: #ff3b30; font-size: 12px; line-height: 1.4; }
  .tabs { display: flex; gap: 8px; margin-bottom: 6px; }
  .tab {
    border: 1px solid #5a1410; padding: 4px 10px; cursor: pointer;
    font-size: 10px; color: #a02520; user-select: none;
  }
  .tab.active { color: #ff3b30; border-color: #ff3b30; }
  @media (max-width: 700px) {
    main { grid-template-columns: 1fr; grid-template-rows: 60vh auto; }
    aside { border-left: none; border-top: 1px solid #5a1410; }
  }
</style>
</head>
<body>
<header>
  <h1>MIRA</h1>
  <span class="sub">live preview</span>
  <span class="spacer"></span>
  <span id="conn" class="pill">waiting</span>
</header>

<main>
  <div class="imgcol" id="imgcol">
    <img id="frame" alt="">
  </div>

  <aside>
    <div class="tabs">
      <span class="tab active" data-src="stack">stack</span>
      <span class="tab" data-src="frame">last frame</span>
      <span class="tab" data-src="live">iphone live</span>
    </div>

    <div class="section" id="jog-section" style="display:none">JOG</div>
    <div id="jog-panel" style="display:none">
      <div class="row"><span class="k">arrows</span> <span class="v">N S E W</span></div>
      <div class="row"><span class="k">rate</span> <span class="v" id="rate">5</span> <span class="k">(1-9 keys)</span></div>
      <div class="row"><span class="k">status</span> <span class="v" id="jog-status">idle</span></div>
      <div class="msg" style="margin-top:4px">click frame area + hold arrow to move.<br>release to stop. Q to release all.</div>
    </div>

    <div class="section">TARGET</div>
    <div class="row"><span class="k">name</span> <span class="v" id="target">-</span></div>
    <div class="row"><span class="k">category</span> <span class="v" id="category">-</span></div>
    <div class="row"><span class="k">pipeline</span> <span class="v" id="pipeline">-</span></div>

    <div class="section">PHASE</div>
    <div class="row"><span class="k">state</span> <span class="v" id="phase">idle</span></div>
    <div class="msg" id="message"></div>

    <div class="section">EXPOSURE</div>
    <div class="row"><span class="k">ISO</span> <span class="v" id="iso">-</span></div>
    <div class="row"><span class="k">shutter</span> <span class="v" id="shutter">-</span></div>
    <div class="row"><span class="k">bias</span> <span class="v" id="bias">-</span></div>
    <div class="row"><span class="k">mean lum</span> <span class="v" id="meanlum">-</span></div>

    <div class="section">PROGRESS</div>
    <div class="row"><span class="k">captured</span> <span class="v" id="captured">0 / 0</span></div>
    <div class="row"><span class="k">stacked</span> <span class="v" id="stacked">0</span></div>
    <div class="progress"><div class="bar" id="bar" style="width:0%"></div></div>

    <div class="section">OUTPUT</div>
    <div class="msg" id="output" style="word-break:break-all">-</div>
    <div class="row"><span class="k">updated</span> <span class="v" id="updated">-</span></div>
  </aside>
</main>

<script>
let imgSrc = "stack";   // "stack" | "frame" | "live"
let imgEl = document.getElementById("frame");
let connEl = document.getElementById("conn");

document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    imgSrc = t.dataset.src;
    refreshImage();
  });
});

function refreshImage() {
  // Cache-bust with epoch ms so the browser always reloads
  const url = "/" + imgSrc + ".jpg?t=" + Date.now();
  // Use a hidden image to preload, only swap on success
  const probe = new Image();
  probe.onload = () => {
    imgEl.src = url;
    imgEl.style.display = "block";
    document.getElementById("imgcol").classList.remove("empty");
  };
  probe.onerror = () => {
    if (imgSrc === "stack") {
      imgEl.style.display = "none";
      document.getElementById("imgcol").classList.add("empty");
    }
  };
  probe.src = url;
}

function fmt(v, dp=2) {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") return v.toFixed(dp);
  return v;
}
function fmtShutter(ms) {
  if (ms === null || ms === undefined) return "-";
  if (ms >= 500) return (ms / 1000).toFixed(2) + "s";
  if (ms >= 1) return ms.toFixed(1) + "ms";
  return ms.toFixed(3) + "ms";
}

async function refreshState() {
  try {
    const r = await fetch("/state.json", { cache: "no-store" });
    if (!r.ok) throw new Error("bad status " + r.status);
    const s = await r.json();
    connEl.textContent = "live";
    connEl.classList.add("live");

    document.getElementById("target").textContent = s.target || "-";
    document.getElementById("category").textContent = s.category || "-";
    document.getElementById("pipeline").textContent = s.pipeline || "-";
    document.getElementById("phase").textContent = s.phase || "idle";
    document.getElementById("message").textContent = s.message || "";
    document.getElementById("iso").textContent = s.iso == null ? "-" : Math.round(s.iso);
    document.getElementById("shutter").textContent = fmtShutter(s.shutter_ms);
    document.getElementById("bias").textContent = s.bias == null ? "-" : fmt(s.bias, 2) + " EV";
    document.getElementById("meanlum").textContent = s.mean_lum == null ? "-" : fmt(s.mean_lum, 1);
    document.getElementById("captured").textContent =
      (s.frames_captured || 0) + " / " + (s.frames_target || 0);
    document.getElementById("stacked").textContent = s.frames_stacked || 0;
    document.getElementById("output").textContent = s.output_path || "-";
    document.getElementById("updated").textContent = s.updated_at || "-";

    const pct = (s.frames_target > 0)
      ? Math.min(100, 100 * (s.frames_captured || 0) / s.frames_target)
      : 0;
    document.getElementById("bar").style.width = pct + "%";
  } catch (e) {
    connEl.textContent = "no state";
    connEl.classList.remove("live");
  }
}

setInterval(refreshState, 700);
setInterval(refreshImage, 500);
refreshState();
refreshImage();

// --- Mount jog controls (only active if /jog/info reports enabled) ---
let jogEnabled = false;
const heldKeys = new Set();
const KEY_DIR = {
  ArrowUp: "north",  ArrowDown: "south",
  ArrowLeft: "east", ArrowRight: "west",
};

async function jogInit() {
  try {
    const r = await fetch("/jog/info");
    if (!r.ok) return;
    const info = await r.json();
    if (info.enabled) {
      jogEnabled = true;
      document.getElementById("jog-section").style.display = "";
      document.getElementById("jog-panel").style.display = "";
    }
  } catch (e) {}
}

async function jogPost(path, body) {
  try {
    await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
  } catch (e) {}
}

window.addEventListener("keydown", (e) => {
  if (!jogEnabled) return;
  if (e.repeat) return;
  // Rate keys 1..9
  if (e.key >= "1" && e.key <= "9") {
    const r = parseInt(e.key);
    document.getElementById("rate").textContent = r;
    jogPost("/jog/rate", {rate: r});
    return;
  }
  if (e.key === "q" || e.key === "Q") {
    jogPost("/jog/stop-all", {});
    heldKeys.clear();
    document.getElementById("jog-status").textContent = "all stop";
    return;
  }
  const dir = KEY_DIR[e.key];
  if (!dir) return;
  e.preventDefault();
  if (heldKeys.has(e.key)) return;
  heldKeys.add(e.key);
  jogPost("/jog", {direction: dir, action: "start"});
  document.getElementById("jog-status").textContent = "moving " + dir;
});

window.addEventListener("keyup", (e) => {
  if (!jogEnabled) return;
  const dir = KEY_DIR[e.key];
  if (!dir) return;
  e.preventDefault();
  heldKeys.delete(e.key);
  jogPost("/jog", {direction: dir, action: "stop"});
  document.getElementById("jog-status").textContent = heldKeys.size ? "moving" : "idle";
});

jogInit();
</script>
</body>
</html>
"""


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
            self._send_html(_HTML)
        elif path == "/state.json":
            self._send_state()
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
        else:
            self.send_error(404)

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
