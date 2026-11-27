"""A tiny fake ArduCopter that speaks just enough MAVLink for the unit tests.

It streams heartbeat, battery, attitude, position, GPS, HUD, EKF and RC
messages; ACKs commands (optionally after dropping a few, to test retries);
and can go silent (``paused``) to test the watchdog.
"""

from __future__ import annotations

import math
import os
import socket
import threading
import time

os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")

from pymavlink import mavutil  # noqa: E402

mavlink = mavutil.mavlink

COPTER_LOITER = 5
BANNER = ["ArduCopter V4.5.7 (2a3dc4b7)", "ChibiOS: 6a85082c", "fmuv3 003A0029 3436510F 30383336"]


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAutopilot:
    def __init__(
        self,
        port: int,
        rate_hz: float = 10.0,
        legacy: bool = False,
        ack: bool = False,
        drop_first: int = 0,
    ) -> None:
        # legacy=True behaves like ArduCopter 3.6: no MAV_CMD_REQUEST_MESSAGE support,
        # and (when ack=True) SET_MESSAGE_INTERVAL is answered UNSUPPORTED.
        self.legacy = legacy
        self.ack = ack
        self.drop_first = drop_first
        self.roll_deg = 0.0
        self.pitch_deg = 0.0
        self.yaw_deg = 90.0
        self.paused = threading.Event()
        self.confirmations: list[tuple[int, int]] = []
        self.data_streams: list[int] = []
        self.gcs_heartbeats = 0
        self.conn = mavutil.mavlink_connection(
            f"udpout:127.0.0.1:{port}", source_system=1, source_component=1
        )
        self.rate_hz = rate_hz
        self.commands: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> FakeAutopilot:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.conn.close()

    def _loop(self) -> None:
        mav = self.conn.mav
        tick = 0
        while not self._stop.is_set():
            if self.paused.is_set():
                # Silent, like a USB unplug: drain and drop whatever arrives.
                while self.conn.recv_match(blocking=False) is not None:
                    pass
                time.sleep(1.0 / self.rate_hz)
                continue
            mav.heartbeat_send(
                mavlink.MAV_TYPE_QUADROTOR,
                mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                COPTER_LOITER,
                mavlink.MAV_STATE_STANDBY,
            )
            mav.sys_status_send(0, 0, 0, 500, 12600, 150, 87, 0, 0, 0, 0, 0, 0)
            mav.attitude_send(
                int(time.monotonic() * 1000) & 0xFFFFFFFF,
                math.radians(self.roll_deg),
                math.radians(self.pitch_deg),
                math.radians(self.yaw_deg),
                0.0,
                0.0,
                0.0,
            )
            mav.global_position_int_send(0, -353632610, 1491652300, 584000, 1500, 10, -20, 0, 9000)
            mav.gps_raw_int_send(0, 3, -353632610, 1491652300, 584000, 80, 120, 0, 9000, 12)
            mav.vfr_hud_send(0.0, 0.3, 90, 0, 584.0, 0.0)
            mav.ekf_status_report_send(0x33F, 0.1, 0.2, 0.1, 0.05, 0.0)
            mav.rc_channels_send(0, 8, *([1500] * 4 + [1000] * 4 + [0] * 10), 200)
            if tick % 20 == 0:  # repeat: the first one may go out before anyone listens
                mav.statustext_send(mavlink.MAV_SEVERITY_INFO, b"Fake ArduCopter ready")
            tick += 1
            while (msg := self.conn.recv_match(blocking=False)) is not None:
                kind = msg.get_type()
                if kind == "COMMAND_LONG":
                    self._on_command(msg)
                elif kind == "REQUEST_DATA_STREAM":
                    self.data_streams.append(msg.req_stream_id)
                elif kind == "HEARTBEAT":
                    self.gcs_heartbeats += 1
            time.sleep(1.0 / self.rate_hz)

    def _on_command(self, msg) -> None:
        self.commands.append(msg.command)
        self.confirmations.append((msg.command, msg.confirmation))
        mav = self.conn.mav
        if self.ack:
            if self.drop_first > 0:
                self.drop_first -= 1
            else:
                result = mavlink.MAV_RESULT_ACCEPTED
                if self.legacy and msg.command == mavlink.MAV_CMD_SET_MESSAGE_INTERVAL:
                    result = mavlink.MAV_RESULT_UNSUPPORTED
                mav.command_ack_send(msg.command, result, 0, 0, msg.get_srcSystem(), 0)
        wants_version = (
            msg.command == mavlink.MAV_CMD_REQUEST_MESSAGE and int(msg.param1) == 148
        ) and not self.legacy
        if self.legacy and msg.command == mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES:
            wants_version = True
        if wants_version:
            mav.autopilot_version_send(
                capabilities=0xFFFF,
                flight_sw_version=(4 << 24) | (5 << 16) | (7 << 8) | 255,
                middleware_sw_version=0,
                os_sw_version=0,
                board_version=9 << 16,
                flight_custom_version=b"2a3dc4b7",
                middleware_custom_version=b"\0" * 8,
                os_custom_version=b"\0" * 8,
                vendor_id=0x26AC,
                product_id=0x11,
                uid=0x1234ABCD,
            )
        elif msg.command == 42428:
            for line in BANNER:
                mav.statustext_send(mavlink.MAV_SEVERITY_INFO, line.encode())
