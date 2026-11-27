"""Thin, synchronous helpers over pymavlink.

Phase 0 only needs to connect, see the autopilot's heartbeat and read a few
messages. Phase 1 replaces this with an async link layer, watchdog and
command helper; keep these helpers small so that move is easy.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field

# Use MAVLink 2 and the ArduPilot dialect. Must be set before pymavlink imports.
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")

from pymavlink import mavutil  # noqa: E402

from quad_controller.config import Config  # noqa: E402

mavlink = mavutil.mavlink

# Heartbeats from these vehicle types are not the autopilot (GCS, MAVProxy, us).
_NON_AUTOPILOT_TYPES = {
    mavlink.MAV_TYPE_GCS,
    mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
    mavlink.MAV_TYPE_GIMBAL,
    mavlink.MAV_TYPE_ADSB,
    mavlink.MAV_TYPE_CAMERA,
}

MAV_CMD_DO_SEND_BANNER = 42428  # ArduPilot: resend the boot banner as STATUSTEXT


class LinkTimeout(TimeoutError):
    """No autopilot heartbeat arrived in time."""


def is_autopilot_heartbeat(msg) -> bool:
    return (
        msg.get_type() == "HEARTBEAT"
        and msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID
        and msg.type not in _NON_AUTOPILOT_TYPES
    )


def open_connection(cfg: Config):
    """Open the MAVLink connection named by the active target."""
    return mavutil.mavlink_connection(
        cfg.target.connection,
        baud=cfg.target.baud,
        source_system=cfg.mavlink.source_system,
        source_component=cfg.mavlink.source_component,
        autoreconnect=True,
    )


def close_quietly(conn) -> None:
    # pymavlink's TCP close fails if the socket never connected.
    if conn is None:
        return
    with contextlib.suppress(OSError, AttributeError):
        conn.close()


def wait_autopilot_heartbeat(conn, timeout_s: float):
    """Block until a heartbeat from an autopilot arrives; set the conn's target ids."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LinkTimeout(f"no autopilot heartbeat within {timeout_s:.1f}s")
        msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=min(remaining, 0.5))
        if msg is not None and is_autopilot_heartbeat(msg):
            conn.target_system = msg.get_srcSystem()
            conn.target_component = msg.get_srcComponent()
            return msg


def send_companion_heartbeat(conn) -> None:
    """Announce ourselves as an onboard (companion) computer."""
    conn.mav.heartbeat_send(
        mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
        mavlink.MAV_AUTOPILOT_INVALID,
        0,
        0,
        mavlink.MAV_STATE_ACTIVE,
    )


def command_long(conn, command: int, *params: float) -> None:
    p = list(params) + [0.0] * (7 - len(params))
    conn.mav.command_long_send(conn.target_system, conn.target_component, command, 0, *p[:7])


def request_message_interval(conn, msg_id: int, hz: float) -> None:
    interval_us = int(1e6 / hz) if hz > 0 else -1
    command_long(conn, mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, interval_us)


def request_message(conn, msg_id: int) -> None:
    command_long(conn, mavlink.MAV_CMD_REQUEST_MESSAGE, msg_id)


def mode_name(heartbeat) -> str:
    return mavutil.mode_string_v10(heartbeat)


def is_armed(heartbeat) -> bool:
    return bool(heartbeat.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)


def decode_sw_version(v: int) -> str:
    """ArduPilot packs flight_sw_version as major.minor.patch.type in four bytes."""
    major, minor, patch, fw_type = (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
    types = {0: "dev", 64: "alpha", 128: "beta", 192: "rc", 255: "official"}
    return f"{major}.{minor}.{patch}-{types.get(fw_type, fw_type)}"


@dataclass
class Snapshot:
    """What `quadctl run` reports. Phase 1 replaces this with the full state model."""

    mode: str = "UNKNOWN"
    armed: bool = False
    system_status: str = "UNKNOWN"
    voltage_v: float | None = None
    current_a: float | None = None
    battery_pct: int | None = None
    last_heartbeat: float | None = None
    heartbeats: int = 0
    extra: dict = field(default_factory=dict)

    def heartbeat_age(self, now: float | None = None) -> float | None:
        if self.last_heartbeat is None:
            return None
        return (now or time.monotonic()) - self.last_heartbeat

    def update(self, msg) -> bool:
        """Apply a message. Returns True if the mode or armed state changed."""
        kind = msg.get_type()
        if kind == "HEARTBEAT" and is_autopilot_heartbeat(msg):
            before = (self.mode, self.armed)
            self.mode = mode_name(msg)
            self.armed = is_armed(msg)
            self.system_status = mavlink.enums["MAV_STATE"][msg.system_status].name.removeprefix(
                "MAV_STATE_"
            )
            self.last_heartbeat = time.monotonic()
            self.heartbeats += 1
            return before != (self.mode, self.armed)
        if kind == "SYS_STATUS":
            if msg.voltage_battery not in (0, 0xFFFF):
                self.voltage_v = round(msg.voltage_battery / 1000.0, 2)
            self.current_a = (
                None if msg.current_battery < 0 else round(msg.current_battery / 100.0, 2)
            )
            self.battery_pct = None if msg.battery_remaining < 0 else int(msg.battery_remaining)
        return False

    def fields(self) -> dict:
        age = self.heartbeat_age()
        return {
            "mode": self.mode,
            "armed": self.armed,
            "state": self.system_status,
            "voltage_v": self.voltage_v,
            "current_a": self.current_a,
            "battery_pct": self.battery_pct,
            "hb_age_s": None if age is None else round(age, 2),
        }
