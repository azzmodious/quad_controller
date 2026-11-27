"""Phase 1: state model, watchdog, command helper, link layer, recorder, TUI."""

from __future__ import annotations

import asyncio
import json
import math
import time

import pytest
from rich.console import Console

from quad_controller.cli import main
from quad_controller.commands import CommandBlocked, CommandSender, flight_commands_allowed
from quad_controller.config import ConfigError, load_config
from quad_controller.link import MavLink
from quad_controller.recorder import Recorder, read_recording
from quad_controller.state import VehicleState
from quad_controller.tui import LinkInfo, compass_tape, horizon, horizon_mask, render
from quad_controller.watchdog import LinkStatus, LinkWatchdog
from tests.fake_autopilot import FakeAutopilot, free_udp_port, mavlink

MAV = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)


def _decode(msg):
    """Round-trip through bytes so the message looks like a received one."""
    return mavlink.MAVLink(None).decode(bytearray(msg.pack(MAV)))


# ----- state model ----------------------------------------------------------


def test_state_applies_messages_with_timestamps():
    s = VehicleState()
    att = MAV.attitude_encode(0, math.radians(10), math.radians(-5), math.radians(-90), 0, 0, 0)
    assert s.apply(_decode(att), now=100.0) == "attitude"
    assert s.attitude.roll_deg == 10.0 and s.attitude.pitch_deg == -5.0
    assert s.attitude.heading_deg == 270.0
    assert s.attitude.age(now=100.2) == pytest.approx(0.2)
    assert not s.attitude.is_stale(now=100.2)
    assert s.attitude.is_stale(now=101.0)  # attitude goes stale after 0.5 s
    assert s.gps.is_stale(now=100.0)  # never received

    gps = MAV.gps_raw_int_encode(0, 3, -353632610, 1491652300, 584000, 80, 120, 0, 0, 14)
    s.apply(_decode(gps), now=100.0)
    assert s.gps.fix_name == "3D" and s.gps.satellites == 14 and s.gps.hdop == 0.8
    assert s.gps.lat == pytest.approx(-35.363261)

    sys_status = MAV.sys_status_encode(0, 0, 0, 500, 0xFFFF, -1, -1, 0, 0, 0, 0, 0, 0)
    s.apply(_decode(sys_status), now=100.0)
    assert s.battery.voltage_v is None and s.battery.current_a is None

    ekf = MAV.ekf_status_report_encode(0x33F, 0.1, 0.2, 0.1, 0.05, 0.0)
    s.apply(_decode(ekf), now=100.0)
    assert s.ekf.attitude_ok and s.ekf.position_ok

    # Heartbeats from a GCS are not the autopilot.
    gcs = MAV.heartbeat_encode(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    assert s.apply(_decode(gcs), now=100.0) is None
    assert s.heartbeat.count == 0


def test_state_round_trips_through_dict():
    s = VehicleState()
    att = MAV.attitude_encode(0, math.radians(20), 0, 0, 0, 0, 0)
    s.apply(_decode(att), now=50.0)
    s.apply(_decode(MAV.statustext_encode(4, b"PreArm: test")), now=49.0)
    data = json.loads(json.dumps(s.to_dict(now=51.0)))
    back = VehicleState.from_dict(data, now=200.0)
    assert back.attitude.roll_deg == 20.0
    assert back.attitude.age(now=200.0) == pytest.approx(1.0)
    assert back.gps.t is None
    assert back.messages[-1].text == "PreArm: test"


# ----- watchdog -------------------------------------------------------------


def test_watchdog_thresholds_and_events():
    clock = [0.0]
    wd = LinkWatchdog(1.0, 3.0, clock=lambda: clock[0])
    events = []
    wd.subscribe(events.append)
    assert wd.check() is None and wd.status is LinkStatus.WAITING

    wd.heartbeat_received()
    wd.heartbeat_sent()
    assert wd.check().name == "link_up"
    clock[0] = 1.5
    assert wd.check().status is LinkStatus.DEGRADED
    clock[0] = 3.2
    ev = wd.check()
    assert ev.name == "link_lost" and ev.rx_age_s == pytest.approx(3.2)
    wd.heartbeat_received()
    wd.heartbeat_sent()
    assert wd.check().name == "link_restored"
    assert [e.name for e in events] == ["link_up", "link_degraded", "link_lost", "link_restored"]


def test_watchdog_counts_our_own_heartbeat():
    clock = [0.0]
    wd = LinkWatchdog(1.0, 3.0, clock=lambda: clock[0], tx_period_s=1.0)
    wd.heartbeat_sent()
    clock[0] = 1.9
    wd.heartbeat_received()
    assert wd.check().status is LinkStatus.UP  # 0.9 s late is fine at 1 Hz
    clock[0] = 4.5
    wd.message_received()  # autopilot fine, but our heartbeat is 3.5 s late
    assert wd.check().status is LinkStatus.LOST
    assert "our heartbeat" in wd.evaluate()[1]


def test_watchdog_streams_keep_link_fresh_between_heartbeats():
    clock = [0.0]
    wd = LinkWatchdog(1.0, 3.0, clock=lambda: clock[0])
    wd.message_received()  # ignored until the autopilot has been identified
    assert wd.last_rx is None
    wd.heartbeat_received()
    wd.heartbeat_sent()
    wd.check()
    for _ in range(15):  # 1.5 s of 10 Hz attitude, no new heartbeat
        clock[0] += 0.1
        wd.message_received()
        wd.heartbeat_sent()
        assert wd.check() is None


# ----- link layer against the fake autopilot --------------------------------


def _link(port, **kw) -> MavLink:
    defaults = {"streams": {"ATTITUDE": 20, "SYS_STATUS": 2}}
    return MavLink(f"udpin:127.0.0.1:{port}", **{**defaults, **kw})


async def _until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def test_link_tracks_state_and_requests_streams():
    port = free_udp_port()

    async def go():
        with FakeAutopilot(port, ack=True) as fake:
            link = _link(port)
            CommandSender(link)
            async with link:
                assert await link.wait_connected(5)
                fake.roll_deg, fake.pitch_deg = 15.0, -8.0
                await _until(lambda: link.state.attitude.roll_deg == 15.0)
                await _until(lambda: fake.commands.count(511) >= 2)
                assert link.state.attitude.pitch_deg == -8.0
                assert link.state.heartbeat.mode == "LOITER"
                assert link.state.gps.satellites == 12
                assert link.state.battery.voltage_v == 12.6
                assert link.state.rc.channels[:4] == [1500] * 4
                await _until(lambda: len(link.state.messages) > 0)
                assert link.state.messages[-1].text == "Fake ArduCopter ready"
                assert link.status is LinkStatus.UP
                await _until(lambda: fake.gcs_heartbeats >= 1)
            assert not fake.data_streams  # modern firmware: no legacy fallback

    asyncio.run(go())


def test_link_falls_back_to_request_data_stream_on_old_firmware():
    port = free_udp_port()

    async def go():
        with FakeAutopilot(port, ack=True, legacy=True) as fake:
            link = _link(port)
            CommandSender(link)
            async with link:
                assert await link.wait_connected(5)
                await _until(lambda: len(fake.data_streams) >= 2)
            assert mavlink.MAV_DATA_STREAM_EXTRA1 in fake.data_streams
            assert mavlink.MAV_DATA_STREAM_EXTENDED_STATUS in fake.data_streams

    asyncio.run(go())


def test_watchdog_fires_and_recovers_when_autopilot_goes_silent():
    port = free_udp_port()

    async def go():
        with FakeAutopilot(port) as fake:
            link = _link(port, degraded_after_s=0.5, lost_after_s=1.5)
            seen = []
            link.watchdog.subscribe(lambda e: seen.append((e.name, time.monotonic())))
            async with link:
                assert await link.wait_connected(5)
                fake.paused.set()
                t0 = time.monotonic()
                await _until(lambda: link.status is LinkStatus.LOST, timeout=4)
                assert time.monotonic() - t0 < 2.5
                fake.paused.clear()
                await _until(lambda: link.status is LinkStatus.UP, timeout=4)
            names = [n for n, _ in seen]
            assert names == ["link_up", "link_degraded", "link_lost", "link_restored"]

    asyncio.run(go())


# ----- command helper -------------------------------------------------------


def test_command_retries_until_ack():
    port = free_udp_port()

    async def go():
        with FakeAutopilot(port, ack=True, drop_first=2) as fake:
            link = _link(port, streams={})
            cmds = CommandSender(link)
            async with link:
                assert await link.wait_connected(5)
                res = await cmds.send(
                    mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES, 1, retries=3, timeout_s=0.4
                )
            assert res.ok and res.attempts == 3 and res.result_name == "ACCEPTED"
            confirms = [c for cmd, c in fake.confirmations if cmd == 520]
            assert confirms == [0, 1, 2]

    asyncio.run(go())


def test_command_reports_no_ack():
    port = free_udp_port()

    async def go():
        with FakeAutopilot(port, ack=False):
            link = _link(port, streams={})
            cmds = CommandSender(link)
            async with link:
                assert await link.wait_connected(5)
                res = await cmds.send(520, 1, retries=1, timeout_s=0.2)
            assert not res.ok and res.result is None and res.result_name == "NO_ACK"
            assert res.attempts == 2

    asyncio.run(go())


def test_flight_commands_are_blocked_by_default():
    async def go():
        link = MavLink("udpin:127.0.0.1:1")
        cmds = CommandSender(link)
        with pytest.raises(CommandBlocked):
            await cmds.arm()
        with pytest.raises(CommandBlocked):
            await cmds.set_mode("GUIDED")

    asyncio.run(go())


def test_flight_commands_only_allowed_on_sitl(tmp_path):
    assert flight_commands_allowed(load_config(target="sitl", env={}))
    assert not flight_commands_allowed(load_config(target="hardware", env={}))
    # A serial port is real hardware even if someone labels it sitl.
    assert not flight_commands_allowed(
        load_config(target="sitl", connection="/dev/ttyACM0", env={})
    )


# ----- recorder and replay --------------------------------------------------


def test_record_and_replay(tmp_path):
    port = free_udp_port()
    path = tmp_path / "rec.jsonl.gz"

    async def go():
        with FakeAutopilot(port) as fake:
            fake.roll_deg = 30.0
            link = _link(port)
            async with link:
                assert await link.wait_connected(5)
                await _until(lambda: link.state.attitude.roll_deg == 30.0)
                async with Recorder(link, path, rate_hz=10, target="test") as rec:
                    await asyncio.sleep(1.05)
            return rec.frames

    frames_written = asyncio.run(go())
    assert 9 <= frames_written <= 13
    header, frames = read_recording(path)
    assert header["format"] == "quad_controller.telemetry" and header["target"] == "test"
    frames = list(frames)
    assert len(frames) == frames_written
    assert frames[-1].t == pytest.approx(frames_written / 10 - 0.1, abs=0.06)
    assert all(f.state.attitude.roll_deg == 30.0 for f in frames)
    assert frames[0].link == "up"


def test_replay_tolerates_truncated_last_line(tmp_path):
    path = tmp_path / "rec.jsonl"
    state = VehicleState().to_dict(now=0)
    path.write_text(
        json.dumps({"format": "quad_controller.telemetry", "version": 1})
        + "\n"
        + json.dumps({"t": 0.0, "link": "up", "state": state})
        + '\n{"t": 0.1, "li'
    )
    _h, frames = read_recording(path)
    assert len(list(frames)) == 1


def test_recorder_rejects_other_files(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"hello": 1}\n')
    with pytest.raises(ValueError):
        read_recording(p)


# ----- terminal view --------------------------------------------------------


def _text(renderable, width=100) -> str:
    import io

    console = Console(width=width, record=True, color_system=None, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_render_shows_values_and_staleness():
    s = VehicleState()
    s.apply(_decode(MAV.attitude_encode(0, math.radians(12.3), math.radians(-4), 0, 0, 0, 0)), 10)
    s.apply(_decode(MAV.sys_status_encode(0, 0, 0, 500, 12600, 150, 87, 0, 0, 0, 0, 0, 0)), 10)
    out = _text(render(s, LinkInfo(status="up", target="sitl"), now=10.1))
    assert "+12.3°" in out and "-4.0°" in out
    assert "12.60 V" in out and "87%" in out
    assert "LINK UP" in out
    assert "no data" in out  # GPS never arrived
    stale = _text(render(s, LinkInfo(status="degraded"), now=12.0))
    assert "stale 2.0s" in stale
    narrow = _text(render(s, LinkInfo(), now=10.1, width=60), width=60)
    assert "+12.3°" in narrow


def test_horizon_tilts_the_right_way():
    level = horizon_mask(0, 0)
    assert not any(level[4]) and all(level[6])  # sky above the middle, ground below
    right = horizon_mask(30, 0)
    # Roll right: the horizon rises on the right, so the right edge just above the
    # middle shows ground and the left edge just below it shows sky.
    assert right[4][-1] and not right[4][0]
    assert not right[6][0] and right[6][-1]
    nose_up = horizon_mask(0, 10)
    assert not nose_up[6][14]  # more sky
    assert horizon(None, None).plain.count("\n") == 10


def test_compass_tape_centres_heading():
    assert compass_tape(0).plain.split("\n")[0][18] == "N"
    assert compass_tape(90).plain.split("\n")[0][18] == "E"
    assert "273.0°" in compass_tape(273).plain


# ----- CLI ------------------------------------------------------------------


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    import logging

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        f"logging:\n  dir: {tmp_path / 'logs'}\n  console: false\n"
        f"recorder:\n  dir: {tmp_path / 'rec'}\n"
    )
    monkeypatch.setenv("QC_CONFIG", str(cfg))
    yield tmp_path
    logging.getLogger().handlers.clear()


def test_cli_record_then_replay(cfg_file, capsys):
    port = free_udp_port()
    out = cfg_file / "r.jsonl"
    with FakeAutopilot(port):
        rc = main(
            [
                "record",
                "--connection",
                f"udpin:127.0.0.1:{port}",
                "--duration",
                "1",
                "--out",
                str(out),
            ]
        )
    assert rc == 0 and out.exists()
    assert main(["replay", str(out), "--speed", "5", "--no-screen"]) == 0
    assert "REPLAY" in capsys.readouterr().out


def test_cli_telemetry_runs_and_records(cfg_file, capsys):
    port = free_udp_port()
    with FakeAutopilot(port):
        rc = main(
            [
                "telemetry",
                "--connection",
                f"udpin:127.0.0.1:{port}",
                "--duration",
                "1.5",
                "--no-screen",
                "--record",
            ]
        )
    assert rc == 0
    recs = list((cfg_file / "rec").glob("telemetry-*.jsonl.gz"))
    assert len(recs) == 1
    out = capsys.readouterr().out
    assert "LOITER" in out and "Saved" in out


def test_run_logs_attitude_and_link(cfg_file):
    port = free_udp_port()
    with FakeAutopilot(port) as fake:
        fake.roll_deg = 7.0
        assert main(["run", "--connection", f"udpin:127.0.0.1:{port}", "--duration", "2.5"]) == 0
    lines = (cfg_file / "logs" / "quad_controller.jsonl").read_text().splitlines()
    status = [json.loads(x) for x in lines if '"status"' in x][-1]
    assert status["roll_deg"] == 7.0 and status["link"] == "up" and status["sats"] == 12


def test_link_config_validation(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("link:\n  degraded_after_s: 3\n  lost_after_s: 1\n")
    with pytest.raises(ConfigError):
        load_config(f, env={})
    f.write_text("link:\n  streams:\n    attitude: 50\n")
    streams = load_config(f, env={}).link.streams
    assert streams["ATTITUDE"] == 50.0 and streams["SYS_STATUS"] == 2.0  # merged
    assert load_config(env={}).link.lost_after_s == 3.0
