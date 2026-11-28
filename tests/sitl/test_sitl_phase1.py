"""Phase 1 against a live ArduCopter SITL (started by scripts/sitl.sh).

1. Our state matches what an independent connection sees (MAVProxy's second port).
2. Take off, fly a circle, and check attitude, heading and altitude follow.
3. Record the flight and replay it.
4. Freeze SITL (SIGSTOP) and check the watchdog reports the link lost within 3 s,
   then resume it and check the link comes back by itself.

Flight commands are allowed here only because the target is SITL.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
import time
from pathlib import Path

import pytest

from quad_controller.commands import CommandSender, flight_commands_allowed
from quad_controller.config import load_config
from quad_controller.link import MavLink
from quad_controller.recorder import Recorder, read_recording
from quad_controller.watchdog import LinkStatus

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.skipif(os.environ.get("QC_SITL") != "1", reason="set QC_SITL=1 with SITL running"),
]

ROOT = Path(__file__).resolve().parents[2]
GCS_PORT = "udpin:127.0.0.1:14551"  # MAVProxy's second output


async def _until(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.1)


def _independent_sample(kinds=("ATTITUDE", "GLOBAL_POSITION_INT"), timeout=5.0):
    """Read the newest of each message on MAVProxy's GCS port with plain pymavlink."""
    from pymavlink import mavutil

    conn = mavutil.mavlink_connection(GCS_PORT, source_system=250)
    got = {}
    deadline = time.monotonic() + timeout
    try:
        while len(got) < len(kinds) and time.monotonic() < deadline:
            msg = conn.recv_match(type=list(kinds), blocking=True, timeout=0.5)
            if msg is not None:
                got[msg.get_type()] = (time.monotonic(), msg)
    finally:
        conn.close()
    return got


async def _fly_circle(link, speed=3.0, turn_dps=20.0):
    """GUIDED velocity setpoints whose direction turns at turn_dps (radius ~8.6 m).

    Guided velocity commands time out after a few seconds, so keep sending at 5 Hz.
    """
    from pymavlink import mavutil

    ignore_pos_acc_yawrate = 0b1001_1100_0111
    t0 = time.monotonic()
    while True:
        direction = math.radians((time.monotonic() - t0) * turn_dps)
        vn, ve = speed * math.cos(direction), speed * math.sin(direction)
        link.send(
            lambda m, vn=vn, ve=ve, yaw=direction: m.set_position_target_local_ned_send(
                0,
                link.target_system,
                link.target_component,
                mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                ignore_pos_acc_yawrate,
                0,
                0,
                0,
                vn,
                ve,
                0,
                0,
                0,
                0,
                yaw,
                0,
            )
        )
        await asyncio.sleep(0.2)


def _angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def test_phase1_sitl_flight_record_and_watchdog(tmp_path):
    cfg = load_config(target="sitl", env={})
    assert flight_commands_allowed(cfg)
    asyncio.run(_flight(cfg, tmp_path))


async def _flight(cfg, tmp_path):
    link = MavLink.from_config(cfg)
    cmds = CommandSender(link, allow_flight_commands=True)
    events = []
    link.watchdog.subscribe(lambda e: events.append((time.monotonic(), e)))
    rec_path = tmp_path / "flight.jsonl.gz"
    async with link:
        assert await link.wait_connected(10), "no heartbeat from SITL"
        s = link.state
        await _until(lambda: not s.attitude.is_stale(), 10, "ATTITUDE stream")
        await _until(lambda: s.gps.has_3d_fix, 60, "GPS 3D fix")
        await _until(lambda: s.ekf.position_ok, 90, "EKF position estimate")

        # 1. Our state agrees with an independent reader of the same vehicle.
        sample = await asyncio.to_thread(_independent_sample)
        _t, att = sample["ATTITUDE"]
        assert abs(s.attitude.roll_deg - math.degrees(att.roll)) < 2.0
        assert abs(s.attitude.pitch_deg - math.degrees(att.pitch)) < 2.0
        assert _angle_diff(s.attitude.heading_deg, math.degrees(att.yaw) % 360) < 3.0
        _t, pos = sample["GLOBAL_POSITION_INT"]
        assert abs(s.position.lat - pos.lat / 1e7) < 1e-5
        assert abs(s.position.alt_rel_m - pos.relative_alt / 1000) < 0.5
        assert s.battery.voltage_v and s.battery.voltage_v > 10
        assert len(s.rc.channels) >= 8

        # 2. Take off and fly a circle, recording it.
        async with Recorder(link, rec_path, rate_hz=10, target="sitl"):
            assert (await cmds.set_mode("GUIDED")).ok
            await _until(lambda: s.heartbeat.mode == "GUIDED", 5, "GUIDED mode")
            armed = None
            for _ in range(30):  # pre-arm checks may need a few more seconds
                armed = await cmds.arm(True)
                if armed.ok:
                    break
                await asyncio.sleep(2)
            assert armed is not None and armed.ok, f"arm refused: {armed.result_name}"
            await _until(lambda: s.heartbeat.armed, 5, "armed flag")
            assert (await cmds.takeoff(10)).ok
            await _until(lambda: (s.position.alt_rel_m or 0) > 9.0, 40, "takeoff to 10 m")

            # Fly the circle ourselves in GUIDED with velocity setpoints. (CIRCLE mode
            # takes climb rate from the throttle stick, which SITL leaves at 1000, and
            # the RC-override workaround depends on a param renamed across versions.)
            circling = asyncio.create_task(_fly_circle(link))
            headings, rolls, lats = [], [], []
            t_end = time.monotonic() + 25
            while time.monotonic() < t_end:
                headings.append(s.attitude.heading_deg)
                rolls.append(abs(s.attitude.roll_deg))
                lats.append(s.position.lat)
                await asyncio.sleep(0.2)
            assert not s.attitude.is_stale() and not s.position.is_stale()
            turned = sum(_angle_diff(a, b) for a, b in zip(headings, headings[1:], strict=False))
            assert turned > 120, f"heading only changed {turned:.0f} deg while circling"
            assert max(rolls) > 2.0, "no bank while circling"
            assert max(lats) - min(lats) > 2e-5, "position did not move"
            assert abs(s.position.alt_rel_m - 10) < 2.0

            circling.cancel()
            assert (await cmds.set_mode("LAND")).ok

        # 3. Replay what we recorded.
        _header, frames = read_recording(rec_path)
        frames = list(frames)
        assert len(frames) > 150
        modes = {f.state.heartbeat.mode for f in frames}
        assert "GUIDED" in modes and any(f.state.heartbeat.armed for f in frames)
        assert max(f.state.position.alt_rel_m or 0 for f in frames) > 9.0

        # 4. Freeze SITL and watch the watchdog.
        pid = int((ROOT / ".sitl" / "arducopter.pid").read_text())
        await _until(lambda: link.status is LinkStatus.UP, 5, "link up before freeze")
        os.kill(pid, signal.SIGSTOP)
        frozen_at = time.monotonic()
        try:
            await _until(lambda: link.status is LinkStatus.LOST, 6, "link_lost")
            lost_after = time.monotonic() - frozen_at
        finally:
            os.kill(pid, signal.SIGCONT)
        assert lost_after < 3.6, f"link_lost took {lost_after:.1f}s"
        await _until(lambda: link.status is LinkStatus.UP, 15, "link_restored")
        names = [e.name for _t, e in events]
        assert names[0] == "link_up"
        assert "link_lost" in names and names[-1] == "link_restored"
        # Streams come back after the link does.
        await _until(lambda: not s.attitude.is_stale(), 10, "ATTITUDE after restore")
