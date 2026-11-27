"""Telemetry recorder and replay.

A recording is JSON lines: a header, then one frame per tick (10 Hz by default)::

    {"format": "quad_controller.telemetry", "version": 1, "started": "...", ...}
    {"t": 0.1, "link": "up", "state": {...VehicleState.to_dict()...}}

Files ending in ``.gz`` are gzip-compressed. Replays rebuild a
:class:`VehicleState` per frame, so tests and the TUI use the same code for
live and recorded data.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from quad_controller.config import Config
from quad_controller.logging_setup import resolve_log_dir
from quad_controller.state import VehicleState

FORMAT = "quad_controller.telemetry"
VERSION = 1


def _open(path: Path, mode: str) -> TextIO:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, mode + "b"), encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def default_recording_path(cfg: Config) -> Path:
    base = (
        Path(cfg.recorder.dir).expanduser()
        if cfg.recorder.dir
        else resolve_log_dir(cfg.logging) / "recordings"
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / f"telemetry-{cfg.target.name}-{stamp}.jsonl.gz"


class Recorder:
    """Writes the link's state to ``path`` at ``rate_hz`` until stopped."""

    def __init__(self, link, path: str | Path, rate_hz: float = 10.0, **header: Any) -> None:
        self.link = link
        self.path = Path(path)
        self.rate_hz = rate_hz
        self.header = header
        self.frames = 0
        self._task: asyncio.Task | None = None
        self._fh: TextIO | None = None

    async def __aenter__(self) -> Recorder:
        self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = _open(self.path, "w")
        head = {
            "format": FORMAT,
            "version": VERSION,
            "started": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "rate_hz": self.rate_hz,
            "connection": getattr(self.link, "connection", None),
            **self.header,
        }
        self._fh.write(json.dumps(head) + "\n")
        self._task = asyncio.create_task(self._run(), name="recorder")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write_frame(self, t: float) -> None:
        assert self._fh is not None
        frame = {
            "t": round(t, 3),
            "link": str(self.link.watchdog.status),
            "state": self.link.state.to_dict(),
        }
        self._fh.write(json.dumps(frame) + "\n")
        self.frames += 1

    async def _run(self) -> None:
        period = 1.0 / self.rate_hz
        t0 = time.monotonic()
        tick = 0
        while True:
            self.write_frame(time.monotonic() - t0)
            if tick % max(1, int(self.rate_hz)) == 0:
                self._fh.flush()  # at most ~1 s lost if the Pi loses power
            tick += 1
            # Sleep to the next tick boundary so the rate does not drift.
            await asyncio.sleep(max(0.0, t0 + tick * period - time.monotonic()))


@dataclass
class Frame:
    t: float
    link: str
    state: VehicleState
    raw: dict[str, Any]


def read_recording(path: str | Path) -> tuple[dict[str, Any], Iterator[Frame]]:
    """Return (header, frames). Frames are rebuilt lazily as you iterate."""
    path = Path(path)
    fh = _open(path, "r")
    first = fh.readline()
    try:
        header = json.loads(first)
    except json.JSONDecodeError as exc:
        fh.close()
        raise ValueError(f"{path}: not a telemetry recording") from exc
    if header.get("format") != FORMAT:
        fh.close()
        raise ValueError(f"{path}: not a telemetry recording")

    def frames() -> Iterator[Frame]:
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    return  # last line cut off by a power loss
                yield Frame(
                    t=raw["t"],
                    link=raw.get("link", "unknown"),
                    state=VehicleState.from_dict(raw["state"]),
                    raw=raw,
                )

    return header, frames()


async def replay(path: str | Path, speed: float = 1.0) -> Iterator[Frame]:
    """Async generator that yields frames in real time (scaled by ``speed``)."""
    _header, frames = read_recording(path)
    start = time.monotonic()
    for frame in frames:
        delay = start + frame.t / speed - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        yield frame
