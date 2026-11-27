"""Read back the JSON-lines log written by :mod:`quad_controller.logging_setup`."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from quad_controller.logging_setup import LOG_FILE_NAME

_LEVELS = {name: logging.getLevelName(name) for name in ("DEBUG", "INFO", "WARNING", "ERROR")}
_CORE = ("ts", "level", "logger", "event")


def log_files(log_dir: Path) -> list[Path]:
    """Rotated files oldest first, then the live file."""
    base = log_dir / LOG_FILE_NAME
    rotated = sorted(
        log_dir.glob(LOG_FILE_NAME + ".*"),
        key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0,
        reverse=True,
    )
    return [*rotated, base] if base.exists() else rotated


def parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return {"ts": "", "level": "RAW", "logger": "", "event": line}
    return entry if isinstance(entry, dict) else None


def matches(entry: dict[str, Any], min_level: str | None, grep: str | None) -> bool:
    if min_level:
        lvl = _LEVELS.get(str(entry.get("level")), logging.INFO)
        if lvl < _LEVELS[min_level]:
            return False
    return not (grep and grep.lower() not in json.dumps(entry).lower())


def local_time(ts: str) -> str:
    """2026-11-27T18:00:00.123+00:00 -> 13:00:00.123 in the Pi's local time zone."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return dt.astimezone().strftime("%H:%M:%S.%f")[:-3]


def render(entry: dict[str, Any]) -> str:
    short_ts = local_time(str(entry.get("ts", "")))
    extra = " ".join(f"{k}={v}" for k, v in entry.items() if k not in _CORE)
    line = f"{short_ts} {entry.get('level', ''):<7} {entry.get('event', '')}"
    return f"{line} {extra}" if extra else line


def iter_entries(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        entry = parse_line(line)
        if entry is not None:
            yield entry


def show(
    log_dir: Path,
    out: TextIO,
    *,
    min_level: str | None = None,
    grep: str | None = None,
    tail: int | None = None,
    follow: bool = False,
    poll_s: float = 0.5,
) -> int:
    files = log_files(log_dir)
    if not files and not follow:
        out.write(f"no logs in {log_dir}\n")
        return 1

    selected: list[dict[str, Any]] = []
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as fh:
            selected.extend(e for e in iter_entries(fh) if matches(e, min_level, grep))
    if tail is not None:
        selected = selected[-tail:] if tail > 0 else []
    for entry in selected:
        out.write(render(entry) + "\n")
    out.flush()

    if not follow:
        return 0

    live = log_dir / LOG_FILE_NAME
    pos = live.stat().st_size if live.exists() else 0
    try:
        while True:
            if live.exists():
                size = live.stat().st_size
                if size < pos:  # rotated
                    pos = 0
                if size > pos:
                    with live.open(encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    for entry in iter_entries(chunk.splitlines()):
                        if matches(entry, min_level, grep):
                            out.write(render(entry) + "\n")
                    out.flush()
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0
