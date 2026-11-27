"""A tiny fake ArduCopter that speaks just enough MAVLink for the Phase 0 tests."""

from __future__ import annotations

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
    def __init__(self, port: int, rate_hz: float = 10.0, legacy: bool = False) -> None:
        # legacy=True behaves like ArduCopter 3.6: no MAV_CMD_REQUEST_MESSAGE support.
        self.legacy = legacy
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
        while not self._stop.is_set():
            mav.heartbeat_send(
                mavlink.MAV_TYPE_QUADROTOR,
                mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                COPTER_LOITER,
                mavlink.MAV_STATE_STANDBY,
            )
            mav.sys_status_send(0, 0, 0, 500, 12600, 150, 87, 0, 0, 0, 0, 0, 0)
            while (msg := self.conn.recv_match(blocking=False)) is not None:
                if msg.get_type() == "COMMAND_LONG":
                    self._on_command(msg)
            time.sleep(1.0 / self.rate_hz)

    def _on_command(self, msg) -> None:
        self.commands.append(msg.command)
        mav = self.conn.mav
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
