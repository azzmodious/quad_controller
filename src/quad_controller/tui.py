"""Terminal telemetry view for bench work and SSH (works in the phone's browser SSH too).

``render()`` is a pure function of a :class:`VehicleState` and link info, so the
same view shows live data, replays and test fixtures.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from rich.console import Group as RGroup
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from quad_controller.state import Group, VehicleState

SKY = "on #1d4e89"
GROUND = "on #6b4423"
STALE = "dim"


@dataclass
class LinkInfo:
    status: str = "waiting"
    connection: str = ""
    target: str = ""
    rx_age_s: float | None = None
    tx_age_s: float | None = None
    recording: str | None = None
    source: str = "live"  # or "replay"
    hint: str | None = None


_LINK_STYLE = {
    "up": "bold black on green",
    "degraded": "bold black on yellow",
    "lost": "bold white on red",
    "waiting": "bold black on grey70",
}


def _fmt(v, spec: str = ".1f", unit: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:{spec}}{unit}"


def _age_note(group: Group, now: float) -> Text:
    age = group.age(now)
    if age is None:
        return Text("no data", style="red")
    if group.is_stale(now):
        return Text(f"stale {age:.1f}s", style="bold yellow")
    return Text(f"{age:.1f}s", style="dim")


def _title(name: str, group: Group, now: float) -> Text:
    t = Text(name + " ", style="bold")
    t.append_text(_age_note(group, now))
    return t


def horizon_mask(roll_deg: float, pitch_deg: float, w: int = 29, h: int = 11) -> list[list[bool]]:
    """Which cells of the horizon show ground (True) for this attitude.

    Seen from the cockpit: roll right (positive) makes the horizon rise on the
    right, i.e. it turns anticlockwise; pitch up (positive) moves it down.
    """
    roll = math.radians(max(-80.0, min(80.0, roll_deg)))
    pitch_rows = pitch_deg / 4.0  # 4 degrees of pitch per text row
    cx, cy = (w - 1) / 2, (h - 1) / 2
    mask = []
    for row in range(h):
        y = cy - row  # up is positive
        # Cells are about twice as tall as wide, so x is halved to keep angles true.
        mask.append([y < math.tan(roll) * (col - cx) / 2.0 - pitch_rows for col in range(w)])
    return mask


def horizon(roll_deg: float | None, pitch_deg: float | None, w: int = 29, h: int = 11) -> Text:
    """An artificial horizon drawn with background colours, with a fixed aircraft mark."""
    out = Text()
    if roll_deg is None or pitch_deg is None:
        for row in range(h):
            out.append(" " * w, style="on grey23")
            if row < h - 1:
                out.append("\n")
        return out
    mask = horizon_mask(roll_deg, pitch_deg, w, h)
    cx, cy = (w - 1) // 2, (h - 1) // 2
    for row in range(h):
        for col in range(w):
            style = GROUND if mask[row][col] else SKY
            ch = " "
            if row == cy and abs(col - cx) <= 4:
                ch = "┼" if col == cx else "─"
                style = "bold yellow " + style
            out.append(ch, style=style)
        if row < h - 1:
            out.append("\n")
    return out


_POINTS = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}


def compass_tape(heading: float | None, width: int = 37, deg_per_char: float = 5.0) -> Text:
    out = Text()
    if heading is None:
        out.append("·" * width + "\n", style=STALE)
        out.append(" " * (width // 2) + "▲", style=STALE)
        return out
    half = width // 2
    chars = [" "] * width
    for i in range(width):
        deg = (heading + (i - half) * deg_per_char) % 360
        nearest = round(deg / deg_per_char) * deg_per_char % 360
        chars[i] = "·" if nearest % 15 else "|"
    # Labels go on top of the ticks, centred on their bearing.
    for bearing, label in _POINTS.items():
        offset = ((bearing - heading + 180) % 360 - 180) / deg_per_char
        start = round(half + offset - (len(label) - 1) / 2)
        for j, c in enumerate(label):
            if 0 <= start + j < width:
                chars[start + j] = c
    line = "".join(chars)
    for c in line:
        out.append(c, style="bold cyan" if c.isalpha() else "grey62")
    out.append("\n" + " " * half + "▲", style="bold yellow")
    out.append(f"  {heading:05.1f}°", style="bold")
    return out


def bar(value: float | None, lo: float, hi: float, width: int = 16, style: str = "green") -> Text:
    if value is None:
        return Text("░" * width, style=STALE)
    frac = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    filled = round(frac * width)
    t = Text("█" * filled, style=style)
    t.append("░" * (width - filled), style="grey30")
    return t


def centre_bar(value: float | None, limit: float, width: int = 15) -> Text:
    """A bar that grows left or right from the middle (for roll and pitch)."""
    if value is None:
        return Text("░" * width, style=STALE)
    half = width // 2
    n = round(max(-1.0, min(1.0, value / limit)) * half)
    chars = ["░"] * width
    chars[half] = "│"
    rng = range(half + 1, half + n + 1) if n > 0 else range(half + n, half)
    for i in rng:
        chars[i] = "█"
    style = "red" if abs(value) > limit * 0.75 else "cyan"
    return Text("".join(chars), style=style)


def _kv(table: Table, key: str, value, style: str | None = None) -> None:
    table.add_row(
        Text(key, style="grey62"),
        value if isinstance(value, Text) else Text(value, style=style or ""),
    )


def _kv_table() -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True)
    return t


def header(state: VehicleState, info: LinkInfo, now: float) -> Text:
    t = Text()
    t.append(f" LINK {info.status.upper()} ", style=_LINK_STYLE.get(info.status, "bold"))
    t.append(f"  rx {_fmt(info.rx_age_s, '.1f', 's')}  tx {_fmt(info.tx_age_s, '.1f', 's')}  ")
    hb = state.heartbeat
    t.append(f" {hb.mode} ", style="bold black on cyan")
    t.append(" ")
    if hb.armed:
        t.append(" ARMED ", style="bold white on red")
    else:
        t.append(" DISARMED ", style="bold black on green")
    t.append(f"  {info.target} {info.connection}", style="grey62")
    if info.source == "replay":
        t.append("  REPLAY", style="bold magenta")
    if info.recording:
        t.append(f"  ● REC {info.recording}", style="bold red")
    return t


def attitude_panel(state: VehicleState, now: float) -> Panel:
    a = state.attitude
    stale = a.is_stale(now)
    kv = _kv_table()
    kv.add_column(no_wrap=True)
    kv.add_row(
        Text("roll ", style="grey62"),
        Text(_fmt(a.roll_deg, "+6.1f", "°")),
        centre_bar(a.roll_deg, 45),
    )
    kv.add_row(
        Text("pitch", style="grey62"),
        Text(_fmt(a.pitch_deg, "+6.1f", "°")),
        centre_bar(a.pitch_deg, 45),
    )
    kv.add_row(
        Text("rates", style="grey62"),
        Text(
            f"{_fmt(a.rollspeed_dps, '+.0f')} {_fmt(a.pitchspeed_dps, '+.0f')} "
            f"{_fmt(a.yawspeed_dps, '+.0f')} °/s",
            style="grey62",
        ),
        Text(""),
    )
    body = RGroup(
        horizon(a.roll_deg, a.pitch_deg),
        kv,
        compass_tape(a.heading_deg),
    )
    return Panel(body, title=_title("Attitude", a, now), border_style="yellow" if stale else "blue")


def position_panel(state: VehicleState, now: float) -> Panel:
    g, p, h = state.gps, state.position, state.hud
    kv = _kv_table()
    fix_style = "green" if g.has_3d_fix else "yellow" if g.fix_type == 2 else "red"
    gps_line = Text(f"{g.fix_name}", style=f"bold {fix_style}")
    gps_line.append(f"  sats {_fmt(g.satellites, 'd')}  hdop {_fmt(g.hdop, '.1f')}")
    gps_line.append("  ")
    gps_line.append_text(_age_note(g, now))
    _kv(kv, "gps", gps_line)
    _kv(kv, "lat/lon", f"{_fmt(p.lat, '.7f')}, {_fmt(p.lon, '.7f')}")
    _kv(kv, "alt", f"{_fmt(p.alt_rel_m, '.1f', ' m')} rel   {_fmt(p.alt_msl_m, '.1f', ' m')} msl")
    _kv(
        kv,
        "speed",
        f"{_fmt(h.groundspeed_mps, '.1f', ' m/s')}   climb {_fmt(h.climb_mps, '+.1f', ' m/s')}",
    )
    _kv(kv, "throttle", bar(h.throttle_pct, 0, 100, style="magenta"))
    e = state.ekf
    ekf = Text()
    if e.t is None:
        ekf.append("no data", style="red")
    else:
        ekf.append(
            "att ✓ " if e.attitude_ok else "att ✗ ", style="green" if e.attitude_ok else "red"
        )
        ekf.append("pos ✓" if e.position_ok else "pos ✗", style="green" if e.position_ok else "red")
        ekf.append(f"  flags 0x{e.flags:03x}", style="grey62")
    _kv(kv, "ekf", ekf)
    return Panel(kv, title=_title("Position", p, now), border_style="blue")


def battery_panel(state: VehicleState, now: float) -> Panel:
    b = state.battery
    kv = _kv_table()
    pct = b.remaining_pct
    style = "green" if pct is None or pct > 40 else "yellow" if pct > 20 else "red"
    _kv(kv, "voltage", f"{_fmt(b.voltage_v, '.2f', ' V')}   {_fmt(b.current_a, '.1f', ' A')}")
    line = bar(pct, 0, 100, style=style)
    line.append(f" {_fmt(pct, 'd', '%')}")
    _kv(kv, "remain", line)
    return Panel(kv, title=_title("Battery", b, now), border_style="blue")


def rc_panel(state: VehicleState, now: float) -> Panel:
    r = state.rc
    kv = _kv_table()
    chans = r.channels[:8] if r.channels else [None] * 8
    for i, v in enumerate(chans, start=1):
        val = v if v else None
        line = bar(val, 1000, 2000, width=12, style="cyan")
        line.append(f" {_fmt(val, '4d')}")
        _kv(kv, f"ch{i}", line)
    if r.rssi is not None:
        _kv(kv, "rssi", str(r.rssi))
    return Panel(kv, title=_title("RC in", r, now), border_style="blue")


def messages_panel(state: VehicleState, now: float, n: int = 6) -> Panel:
    t = Text()
    msgs = list(state.messages)[-n:]
    if not msgs:
        t.append("no messages yet", style="dim")
    for m in msgs:
        sev = m.severity
        style = "bold red" if sev <= 3 else "yellow" if sev == 4 else ""
        t.append(f"{now - m.t:5.0f}s ", style="grey62")
        t.append(m.text + "\n", style=style)
    t.rstrip()
    return Panel(t, title=Text("Messages", style="bold"), border_style="blue")


def render(state: VehicleState, info: LinkInfo, now: float | None = None, width: int = 100):
    now = time.monotonic() if now is None else now
    parts = [header(state, info, now)]
    if info.hint:
        parts.append(Text(info.hint, style="yellow"))
    att = attitude_panel(state, now)
    pos = position_panel(state, now)
    bat = battery_panel(state, now)
    rc = rc_panel(state, now)
    msgs = messages_panel(state, now)
    if width >= 90:
        grid = Table.grid(padding=(0, 1), expand=False)
        grid.add_column(width=43)
        grid.add_column(ratio=1)
        grid.add_row(att, RGroup(pos, bat))
        grid.add_row(rc, msgs)
        parts.append(grid)
    else:
        # Narrow terminal (phone): stack the panels.
        parts += [att, pos, bat, msgs, rc]
    return RGroup(*parts)
