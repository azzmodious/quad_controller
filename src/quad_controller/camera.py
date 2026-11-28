"""Pi Camera live view: an MJPEG stream and snapshots over HTTP.

A first, small step toward Phase 3. It needs no extra Python packages: frames
come from `rpicam-vid --codec mjpeg` (part of Raspberry Pi OS) and are served
with the standard library's HTTP server.

    python -m quad_controller.camera            # http://localhost:8000/camera

The Cloudflare tunnel already sends drone01.<domain> to localhost:8000, behind
Cloudflare Access, so the page is reachable from anywhere you can sign in.

The camera only runs while someone is watching: it starts on the first viewer
and stops a few seconds after the last one leaves.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO

log = logging.getLogger("quad_controller.camera")

SOI = b"\xff\xd8"  # JPEG start of image
EOI = b"\xff\xd9"  # JPEG end of image
BOUNDARY = "qcframe"


def split_jpegs(stream: BinaryIO, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield complete JPEG images from a byte stream of back-to-back JPEGs.

    Inside JPEG entropy-coded data every 0xFF byte is stuffed with 0x00, so the
    two-byte EOI marker only appears at the real end of an image.
    """
    buf = bytearray()
    while True:
        data = stream.read1(chunk_size) if hasattr(stream, "read1") else stream.read(chunk_size)
        if not data:
            return
        buf += data
        while True:
            start = buf.find(SOI)
            if start < 0:
                # Keep a trailing 0xFF in case it is the first half of a marker.
                del buf[: max(0, len(buf) - 1)]
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                if start:
                    del buf[:start]
                break
            yield bytes(buf[start : end + 2])
            del buf[: end + 2]


def find_camera_binary() -> str | None:
    """rpicam-vid on current Raspberry Pi OS, libcamera-vid on older (Bullseye) images."""
    return shutil.which("rpicam-vid") or shutil.which("libcamera-vid")


NOIR_TUNING = [
    "/usr/share/libcamera/ipa/rpi/pisp/ov5647_noir.json",  # Pi 5
    "/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json",  # Pi 4 and earlier
]


def noir_tuning_file() -> str | None:
    """The NoIR OV5647 tuning file for this Pi, if installed."""
    from pathlib import Path

    return next((p for p in NOIR_TUNING if Path(p).exists()), None)


