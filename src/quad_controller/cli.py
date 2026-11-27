"""`quadctl` command line.

quadctl run              connect and log heartbeat, mode and battery (the Phase 0 service)
quadctl check            exit 0 if an autopilot heartbeat arrives in time, else 1
quadctl firmware-info    print board, firmware version and boot banner
quadctl logs             view the JSON-lines log
quadctl config           print the resolved configuration
"""

from __future__ import annotations

import argparse
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


class _Stop:
    def __init__(self) -> None:
        self.requested = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, _frame) -> None:
        self.requested = True


def cmd_run(cfg: Config, args) -> int:
    from quad_controller import mavlink as ml

    stop = _Stop()
    started = time.monotonic()
    log_event(
        log,
        logging.INFO,
        "starting",
        target=cfg.target.name,
        connection=cfg.target.connection,
        version=__version__,
    )
    conn = None
    while conn is None and not stop.requested:
        try:
            conn = ml.open_connection(cfg)
        except OSError as exc:
            log_event(log, logging.WARNING, "connect_failed", reason=str(exc))
            time.sleep(2.0)
    if conn is None:
        return 0
    snap = ml.Snapshot()
    link_up = False
    last_hb_sent = last_status = last_stream_req = 0.0
    timeout = cfg.mavlink.heartbeat_timeout_s

    while not stop.requested:
        now = time.monotonic()
        if args.duration is not None and now - started >= args.duration:
            break
        if now - last_hb_sent >= 1.0:
            ml.send_companion_heartbeat(conn)
            last_hb_sent = now

        try:
            msg = conn.recv_match(
                type=["HEARTBEAT", "SYS_STATUS", "STATUSTEXT"], blocking=True, timeout=0.2
            )
        except OSError as exc:
            # SITL restarted or USB serial unplugged: report, back off, keep going.
            log_event(log, logging.WARNING, "link_error", reason=str(exc))
            time.sleep(1.0)
            msg = None
        if msg is not None:
            if msg.get_type() == "STATUSTEXT":
                log_event(log, logging.INFO, "statustext", text=msg.text, severity=msg.severity)
            changed = snap.update(msg)
            if msg.get_type() == "HEARTBEAT" and ml.is_autopilot_heartbeat(msg):
                conn.target_system = msg.get_srcSystem()
                conn.target_component = msg.get_srcComponent()
                if not link_up:
                    link_up = True
                    log_event(
                        log,
                        logging.INFO,
                        "link_up",
                        sysid=conn.target_system,
                        compid=conn.target_component,
                        **snap.fields(),
                    )
                    last_stream_req = 0.0
                elif changed:
                    log_event(log, logging.INFO, "mode_change", **snap.fields())

        now = time.monotonic()
        age = snap.heartbeat_age(now)
        if link_up and age is not None and age > timeout:
            link_up = False
            log_event(log, logging.WARNING, "link_lost", hb_age_s=round(age, 1))
        if link_up and now - last_stream_req >= 10.0:
            # Ask for battery data each 10 s; harmless if already streaming.
            ml.request_message_interval(conn, ml.mavlink.MAVLINK_MSG_ID_SYS_STATUS, 1.0)
            last_stream_req = now
        if now - last_status >= cfg.mavlink.status_interval_s:
            last_status = now
            if link_up:
                log_event(log, logging.INFO, "status", **snap.fields())
            elif snap.last_heartbeat is None:
                log_event(
                    log, logging.INFO, "waiting_for_heartbeat", connection=cfg.target.connection
                )

    ml.close_quietly(conn)
    log_event(log, logging.INFO, "stopped", heartbeats=snap.heartbeats)
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
        setup_logging(cfg.logging, console=cfg.logging.console)
    return COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
