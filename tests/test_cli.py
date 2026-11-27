import json
import logging

import pytest

from quad_controller.cli import flash_hint, main
from quad_controller.mavlink import decode_sw_version
from tests.fake_autopilot import FakeAutopilot, free_udp_port


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"logging:\n  dir: {tmp_path / 'logs'}\n  console: false\n")
    monkeypatch.setenv("QC_CONFIG", str(cfg))
    yield tmp_path / "logs"
    logging.getLogger().handlers.clear()


def events(log_dir):
    path = log_dir / "quad_controller.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_check_ok(log_dir):
    port = free_udp_port()
    with FakeAutopilot(port):
        rc = main(["check", "--connection", f"udpin:127.0.0.1:{port}", "--timeout", "5"])
    assert rc == 0
    ok = [e for e in events(log_dir) if e["event"] == "check_ok"][0]
    assert ok["mode"] == "LOITER" and ok["armed"] is False


def test_check_times_out(log_dir):
    port = free_udp_port()
    rc = main(["check", "--connection", f"udpin:127.0.0.1:{port}", "--timeout", "1"])
    assert rc == 1


def test_run_logs_status_with_battery(log_dir, monkeypatch):
    port = free_udp_port()
    with FakeAutopilot(port) as fake:
        rc = main(["run", "--connection", f"udpin:127.0.0.1:{port}", "--duration", "3"])
    assert rc == 0
    evs = events(log_dir)
    names = [e["event"] for e in evs]
    assert "link_up" in names and "status" in names
    status = [e for e in evs if e["event"] == "status"][-1]
    assert status["mode"] == "LOITER"
    assert status["voltage_v"] == 12.6 and status["battery_pct"] == 87
    # We asked for SYS_STATUS via SET_MESSAGE_INTERVAL.
    assert 511 in fake.commands


def test_firmware_info(log_dir, capsys):
    port = free_udp_port()
    with FakeAutopilot(port):
        rc = main(
            ["firmware-info", "--connection", f"udpin:127.0.0.1:{port}", "--wait", "2", "--json"]
        )
    assert rc == 0
    info = json.loads(capsys.readouterr().out)
    assert info["firmware_version"] == "4.5.7-official"
    assert info["git_hash"] == "2a3dc4b7"
    assert any("fmuv3" in line for line in info["banner"])
    assert info["flash_hint"].startswith("2 MB")


def test_firmware_info_on_arducopter_36(log_dir, capsys):
    """Pre-4.0 firmware only answers MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES."""
    port = free_udp_port()
    with FakeAutopilot(port, legacy=True):
        rc = main(
            ["firmware-info", "--connection", f"udpin:127.0.0.1:{port}", "--wait", "2", "--json"]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["firmware_version"] == "4.5.7-official"


def test_helpers():
    assert decode_sw_version((4 << 24) | (6 << 16) | (0 << 8) | 192) == "4.6.0-rc"
    assert flash_hint(["ArduCopter V4.5.7", "Pixhawk1-1M 003A0029"]).startswith("1 MB")
    assert flash_hint(["ArduCopter V4.5.7", "Pixhawk1 003A0029"]).startswith("2 MB")
    assert flash_hint(["ArduCopter V4.5.7"]).startswith("unknown")


def test_check_over_serial_port(log_dir):
    """The Pixhawk link is a serial port; make sure that path works (needs pyserial)."""
    import os
    import threading
    import time

    from tests.fake_autopilot import COPTER_LOITER, mavlink

    master, slave = os.openpty()
    stop = threading.Event()

    def feed():
        mav = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        while not stop.is_set():
            msg = mav.heartbeat_encode(
                mavlink.MAV_TYPE_QUADROTOR,
                mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                COPTER_LOITER,
                mavlink.MAV_STATE_STANDBY,
            )
            os.write(master, msg.pack(mav))
            time.sleep(0.1)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        rc = main(["check", "--connection", os.ttyname(slave), "--timeout", "5"])
    finally:
        stop.set()
        t.join(timeout=2)
        os.close(master)
        os.close(slave)
    assert rc == 0
