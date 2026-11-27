"""`quadctl` command line.

quadctl run              connect, keep the link alive and log status (the systemd service)
quadctl telemetry        live terminal view of attitude, GPS, battery, RC and messages
quadctl record           save telemetry to a file at 10 Hz
quadctl replay <file>    play a recording back in the terminal view
quadctl check            exit 0 if an autopilot heartbeat arrives in time, else 1
quadctl firmware-info    print board, firmware version and boot banner
quadctl logs             view the JSON-lines log
quadctl config           print the resolved configuration
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from quad_controller import __version__
from quad_controller.config import VALID_TARGETS, Config, ConfigError, load_config
from quad_controller.logging_setup import log_event, resolve_log_dir, setup_logging

log = logging.getLogger("quad_controller")


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="YAML file merged over config/default.yaml")
    common.add_argument("--target", choices=VALID_TARGETS, help="which configured link to use")
    common.add_argument("--connection", help="override the connection string, e.g. tcp:host:5760")

    parser = argparse.ArgumentParser(prog="quadctl", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", parents=[common], help="connect and log vehicle status")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument(
        "--record", nargs="?", const="auto", help="also record telemetry (optional file name)"
    )

    p = sub.add_parser("telemetry", parents=[common], help="live terminal telemetry view")
    p.add_argument("--record", nargs="?", const="auto", help="also record (optional file name)")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--no-screen", action="store_true", help="draw inline, not full screen")

    p = sub.add_parser("record", parents=[common], help="record telemetry to a file")
    p.add_argument("--out", help="file (.jsonl or .jsonl.gz); default: recordings folder")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--rate", type=float, help="frames per second (default: config, 10)")

    p = sub.add_parser("replay", parents=[common], help="play a recording in the terminal view")
    p.add_argument("file")
    p.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
    p.add_argument("--no-screen", action="store_true", help="draw inline, not full screen")

    p = sub.add_parser("check", parents=[common], help="wait for an autopilot heartbeat")
    p.add_argument("--timeout", type=float, help="seconds to wait (default: config)")

    p = sub.add_parser("firmware-info", parents=[common], help="report board and firmware")
    p.add_argument("--wait", type=float, default=4.0, help="seconds to collect the banner")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = sub.add_parser("logs", parents=[common], help="view logs")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    p.add_argument("-n", "--tail", type=int, help="only the last N matching lines")
    p.add_argument("--level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="min level")
    p.add_argument("--grep", help="only lines containing this text")
    p.add_argument("--dir", help="log directory (default: from config)")

    sub.add_parser("config", parents=[common], help="print resolved configuration")
    return parser


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    return stop


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


def _open_link(cfg: Config):
    from quad_controller.commands import CommandSender
    from quad_controller.link import MavLink

    link = MavLink.from_config(cfg)
    # The service and the telemetry tools never fly the aircraft.
    CommandSender(link, allow_flight_commands=False)
    return link


def status_fields(link) -> dict:
    s = link.state
    age = link.watchdog.rx_age()
    return {
        "link": str(link.watchdog.status),
        "mode": s.heartbeat.mode,
        "armed": s.heartbeat.armed,
        "state": s.heartbeat.system_status,
        "voltage_v": s.battery.voltage_v,
        "current_a": s.battery.current_a,
        "battery_pct": s.battery.remaining_pct,
        "roll_deg": s.attitude.roll_deg,
        "pitch_deg": s.attitude.pitch_deg,
        "heading_deg": s.attitude.heading_deg,
        "gps_fix": s.gps.fix_name,
        "sats": s.gps.satellites,
        "alt_rel_m": s.position.alt_rel_m,
        "hb_age_s": None if age is None else round(age, 2),
    }


def _recording_path(cfg: Config, value: str | None):
    from quad_controller.recorder import default_recording_path

    if value in (None, "auto", ""):
        return default_recording_path(cfg)
    return Path(value).expanduser()


def cmd_run(cfg: Config, args) -> int:
    return asyncio.run(_run(cfg, args))


async def _run(cfg: Config, args) -> int:
    from quad_controller.recorder import Recorder
    from quad_controller.watchdog import LinkStatus

    stop = _stop_event()
    log_event(
        log,
        logging.INFO,
        "starting",
        target=cfg.target.name,
        connection=cfg.target.connection,
        version=__version__,
    )
    link = _open_link(cfg)
    last = {"mode": None, "armed": None}

    def on_message(msg) -> None:
        if msg.get_type() != "HEARTBEAT" or link.state.heartbeat.count == 0:
            return
        hb = link.state.heartbeat
        if last["mode"] is not None and (hb.mode, hb.armed) != (last["mode"], last["armed"]):
            log_event(log, logging.INFO, "mode_change", **status_fields(link))
        last["mode"], last["armed"] = hb.mode, hb.armed

    link.subscribe(on_message)
    started = time.monotonic()
    recorder = None
    async with link:
        if args.record is not None:
            path = _recording_path(cfg, args.record)
            recorder = Recorder(link, path, cfg.recorder.rate_hz, target=cfg.target.name)
            recorder.start()
            log_event(log, logging.INFO, "recording", path=str(path))
        try:
            while not stop.is_set():
                remaining = None
                if args.duration is not None:
                    remaining = args.duration - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                interval = cfg.mavlink.status_interval_s
                await _wait(stop, interval if remaining is None else min(interval, remaining))
                if stop.is_set():
                    break
                if link.watchdog.status is LinkStatus.WAITING:
                    log_event(
                        log, logging.INFO, "waiting_for_heartbeat", connection=cfg.target.connection
                    )
                elif link.watchdog.status is not LinkStatus.LOST:
                    log_event(log, logging.INFO, "status", **status_fields(link))
        finally:
            if recorder is not None:
                await recorder.stop()
    log_event(log, logging.INFO, "stopped", heartbeats=link.state.heartbeat.count)
    return 0


def cmd_telemetry(cfg: Config, args) -> int:
    return asyncio.run(_telemetry(cfg, args))


def _waiting_hint(cfg: Config, link, waited: float) -> str | None:
    from quad_controller.link import is_serial
    from quad_controller.watchdog import LinkStatus

    status = link.watchdog.status
    if status is LinkStatus.WAITING and waited > 5:
        hint = f"No heartbeat on {cfg.target.connection} yet."
        if is_serial(cfg.target.connection):
            hint += (
                " If the quad-controller service is running it holds this port:"
                " sudo systemctl stop quad-controller"
            )
        return hint
    if status is LinkStatus.LOST:
        return "Link lost. Check the cable and the Pixhawk power; reconnecting by itself."
    return None


async def _telemetry(cfg: Config, args) -> int:
    from rich.console import Console
    from rich.live import Live

    from quad_controller.recorder import Recorder
    from quad_controller.tui import LinkInfo, render

    stop = _stop_event()
    console = Console()
    link = _open_link(cfg)
    started = time.monotonic()
    recorder = None
    async with link:
        info = LinkInfo(connection=cfg.target.connection, target=cfg.target.name)
        if args.record is not None:
            path = _recording_path(cfg, args.record)
            recorder = Recorder(link, path, cfg.recorder.rate_hz, target=cfg.target.name)
            recorder.start()
            info.recording = path.name
        try:
            with Live(console=console, refresh_per_second=10, screen=not args.no_screen) as live:
                while not stop.is_set():
                    if args.duration is not None and time.monotonic() - started >= args.duration:
                        break
                    info.status = str(link.watchdog.status)
                    info.rx_age_s = link.watchdog.rx_age()
                    info.tx_age_s = link.watchdog.tx_age()
                    info.hint = _waiting_hint(cfg, link, time.monotonic() - started)
                    live.update(render(link.state, info, width=console.width))
                    await _wait(stop, 0.1)
        finally:
            if recorder is not None:
                await recorder.stop()
                console.print(f"Saved {recorder.frames} frames to {recorder.path}")
    return 0


def cmd_record(cfg: Config, args) -> int:
    return asyncio.run(_record(cfg, args))


async def _record(cfg: Config, args) -> int:
    from quad_controller.recorder import Recorder

    stop = _stop_event()
    link = _open_link(cfg)
    path = _recording_path(cfg, args.out)
    async with link:
        if not await link.wait_connected(cfg.mavlink.heartbeat_timeout_s):
            log_event(log, logging.ERROR, "record_failed", reason="no heartbeat")
            return 1
        async with Recorder(
            link, path, args.rate or cfg.recorder.rate_hz, target=cfg.target.name
        ) as rec:
            log_event(log, logging.INFO, "recording", path=str(path))
            await _wait(stop, args.duration if args.duration is not None else 1e9)
    log_event(log, logging.INFO, "recorded", path=str(path), frames=rec.frames)
    print(path)
    return 0


def cmd_replay(cfg: Config, args) -> int:
    return asyncio.run(_replay(args))


async def _replay(args) -> int:
    from rich.console import Console
    from rich.live import Live

    from quad_controller.recorder import read_recording, replay
    from quad_controller.tui import LinkInfo, render

    stop = _stop_event()
    try:
        header, _frames = read_recording(args.file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    console = Console()
    info = LinkInfo(
        connection=str(header.get("connection") or ""),
        target=str(header.get("target") or ""),
        source="replay",
    )
    with Live(console=console, refresh_per_second=10, screen=not args.no_screen) as live:
        async for frame in replay(args.file, speed=args.speed):
            if stop.is_set():
                break
            info.status = frame.link
            info.rx_age_s = frame.state.heartbeat.age()
            live.update(render(frame.state, info, width=console.width))
    return 0


def cmd_check(cfg: Config, args) -> int:
    from quad_controller import mavlink as ml

    timeout = args.timeout or cfg.mavlink.heartbeat_timeout_s
    t0 = time.monotonic()
    conn = None
    try:
        conn = ml.open_connection(cfg)
        hb = ml.wait_autopilot_heartbeat(conn, timeout)
    except (ml.LinkTimeout, OSError) as exc:
        log_event(
            log, logging.ERROR, "check_failed", connection=cfg.target.connection, reason=str(exc)
        )
        return 1
    finally:
        ml.close_quietly(conn)
    log_event(
        log,
        logging.INFO,
        "check_ok",
        connection=cfg.target.connection,
        sysid=hb.get_srcSystem(),
        mode=ml.mode_name(hb),
        armed=ml.is_armed(hb),
        seconds=round(time.monotonic() - t0, 2),
    )
    return 0


def collect_firmware_info(conn, wait_s: float) -> dict:
    """Ask the autopilot for AUTOPILOT_VERSION and its boot banner."""
    from quad_controller import mavlink as ml

    info: dict = {"banner": []}
    ml.request_message(conn, ml.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION)
    # ArduCopter before 4.0 ignores REQUEST_MESSAGE; it answers this older command.
    ml.command_long(conn, ml.mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES, 1)
    ml.command_long(conn, ml.MAV_CMD_DO_SEND_BANNER)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        msg = conn.recv_match(type=["AUTOPILOT_VERSION", "STATUSTEXT"], blocking=True, timeout=0.3)
        if msg is None:
            continue
        if msg.get_type() == "STATUSTEXT":
            if msg.text not in info["banner"]:
                info["banner"].append(msg.text)
        else:
            info["firmware_version"] = ml.decode_sw_version(msg.flight_sw_version)
            info["board_version"] = msg.board_version
            info["vendor_id"] = msg.vendor_id
            info["product_id"] = msg.product_id
            info["uid"] = f"{msg.uid:016x}"
            raw = bytes(msg.flight_custom_version).split(b"\0", 1)[0]
            info["git_hash"] = raw.decode("ascii", "replace")
            info["capabilities"] = msg.capabilities
    info["flash_hint"] = flash_hint(info["banner"])
    return info


def flash_hint(banner: list[str]) -> str:
    """Guess the flash size from the board name ArduPilot prints in its boot banner.

    Pixhawk 2.4.8 boards run either the Pixhawk1 / fmuv3 build (2 MB flash) or the
    Pixhawk1-1M / fmuv2 build for boards whose chip only exposes 1 MB.
    """
    text = " ".join(banner).lower()
    if "pixhawk1-1m" in text or "fmuv2" in text or "px4v2" in text:
        return "1 MB flash build: some features are compiled out (see docs/hardware/firmware.md)"
    if "pixhawk1" in text or "fmuv3" in text or "px4v3" in text:
        return "2 MB flash build: full feature set"
    return "unknown: check the banner lines for the board name"


def cmd_firmware_info(cfg: Config, args) -> int:
    from quad_controller import mavlink as ml

    conn = None
    try:
        conn = ml.open_connection(cfg)
        hb = ml.wait_autopilot_heartbeat(conn, cfg.mavlink.heartbeat_timeout_s)
        info = {
            "mode": ml.mode_name(hb),
            "mav_type": hb.type,
            **collect_firmware_info(conn, args.wait),
        }
    except (ml.LinkTimeout, OSError) as exc:
        print(f"error: {exc} on {cfg.target.connection}", file=sys.stderr)
        return 1
    finally:
        ml.close_quietly(conn)
    log_event(log, logging.INFO, "firmware_info", **info)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        for key, value in info.items():
            if key == "banner":
                print("banner:")
                for line in value:
                    print(f"  {line}")
            else:
                print(f"{key}: {value}")
    return 0


def cmd_logs(cfg: Config, args) -> int:
    from quad_controller.logview import show

    log_dir = Path(args.dir).expanduser() if args.dir else resolve_log_dir(cfg.logging)
    return show(
        log_dir,
        sys.stdout,
        min_level=args.level,
        grep=args.grep,
        tail=args.tail,
        follow=args.follow,
    )


def cmd_config(cfg: Config, _args) -> int:
    print(json.dumps(asdict(cfg), indent=2))
    return 0


COMMANDS = {
    "run": cmd_run,
    "telemetry": cmd_telemetry,
    "record": cmd_record,
    "replay": cmd_replay,
    "check": cmd_check,
    "firmware-info": cmd_firmware_info,
    "logs": cmd_logs,
    "config": cmd_config,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, target=args.target, connection=args.connection)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.command not in ("logs", "config"):
        # Full-screen views keep log lines off the console; the file still gets them.
        quiet = args.command in ("telemetry", "replay")
        setup_logging(cfg.logging, console=cfg.logging.console and not quiet)
    return COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
