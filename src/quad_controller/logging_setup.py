"""Structured logging: JSON lines on disk with rotation, readable lines on the console.

Use :func:`log_event` to attach fields to a record::

    log_event(log, logging.INFO, "heartbeat", mode="LOITER", armed=False)

On disk that becomes one JSON object per line, which `quadctl logs` can read back.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quad_controller.config import LoggingConfig

LOG_FILE_NAME = "quad_controller.jsonl"
SYSTEM_LOG_DIR = Path("/var/log/quad_controller")
USER_LOG_DIR = Path("~/.local/state/quad_controller/logs").expanduser()


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="milliseconds")


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": _iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def format_fields(fields: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items())


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = f"{ts} {record.levelname:<7} {record.getMessage()}"
        fields = getattr(record, "fields", None)
        if fields:
            line += " " + format_fields(fields)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def resolve_log_dir(cfg: LoggingConfig) -> Path:
    if cfg.dir:
        return Path(cfg.dir).expanduser()
    if SYSTEM_LOG_DIR.is_dir() and os.access(SYSTEM_LOG_DIR, os.W_OK):
        return SYSTEM_LOG_DIR
    return USER_LOG_DIR


def setup_logging(cfg: LoggingConfig, *, console: bool | None = None) -> Path:
    """Configure the root logger. Returns the path of the JSON log file."""
    log_dir = resolve_log_dir(cfg)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(cfg.level.upper())

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(JsonLinesFormatter())
    root.addHandler(file_handler)

    if cfg.console if console is None else console:
        stream = logging.StreamHandler()
        stream.setFormatter(ConsoleFormatter())
        root.addHandler(stream)

    return log_path