class RpicamSource:
    """Runs rpicam-vid and yields JPEG frames until stopped or the process exits."""

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: int = 15,
        quality: int = 80,
        hflip: bool = False,
        vflip: bool = False,
        binary: str | None = None,
        awb: str | None = None,
        awbgains: str | None = None,
        tuning_file: str | None = None,
    ) -> None:
        self.width, self.height, self.fps, self.quality = width, height, fps, quality
        self.hflip, self.vflip = hflip, vflip
        self.binary = binary
        self.awb, self.awbgains, self.tuning_file = awb, awbgains, tuning_file
        self._proc: subprocess.Popen | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)

    def command(self) -> list[str]:
        binary = self.binary or find_camera_binary()
        if not binary:
            raise RuntimeError("rpicam-vid not found (sudo apt install rpicam-apps)")
        cmd = [
            binary,
            "--timeout", "0",
            "--nopreview",
            "--codec", "mjpeg",
            "--width", str(self.width),
            "--height", str(self.height),
            "--framerate", str(self.fps),
            "--quality", str(self.quality),
            "--output", "-",
        ]  # fmt: skip
        if self.hflip:
            cmd.append("--hflip")
        if self.vflip:
            cmd.append("--vflip")
        if self.awb:
            cmd += ["--awb", self.awb]
        if self.awbgains:
            cmd += ["--awbgains", self.awbgains]
        if self.tuning_file:
            cmd += ["--tuning-file", self.tuning_file]
        return cmd

    def frames(self) -> Iterator[bytes]:
        cmd = self.command()
        log.info("starting camera: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()
        try:
            yield from split_jpegs(self._proc.stdout)
        finally:
            self.stop()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                log.debug("rpicam: %s", line)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def last_error(self) -> str:
        """The most useful recent stderr line, for the status page."""
        for line in reversed(self.stderr_tail):
            if "ERROR" in line or "error" in line or "failed" in line.lower():
                return line
        return self.stderr_tail[-1] if self.stderr_tail else ""


class FrameHub:
    """Shares the latest frame with any number of viewers.

    The reader thread starts with the first viewer and stops `idle_s` after the
    last one leaves, so the camera is off when nobody is looking.
    """

    def __init__(self, source, idle_s: float = 5.0, retry_s: float = 2.0) -> None:
        self.source = source
        self.idle_s = idle_s
        self.retry_s = retry_s
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._last_release = time.monotonic()
        self._thread: threading.Thread | None = None
        self._stamps: deque[float] = deque(maxlen=30)
        self.error = ""
        self.started_at: float | None = None

    # -- viewers -----------------------------------------------------------
    def acquire(self) -> None:
        with self._cond:
            self._viewers += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
                self._thread.start()

    def release(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            self._last_release = time.monotonic()

    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, bytes] | None:
        """Block until a frame newer than `after_seq` arrives; None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after_seq:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._seq, self._frame

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        with self._cond:
            stamps = list(self._stamps)
            fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
            if stamps and time.monotonic() - stamps[-1] > 2:
                fps = 0.0
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "viewers": self._viewers,
                "frames": self._seq,
                "fps": round(fps, 1),
                "frame_bytes": len(self._frame) if self._frame else 0,
                "width": getattr(self.source, "width", None),
                "height": getattr(self.source, "height", None),
                "error": self.error,
            }

    # -- reader thread -----------------------------------------------------
    def _idle(self) -> bool:
        with self._cond:
            return self._viewers == 0 and time.monotonic() - self._last_release > self.idle_s

    def _run(self) -> None:
        while not self._idle():
            got_frame = False
            try:
                for frame in self.source.frames():
                    got_frame = True
                    with self._cond:
                        self._frame = frame
                        self._seq += 1
                        self._stamps.append(time.monotonic())
                        self.error = ""
                        self._cond.notify_all()
                    if self._idle():
                        break
                else:
                    err = getattr(self.source, "last_error", lambda: "")()
                    self.error = err or "camera process exited"
            except Exception as exc:  # noqa: BLE001 - report anything to the page
                self.error = str(exc)
            finally:
                stop = getattr(self.source, "stop", None)
                if stop:
                    stop()
            if self.error:
                log.warning("camera: %s", self.error)
            if not got_frame and not self._idle():
                time.sleep(self.retry_s)
        log.info("camera stopped (no viewers)")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>drone01 camera</title>
<style>
:root { --bg:#0d1117; --panel:#161b22; --text:#e6edf3; --muted:#8b949e; --ok:#3fb950;
        --bad:#f85149; --accent:#58a6ff; --line:#30363d; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
header { display:flex; align-items:center; gap:10px; padding:12px 16px;
         border-bottom:1px solid var(--line); }
header h1 { font-size:16px; margin:0; font-weight:600; white-space:nowrap; }
.dot { width:10px; height:10px; border-radius:50%; background:var(--muted); }
.dot.live { background:var(--ok); box-shadow:0 0 8px var(--ok); }
.dot.err { background:var(--bad); }
#state { color:var(--muted); font-size:13px; margin-left:auto; font-variant-numeric:tabular-nums; }
main { max-width:1100px; margin:0 auto; padding:16px; }
.view { position:relative; background:#000; border:1px solid var(--line); border-radius:10px;
        overflow:hidden; aspect-ratio:16/9; display:flex; align-items:center;
        justify-content:center; }
.view img { width:100%; height:100%; object-fit:contain; display:block; }
#msg { position:absolute; color:var(--muted); padding:16px; text-align:center; }
.bar { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
button, a.btn { background:var(--panel); color:var(--text); border:1px solid var(--line);
       border-radius:8px; padding:8px 14px; font:inherit; cursor:pointer; text-decoration:none; }
button:hover, a.btn:hover { border-color:var(--accent); }
button.on { border-color:var(--accent); color:var(--accent); }
#err { color:var(--bad); margin-top:10px; font-size:13px; white-space:pre-wrap; }
footer { color:var(--muted); font-size:12px; margin-top:16px; }
</style></head><body>
<header><span class="dot" id="dot"></span><h1>drone01 camera</h1>
  <span id="state">connecting</span></header>
<main>
  <div class="view" id="view"><img id="img" alt="Camera view">
    <div id="msg">Starting camera</div></div>
  <div class="bar">
    <button id="live" class="on">Live</button>
    <button id="slow">Low bandwidth (1 fps)</button>
    <a class="btn" id="snap" href="snapshot.jpg" download>Save snapshot</a>
    <button id="full">Full screen</button>
  </div>
  <div id="err"></div>
  <footer>Phase 3 preview. The camera turns off a few seconds after you close this page.</footer>
</main>
<script>
const img = document.getElementById('img'), msg = document.getElementById('msg');
let mode = 'live', timer = null;
function stamp() { const d = new Date(); return d.toISOString().slice(0,19).replace(/[:T]/g,'-'); }
document.getElementById('snap').addEventListener('click', e => {
  e.currentTarget.download = 'drone01-' + stamp() + '.jpg';
});
img.onload = () => {
  msg.style.display = 'none'; if (mode === 'slow') timer = setTimeout(pull, 1000);
};
img.onerror = () => { msg.style.display = ''; msg.textContent = 'No picture yet, retrying';
  clearTimeout(timer); timer = setTimeout(start, 2000); };
function pull() { img.src = 'snapshot.jpg?t=' + Date.now(); }
function start() {
  clearTimeout(timer);
  if (mode === 'live') img.src = 'stream.mjpg?t=' + Date.now(); else pull();
}
function setMode(m) {
  mode = m;
  document.getElementById('live').classList.toggle('on', m === 'live');
  document.getElementById('slow').classList.toggle('on', m === 'slow');
  img.src = ''; start();
}
document.getElementById('live').onclick = () => setMode('live');
document.getElementById('slow').onclick = () => setMode('slow');
document.getElementById('full').onclick = () => {
  const v = document.getElementById('view');
  (v.requestFullscreen || v.webkitRequestFullscreen).call(v);
};
async function poll() {
  try {
    const s = await (await fetch('status.json', {cache: 'no-store'})).json();
    const dot = document.getElementById('dot');
    dot.className = 'dot' + (s.error ? ' err' : s.fps > 0 ? ' live' : '');
    document.getElementById('state').textContent = s.fps > 0
      ? `${s.width}x${s.height} · ${s.fps} fps · ${Math.round(s.frame_bytes/1024)} KB/frame · `
        + `${s.viewers} viewer${s.viewers === 1 ? '' : 's'}`
      : (s.running ? 'starting' : 'idle');
    document.getElementById('err').textContent = s.error ? 'Camera: ' + s.error : '';
  } catch (e) { document.getElementById('state').textContent = 'server unreachable'; }
}
start(); poll(); setInterval(poll, 2000);
</script></body></html>
"""


def make_handler(hub: FrameHub, frame_timeout: float = 10.0) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "quad-camera/0.1"

        def log_message(self, fmt, *args) -> None:  # route to our logger, not stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            raw = self.path.split("?", 1)[0]
            if raw == "/camera":  # the page uses relative links, so it needs the slash
                self._send(
                    HTTPStatus.MOVED_PERMANENTLY, b"", "text/plain", {"Location": "/camera/"}
                )
                return
            path = raw.rstrip("/") or "/"
            routes: dict[str, Callable[[], None]] = {
                "/": lambda: self._send(
                    HTTPStatus.FOUND, b"", "text/plain", {"Location": "/camera/"}
                ),
                "/camera": lambda: self._send(
                    HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8"
                ),
                "/camera/stream.mjpg": self._stream,
                "/camera/snapshot.jpg": self._snapshot,
                "/camera/status.json": lambda: self._send(
                    HTTPStatus.OK, json.dumps(hub.status()).encode(), "application/json"
                ),
                "/healthz": lambda: self._send(HTTPStatus.OK, b"ok\n", "text/plain"),
            }
            route = routes.get(path)
            if route is None:
                self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
            else:
                route()

        def _snapshot(self) -> None:
            hub.acquire()
            try:
                # Wait for a frame newer than whatever we have, so it is fresh.
                got = hub.wait_frame(hub.status()["frames"], frame_timeout)
            finally:
                hub.release()
            if got is None:
                err = hub.error or "no frame from the camera yet"
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, (err + "\n").encode(), "text/plain")
                return
            self._send(HTTPStatus.OK, got[1], "image/jpeg")

        def _stream(self) -> None:
            hub.acquire()
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.send_header("Cache-Control", "no-store")
                # Stop proxies (Cloudflare included) from buffering the stream.
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                if self.command == "HEAD":
                    return
                seq = 0
                while True:
                    got = hub.wait_frame(seq, frame_timeout)
                    if got is None:
                        if hub.error:
                            break
                        continue
                    seq, frame = got
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # viewer closed the tab
            finally:
                hub.release()

    return Handler


