import io
import json
import logging

from quad_controller.config import LoggingConfig
from quad_controller.logging_setup import LOG_FILE_NAME, log_event, setup_logging
from quad_controller.logview import show


def test_json_lines_and_viewer(tmp_path):
    path = setup_logging(LoggingConfig(dir=str(tmp_path), console=False))
    log = logging.getLogger("quad_controller.test")
    log_event(log, logging.INFO, "status", mode="LOITER", voltage_v=12.6)
    log_event(log, logging.WARNING, "link_lost", hb_age_s=5.2)
    for h in logging.getLogger().handlers:
        h.flush()

    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    assert path.name == LOG_FILE_NAME
    assert first["event"] == "status" and first["mode"] == "LOITER" and first["level"] == "INFO"

    out = io.StringIO()
    assert show(tmp_path, out, min_level="WARNING") == 0
    assert out.getvalue().count("\n") == 1
    assert "link_lost" in out.getvalue() and "hb_age_s=5.2" in out.getvalue()


def test_rotation(tmp_path):
    setup_logging(LoggingConfig(dir=str(tmp_path), console=False, max_bytes=500, backup_count=2))
    log = logging.getLogger("quad_controller.test")
    for i in range(100):
        log_event(log, logging.INFO, "tick", i=i)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [LOG_FILE_NAME, LOG_FILE_NAME + ".1", LOG_FILE_NAME + ".2"]

    out = io.StringIO()
    show(tmp_path, out, tail=1)
    assert "i=99" in out.getvalue()


def test_viewer_shows_local_time():
    from datetime import UTC, datetime

    from quad_controller.logview import local_time

    utc = datetime(2026, 11, 27, 18, 0, 0, 123000, tzinfo=UTC)
    assert local_time(utc.isoformat(timespec="milliseconds")) == utc.astimezone().strftime(
        "%H:%M:%S.123"
    )
