"""Camera live view: frame splitting and the HTTP server, using a fake rpicam-vid."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from quad_controller.camera import (
    BOUNDARY,
    ColourSettings,
    FrameHub,
    RpicamSource,
    SettingsStore,
    serve,
    split_jpegs,
)


def fake_jpeg(n: int) -> bytes:
    # SOI, a payload with a stuffed 0xFF00 and a lone 0xFF, then EOI.
    return b"\xff\xd8" + bytes([n % 256]) * 50 + b"\xff\x00\xff" + b"\xff\xd9"


class Chunked(io.RawIOBase):
    """Return data in awkward small chunks to exercise buffer edges."""

    def __init__(self, data: bytes, size: int) -> None:
        self.data, self.size, self.pos = data, size, 0

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        out = self.data[self.pos : self.pos + min(n, self.size)]
        self.pos += len(out)
        return out


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 10_000])
def test_split_jpegs_across_chunk_boundaries(chunk):
    frames = [fake_jpeg(i) for i in range(5)]
    stream = Chunked(b"garbage" + b"".join(frames) + b"\xff\xd8partial", chunk)
    assert list(split_jpegs(stream, chunk_size=chunk)) == frames


FAKE_RPICAM = """#!{python}
import sys, time
out = sys.stdout.buffer
i = 0
while True:
    out.write(b"\\xff\\xd8" + bytes([i % 256]) * 2000 + b"\\xff\\xd9")
    out.flush()
    i += 1
    time.sleep(0.02)
"""

BROKEN_RPICAM = """#!/bin/sh
echo "ERROR: *** no cameras available ***" >&2
exit 1
"""


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body.format(python=sys.executable) if "{python}" in body else body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def server(tmp_path):
    started = []

    def start(binary_body):
        source = RpicamSource(binary=_script(tmp_path, "rpicam-vid", binary_body))
        hub = FrameHub(source, idle_s=0.3, retry_s=0.2)
        httpd = serve(hub, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append((httpd, source))
        return f"http://127.0.0.1:{httpd.server_address[1]}", hub

    yield start
    for httpd, source in started:
        httpd.shutdown()
        httpd.server_close()
        source.stop()


@pytest.mark.skipif(os.name == "nt", reason="fake camera is a shell-style script")
def test_page_snapshot_stream_and_idle_shutdown(server):
    base, hub = server(FAKE_RPICAM)

    with urllib.request.urlopen(base + "/camera/") as r:
        assert b"drone01 camera" in r.read()

    with urllib.request.urlopen(base + "/camera/snapshot.jpg", timeout=10) as r:
        assert r.headers["Content-Type"] == "image/jpeg"
        body = r.read()
    assert body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9") and len(body) == 2004

    with urllib.request.urlopen(base + "/camera/stream.mjpg", timeout=10) as r:
        assert BOUNDARY in r.headers["Content-Type"]
        data = b""
        while data.count(b"\xff\xd9") < 3:
            data += r.read1(65536)
        assert data.count(f"--{BOUNDARY}".encode()) >= 3
        status = hub.status()
        assert status["running"] and status["viewers"] == 1

    # Nobody watching: the camera process should stop shortly.
    deadline = time.monotonic() + 5
    while hub.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not hub.status()["running"]
    assert hub.status()["viewers"] == 0


@pytest.mark.skipif(os.name == "nt", reason="fake camera is a shell-style script")
def test_missing_camera_reports_error(server):
    base, hub = server(BROKEN_RPICAM)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/camera/snapshot.jpg", timeout=10)
    assert exc.value.code == 503
    assert b"no cameras available" in exc.value.read()


def test_camera_without_slash_redirects(server):
    base, _ = server(BROKEN_RPICAM)
    req = urllib.request.Request(base + "/camera")
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with opener.open(req) as r:  # follows to /camera/
        assert r.geturl().endswith("/camera/")


def test_colour_options_reach_rpicam():
    joined = " ".join(
        RpicamSource(binary="rpicam-vid", awb="indoor", tuning_file="/x/noir.json").command()
    )
    assert "--awb indoor" in joined
    assert "--tuning-file /x/noir.json" in joined
    # Fixed gains replace the white balance mode.
    joined = " ".join(RpicamSource(binary="rpicam-vid", awb="indoor", awbgains="1.5,1.2").command())
    assert "--awbgains 1.5,1.2" in joined and "--awb " not in joined
    assert "--awb" not in " ".join(RpicamSource(binary="rpicam-vid").command())


# -- colour settings ---------------------------------------------------------


def test_settings_validation():
    s = ColourSettings.from_dict({"auto_wb": False, "red": 1.7, "blue": 1.3})
    assert s.args() == ["--awbgains", "1.7,1.3"]
    assert ColourSettings().args() == []  # defaults add nothing to the command
    for bad in (
        {"red": 99},
        {"red": "1.5"},
        {"auto_wb": 1},
        {"hflip": "yes"},
        {"shutter": 100},
        [],
    ):
        with pytest.raises(ValueError):
            ColourSettings.from_dict(bad)


def test_settings_store_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "sub" / "camera.json")
    assert store.load() is None
    s = ColourSettings(auto_wb=False, red=1.6, blue=1.4, vflip=True)
    store.save(s)
    assert store.load() == s
    store.path.write_text("not json")
    assert store.load() is None  # a broken file is ignored, not fatal


ARGV_RPICAM = """#!{python}
import sys, time
open(sys.argv[0] + ".args", "a").write(" ".join(sys.argv[1:]) + "\\n")
out = sys.stdout.buffer
while True:
    out.write(b"\\xff\\xd8" + b"x" * 500 + b"\\xff\\xd9")
    out.flush()
    time.sleep(0.02)