def serve(hub: FrameHub, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(hub))
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m quad_controller.camera", description=__doc__.splitlines()[0]
    )
    p.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: local only, for the tunnel)"
    )
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100")
    p.add_argument("--hflip", action="store_true", help="mirror left/right")
    p.add_argument("--vflip", action="store_true", help="flip upside down")
    p.add_argument(
        "--awb",
        choices=["auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy"],
        help="white balance mode (fixes a colour tint under some lights)",
    )
    p.add_argument(
        "--awbgains", metavar="RED,BLUE", help="fixed red,blue gains, e.g. 1.5,1.2 (turns off auto)"
    )
    p.add_argument(
        "--noir",
        action="store_true",
        help="colour profile for NoIR cameras (no infrared filter, often look blue or pink)",
    )
    p.add_argument("--tuning-file", help="libcamera tuning JSON (overrides --noir)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    source = RpicamSource(
        args.width,
        args.height,
        args.fps,
        args.quality,
        hflip=args.hflip,
        vflip=args.vflip,
        awb=args.awb,
        awbgains=args.awbgains,
        tuning_file=args.tuning_file or (noir_tuning_file() if args.noir else None),
    )
    if not find_camera_binary():
        log.error("rpicam-vid not found. On Raspberry Pi OS: sudo apt install rpicam-apps")
        return 1
    hub = FrameHub(source)
    try:
        httpd = serve(hub, args.host, args.port)
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", args.host, args.port, exc)
        return 1
    log.info("camera page on http://%s:%d/camera/  (Ctrl+C to stop)", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
