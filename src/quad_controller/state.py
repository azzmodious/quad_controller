"""The vehicle state model: one typed object updated from incoming MAVLink.

Every group of values remembers when it was last updated (``t``, monotonic
seconds), so callers can tell fresh data from stale data. Nothing here talks to
the link; :meth:`VehicleState.apply` takes a decoded message and updates the
matching group, which keeps this module easy to test and to replay.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Any

from quad_controller.mavlink import is_armed, is_autopilot_heartbeat, mavlink, mode_name

GPS_FIX_NAMES = {
    0: "NO_GPS",
    1: "NO_FIX",
    2: "2D",
    3: "3D",
    4: "DGPS",
    5: "RTK_FLOAT",
    6: "RTK_FIXED",
    7: "STATIC",
    8: "PPP",
}

# EKF_STATUS_REPORT flags we need before trusting position (ArduPilot's own
# "EKF ok" check for position modes uses the same idea).
EKF_ATTITUDE = 1
EKF_VEL_HORIZ = 2
EKF_POS_HORIZ_ABS = 16
EKF_CONST_POS_MODE = 128
EKF_UNINITIALIZED = 1024


@dataclass
class Group:
    """Base for one block of state. ``t`` is the monotonic time of the last update."""

    t: float | None = None

    # How old this group may get before it counts as stale, in seconds.
    STALE_AFTER_S = 2.0

    def age(self, now: float | None = None) -> float | None:
        if self.t is None:
            return None
        return (time.monotonic() if now is None else now) - self.t

    def is_stale(self, now: float | None = None) -> bool:
        age = self.age(now)
        return age is None or age > self.STALE_AFTER_S

    def values(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "t"}


@dataclass
class Heartbeat(Group):
    STALE_AFTER_S = 3.0
    mode: str = "UNKNOWN"
    armed: bool = False
    system_status: str = "UNKNOWN"
    sysid: int | None = None
    compid: int | None = None
    count: int = 0


@dataclass
class Attitude(Group):
    STALE_AFTER_S = 0.5
    roll_deg: float | None = None
    pitch_deg: float | None = None
    yaw_deg: float | None = None  # -180..180 from the autopilot
    rollspeed_dps: float | None = None
    pitchspeed_dps: float | None = None
    yawspeed_dps: float | None = None

    @property
    def heading_deg(self) -> float | None:
        return None if self.yaw_deg is None else self.yaw_deg % 360.0


@dataclass
class Position(Group):
    STALE_AFTER_S = 1.0
    lat: float | None = None
    lon: float | None = None
    alt_msl_m: float | None = None
    alt_rel_m: float | None = None
    vn_mps: float | None = None
    ve_mps: float | None = None
    vd_mps: float | None = None
    heading_deg: float | None = None


@dataclass
class Gps(Group):
    STALE_AFTER_S = 2.0
    fix_type: int = 0
    satellites: int | None = None
    hdop: float | None = None
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None

    @property
    def fix_name(self) -> str:
        return GPS_FIX_NAMES.get(self.fix_type, str(self.fix_type))

    @property
    def has_3d_fix(self) -> bool:
        return self.fix_type >= 3


@dataclass
class Battery(Group):
    STALE_AFTER_S = 3.0
    voltage_v: float | None = None
    current_a: float | None = None
    remaining_pct: int | None = None


@dataclass
class Hud(Group):
    STALE_AFTER_S = 1.0
    groundspeed_mps: float | None = None
    airspeed_mps: float | None = None
    climb_mps: float | None = None
    throttle_pct: int | None = None
    heading_deg: float | None = None


@dataclass
class Ekf(Group):
    STALE_AFTER_S = 3.0
    flags: int = 0
    velocity_variance: float | None = None
    pos_horiz_variance: float | None = None
    pos_vert_variance: float | None = None
    compass_variance: float | None = None

    @property
    def attitude_ok(self) -> bool:
        return bool(self.flags & EKF_ATTITUDE) and not self.flags & EKF_UNINITIALIZED

    @property
    def position_ok(self) -> bool:
        need = EKF_ATTITUDE | EKF_VEL_HORIZ | EKF_POS_HORIZ_ABS
        return (self.flags & need) == need and not self.flags & EKF_CONST_POS_MODE


@dataclass
class RcInput(Group):
    STALE_AFTER_S = 1.0
    channels: list[int] = field(default_factory=list)  # microseconds, 0 = not present
    rssi: int | None = None  # 0-254, None = unknown


@dataclass
class StatusText:
    t: float
    severity: int
    text: str

    @property
    def severity_name(self) -> str:
        entry = mavlink.enums["MAV_SEVERITY"].get(self.severity)
        return entry.name.removeprefix("MAV_SEVERITY_") if entry else str(self.severity)


GROUPS = ("heartbeat", "attitude", "position", "gps", "battery", "hud", "ekf", "rc")


@dataclass
class VehicleState:
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    attitude: Attitude = field(default_factory=Attitude)
    position: Position = field(default_factory=Position)
    gps: Gps = field(default_factory=Gps)
    battery: Battery = field(default_factory=Battery)
    hud: Hud = field(default_factory=Hud)
    ekf: Ekf = field(default_factory=Ekf)
    rc: RcInput = field(default_factory=RcInput)
    messages: deque[StatusText] = field(default_factory=lambda: deque(maxlen=20))

    def apply(self, msg, now: float | None = None) -> str | None:
        """Update from one MAVLink message. Returns the group name it changed, if any."""
        now = time.monotonic() if now is None else now
        handler = _HANDLERS.get(msg.get_type())
        if handler is None:
            return None
        return handler(self, msg, now)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """Plain data for logs and recordings; each group carries its ``age_s``."""
        now = time.monotonic() if now is None else now
        out: dict[str, Any] = {}
        for name in GROUPS:
            group: Group = getattr(self, name)
            age = group.age(now)
            out[name] = {**group.values(), "age_s": None if age is None else round(age, 3)}
        out["messages"] = [
            {"age_s": round(now - m.t, 3), "severity": m.severity, "text": m.text}
            for m in self.messages
        ]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], now: float | None = None) -> VehicleState:
        """Rebuild a state from :meth:`to_dict` output (used for replay)."""
        now = time.monotonic() if now is None else now
        state = cls()
        for name in GROUPS:
            raw = dict(data.get(name) or {})
            age = raw.pop("age_s", None)
            group: Group = getattr(state, name)
            known = {f.name for f in fields(group)}
            for key, value in raw.items():
                if key in known:
                    setattr(group, key, value)
            group.t = None if age is None else now - age
        for m in data.get("messages", []):
            state.messages.append(StatusText(now - m["age_s"], m["severity"], m["text"]))
        return state


def _round(value: float, digits: int = 2) -> float:
    return round(value, digits)


def _on_heartbeat(s: VehicleState, msg, now: float) -> str | None:
    if not is_autopilot_heartbeat(msg):
        return None
    hb = s.heartbeat
    hb.mode = mode_name(msg)
    hb.armed = is_armed(msg)
    entry = mavlink.enums["MAV_STATE"].get(msg.system_status)
    hb.system_status = entry.name.removeprefix("MAV_STATE_") if entry else str(msg.system_status)
    hb.sysid = msg.get_srcSystem()
    hb.compid = msg.get_srcComponent()
    hb.count += 1
    hb.t = now
    return "heartbeat"


def _on_attitude(s: VehicleState, msg, now: float) -> str:
    a = s.attitude
    a.roll_deg = _round(math.degrees(msg.roll))
    a.pitch_deg = _round(math.degrees(msg.pitch))
    a.yaw_deg = _round(math.degrees(msg.yaw))
    a.rollspeed_dps = _round(math.degrees(msg.rollspeed))
    a.pitchspeed_dps = _round(math.degrees(msg.pitchspeed))
    a.yawspeed_dps = _round(math.degrees(msg.yawspeed))
    a.t = now
    return "attitude"


def _on_global_position(s: VehicleState, msg, now: float) -> str:
    p = s.position
    p.lat = msg.lat / 1e7
    p.lon = msg.lon / 1e7
    p.alt_msl_m = _round(msg.alt / 1000.0)
    p.alt_rel_m = _round(msg.relative_alt / 1000.0)
    p.vn_mps = _round(msg.vx / 100.0)
    p.ve_mps = _round(msg.vy / 100.0)
    p.vd_mps = _round(msg.vz / 100.0)
    p.heading_deg = None if msg.hdg == 0xFFFF else _round(msg.hdg / 100.0)
    p.t = now
    return "position"


def _on_gps_raw(s: VehicleState, msg, now: float) -> str:
    g = s.gps
    g.fix_type = int(msg.fix_type)
    g.satellites = None if msg.satellites_visible == 255 else int(msg.satellites_visible)
    g.hdop = None if msg.eph == 0xFFFF else _round(msg.eph / 100.0)
    has_pos = msg.fix_type >= 2
    g.lat = msg.lat / 1e7 if has_pos else None
    g.lon = msg.lon / 1e7 if has_pos else None
    g.alt_m = _round(msg.alt / 1000.0) if has_pos else None
    g.t = now
    return "gps"


def _on_sys_status(s: VehicleState, msg, now: float) -> str:
    b = s.battery
    b.voltage_v = None if msg.voltage_battery in (0, 0xFFFF) else _round(msg.voltage_battery / 1e3)
    b.current_a = None if msg.current_battery < 0 else _round(msg.current_battery / 100.0)
    b.remaining_pct = None if msg.battery_remaining < 0 else int(msg.battery_remaining)
    b.t = now
    return "battery"


def _on_vfr_hud(s: VehicleState, msg, now: float) -> str:
    h = s.hud
    h.groundspeed_mps = _round(msg.groundspeed)
    h.airspeed_mps = _round(msg.airspeed)
    h.climb_mps = _round(msg.climb)
    h.throttle_pct = int(msg.throttle)
    h.heading_deg = float(msg.heading)
    h.t = now
    return "hud"


def _on_ekf(s: VehicleState, msg, now: float) -> str:
    e = s.ekf
    e.flags = int(msg.flags)
    e.velocity_variance = _round(msg.velocity_variance, 3)
    e.pos_horiz_variance = _round(msg.pos_horiz_variance, 3)
    e.pos_vert_variance = _round(msg.pos_vert_variance, 3)
    e.compass_variance = _round(msg.compass_variance, 3)
    e.t = now
    return "ekf"


def _on_rc_channels(s: VehicleState, msg, now: float) -> str:
    r = s.rc
    count = int(msg.chancount) or 18
    r.channels = [int(getattr(msg, f"chan{i}_raw")) for i in range(1, min(count, 18) + 1)]
    r.rssi = None if msg.rssi == 255 else int(msg.rssi)
    r.t = now
    return "rc"


def _on_statustext(s: VehicleState, msg, now: float) -> str:
    text = msg.text if isinstance(msg.text, str) else msg.text.decode("ascii", "replace")
    s.messages.append(StatusText(now, int(msg.severity), text.rstrip("\0")))
    return "messages"


_HANDLERS = {
    "HEARTBEAT": _on_heartbeat,
    "ATTITUDE": _on_attitude,
    "GLOBAL_POSITION_INT": _on_global_position,
    "GPS_RAW_INT": _on_gps_raw,
    "SYS_STATUS": _on_sys_status,
    "VFR_HUD": _on_vfr_hud,
    "EKF_STATUS_REPORT": _on_ekf,
    "RC_CHANNELS": _on_rc_channels,
    "STATUSTEXT": _on_statustext,
}