"""


def _post(url, doc):
    req = urllib.request.Request(
        url, data=json.dumps(doc).encode(), headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="fake camera is a shell-style script")
def test_settings_apply_live_and_save(tmp_path):
    binary = _script(tmp_path, "rpicam-vid", ARGV_RPICAM)
    store = SettingsStore(tmp_path / "camera.json")
    source = RpicamSource(binary=binary)
    hub = FrameHub(source, idle_s=0.3, retry_s=0.2)
    httpd = serve(hub, "127.0.0.1", 0, store)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/camera"
    try:
        doc = json.load(urllib.request.urlopen(base + "/settings.json"))
        assert doc["current"]["auto_wb"] is True and doc["saved"] is None
        assert doc["ranges"]["red"] == [0.2, 4.0]

        # Keep a stream open so the camera is running, then change the gains.
        with urllib.request.urlopen(base + "/stream.mjpg", timeout=10) as stream:
            stream.read1(100)
            new = {"auto_wb": False, "red": 1.6, "blue": 1.4}
            doc = json.load(_post(base + "/settings.json", {"settings": new}))
            assert doc["current"]["red"] == 1.6 and doc["saved"] is None
            # The stream survives the camera restart.
            data = b""
            deadline = time.monotonic() + 5
            while data.count(b"\xff\xd9") < 5 and time.monotonic() < deadline:
                data += stream.read1(65536)
            assert data.count(b"\xff\xd9") >= 5
        runs = Path(binary + ".args").read_text().splitlines()
        assert len(runs) >= 2
        assert "--awbgains 1.6,1.4" in runs[-1] and "--awbgains" not in runs[0]
        assert hub.error == ""

        doc = json.load(_post(base + "/settings.json", {"settings": new, "save": True}))
        assert doc["saved"] == doc["current"]
        assert store.load() == ColourSettings(auto_wb=False, red=1.6, blue=1.4)

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(base + "/settings.json", {"settings": {"red": 50}})
        assert exc.value.code == 400
        assert "red must be between" in json.load(exc.value)["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        source.stop()
