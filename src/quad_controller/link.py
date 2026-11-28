"""Async MAVLink link layer.

pymavlink is blocking, so one reader thread owns ``recv`` and hands every
message to the asyncio loop. Everything else (state, watchdog, commands, the
TUI) runs on the loop and never blocks on the link.

What the link does on its own:

* sends our companion-computer heartbeat (``link.heartbeat_hz``)
* asks the autopilot for the streams in ``link.streams`` once it sees a
  heartbeat, again after the link comes back, and again for any stream that
  stops arriving
* reopens the connection after an I/O error (USB unplugged, SITL restarted)
* runs the watchdog and keeps :class:`VehicleState` up to date
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from quad_controller import mavlink as ml
from quad_controller.config import Config
from quad_controller.logging_setup import log_event
from quad_controller.state import VehicleState
from quad_controller.watchdog import LinkEvent, LinkStatus, LinkWatchdog

log = logging.getLogger(__name__)
mavlink = ml.mavlink

# REQUEST_DATA_STREAM group for each message, used when the autopilot refuses
# SET_MESSAGE_INTERVAL. Groups set several messages at once; that is fine here.
_LEGACY_STREAM = {
    "ATTITUDE": mavlink.MAV_DATA_STREAM_EXTRA1,
    "GLOBAL_POSITION_INT": mavlink.MAV_DATA_STREAM_POSITION,
    "GPS_RAW_INT": mavlink.MAV_DATA_STREAM_EXTENDED_STATUS,
    "SYS_STATUS": mavlink.MAV_DATA_STREAM_EXTENDED_STATUS,
    "VFR_HUD": mavlink.MAV_DATA_STREAM_EXTRA2,
    "EKF_STATUS_REPORT": mavlink.MAV_DATA_STREAM_EXTRA3,
    "RC_CHANNELS": mavlink.MAV_DATA_STREAM_RC_CHANNELS,
}

MessageListener = Callable[[Any], None]


def is_serial(connection: str) -> bool:
    c = connection.lower()
    return c.startswith("/dev/") or c.startswith("com") or c.startswith("\\\\.\\")


class MavLink:
    """One MAVLink connection to one autopilot.

    Use it as an async context manager::

        async with MavLink.from_config(cfg) as link:
            await link.wait_connected(10)
            print(link.state.attitude.roll_deg)
    """

    def __init__(
        self,
        connection: str,
        *,
        baud: int = 115200,
        source_system: int = 1,
        source_component: int = 191,
        heartbeat_hz: float = 1.0,
        degraded_after_s: float = 1.0,
        lost_after_s: float = 3.0,
        streams: dict[str, float] | None = None,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.connection = connection
        self.baud = baud
        self.source_system = source_system
        self.source_component = source_component
        self.heartbeat_period = 1.0 / heartbeat_hz if heartbeat_hz > 0 else 1.0
        self.streams = dict(streams or {})
        self.state = VehicleState()
        self.watchdog = LinkWatchdog(
            degraded_after_s, lost_after_s, tx_period_s=self.heartbeat_period
        )
        self.target_system = 1
        self.target_component = 1
        self.messages_received = 0
        self.reconnects = 0
        self._connect = connect or self._default_connect
        self._conn = None
        self._send_lock = threading.Lock()
        self._listeners: list[MessageListener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._tasks: list[asyncio.Task] = []
        self._connected = asyncio.Event()
        self._stream_requested: dict[str, float] = {}
        self._legacy_streams = False
        self._streams_task: asyncio.Task | None = None
        self.commands = None  # set by quad_controller.commands.CommandSender
        self.watchdog.subscribe(self._on_link_event)

    @classmethod
    def from_config(cls, cfg: Config, **kwargs) -> MavLink:
        return cls(
            cfg.target.connection,
            baud=cfg.target.baud,
            source_system=cfg.mavlink.source_system,
            source_component=cfg.mavlink.source_component,
            heartbeat_hz=cfg.link.heartbeat_hz,
            degraded_after_s=cfg.link.degraded_after_s,
            lost_after_s=cfg.link.lost_after_s,
            streams=cfg.link.streams,
            **kwargs,
        )

    # ----- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> MavLink:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="mavlink-rx", daemon=True)
        self._reader.start()
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="mavlink-heartbeat"),
            asyncio.create_task(self._watchdog_loop(), name="mavlink-watchdog"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in [*self._tasks, self._streams_task]:
            if task is not None:
                task.cancel()
        for task in [*self._tasks, self._streams_task]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks = []
        if self._reader is not None:
            await asyncio.to_thread(self._reader.join, 2.0)
        ml.close_quietly(self._conn)
        self._conn = None

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the first autopilot heartbeat. Returns False on timeout."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    @property
    def status(self) -> LinkStatus:
        return self.watchdog.status

    def subscribe(self, listener: MessageListener) -> Callable[[], None]:
        """Call ``listener(msg)`` on the event loop for every message received."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # ----- sending ---------------------------------------------------------

    def send(self, build: Callable[[Any], None]) -> bool:
        """Run ``build(mav)`` under the send lock, e.g. ``lambda m: m.heartbeat_send(...)``.

        Returns False if there is no open connection or the write failed.
        """
        conn = self._conn
        if conn is None:
            return False
        try:
            with self._send_lock:
                build(conn.mav)
        except (OSError, ValueError) as exc:
            log_event(log, logging.DEBUG, "send_failed", reason=str(exc))
            return False
        return True

    def command_long(self, command: int, params: list[float], confirmation: int = 0) -> bool:
        p = (list(params) + [0.0] * 7)[:7]
        return self.send(
            lambda m: m.command_long_send(
                self.target_system, self.target_component, command, confirmation, *p
            )
        )

    # ----- internals -------------------------------------------------------

    def _default_connect(self):
        return ml.mavutil.mavlink_connection(
            self.connection,
            baud=self.baud,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=True,
        )

    def _open(self) -> bool:
        try:
            self._conn = self._connect()
        except (OSError, ValueError) as exc:
            log_event(log, logging.WARNING, "connect_failed", reason=str(exc))
            self._conn = None
            return False
        return True

    def _read_loop(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            if self._conn is None:
                if not self._open():
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
                backoff = 0.5
            try:
                msg = self._conn.recv_match(blocking=True, timeout=0.2)
            except (OSError, ValueError) as exc:
                # USB serial unplugged or SITL restarted: drop the handle and reopen.
                log_event(log, logging.WARNING, "link_error", reason=str(exc))
                ml.close_quietly(self._conn)
                self._conn = None
                self.reconnects += 1
                self._stop.wait(1.0)
                continue
            if msg is None or msg.get_type() == "BAD_DATA":
                continue
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            with contextlib.suppress(RuntimeError):  # loop closed during shutdown
                loop.call_soon_threadsafe(self._dispatch, msg)

    def _dispatch(self, msg) -> None:
        self.messages_received += 1
        now = time.monotonic()
        if msg.get_type() == "HEARTBEAT" and ml.is_autopilot_heartbeat(msg):
            self.target_system = msg.get_srcSystem()
            self.target_component = msg.get_srcComponent()
            self.watchdog.heartbeat_received(now)
            self.watchdog.check(now)
        elif (
            msg.get_srcSystem() == self.target_system
            and msg.get_srcComponent() == self.target_component
        ):
            self.watchdog.message_received(now)
        self.state.apply(msg, now)
        for listener in list(self._listeners):
            try:
                listener(msg)
            except Exception:
                log.exception("message listener failed")

    async def _heartbeat_loop(self) -> None:
        while True:
            ok = self.send(
                lambda m: m.heartbeat_send(
                    mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                    mavlink.MAV_AUTOPILOT_INVALID,
                    0,
                    0,
                    mavlink.MAV_STATE_ACTIVE,
                )
            )
            if ok:
                self.watchdog.heartbeat_sent()
            await asyncio.sleep(self.heartbeat_period)

    async def _watchdog_loop(self) -> None:
        while True:
            self.watchdog.check()
            await asyncio.sleep(0.1)

    def _on_link_event(self, event: LinkEvent) -> None:
        level = logging.INFO if event.status is LinkStatus.UP else logging.WARNING
        log_event(
            log,
            level,
            event.name,
            reason=event.reason,
            rx_age_s=event.rx_age_s,
            tx_age_s=event.tx_age_s,
            sysid=self.target_system,
            compid=self.target_component,
        )
        if event.status is LinkStatus.UP:
            self._connected.set()
            if event.previous in (None, LinkStatus.WAITING, LinkStatus.LOST):
                # A fresh link (or a rebooted Pixhawk) needs its streams asked for again.
                self._stream_requested.clear()
            if self._streams_task is None or self._streams_task.done():
                self._streams_task = asyncio.get_running_loop().create_task(
                    self._stream_loop(), name="mavlink-streams"
                )

    async def _stream_loop(self) -> None:
        """Ask for each stream, then keep re-asking for any that go quiet."""
        while self.watchdog.status is not LinkStatus.LOST:
            now = time.monotonic()
            for name, hz in self.streams.items():
                if hz <= 0:
                    continue
                last_req = self._stream_requested.get(name)
                if last_req is not None and now - last_req < 5.0:
                    continue
                if last_req is not None and self._stream_fresh(name, hz, now):
                    continue
                await self.request_stream(name, hz)
                self._stream_requested[name] = time.monotonic()
            await asyncio.sleep(1.0)

    def _stream_fresh(self, name: str, hz: float, now: float) -> bool:
        group = _STREAM_GROUP.get(name)
        if group is None:
            return True  # can't tell; don't spam requests
        age = getattr(self.state, group).age(now)
        return age is not None and age < max(3.0 / hz, 2.0)

    async def request_stream(self, name: str, hz: float) -> bool:
        """Ask for one message at ``hz``. Uses SET_MESSAGE_INTERVAL and falls back to
        REQUEST_DATA_STREAM if the autopilot refuses it (some pre-4.0 ArduCopter
        builds). Returns True if the autopilot accepted SET_MESSAGE_INTERVAL."""
        msg_id = getattr(mavlink, f"MAVLINK_MSG_ID_{name}", None)
        if msg_id is None:
            log_event(log, logging.WARNING, "unknown_stream", message=name)
            return False
        if not self._legacy_streams and self.commands is not None:
            result = await self.commands.send(
                mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                msg_id,
                int(1e6 / hz),
                retries=0,
                timeout_s=0.7,
            )
            if result.ok:
                return True
            if result.result is not None:
                # A real refusal (not a timeout): switch to the old method for good.
                self._legacy_streams = True
                log_event(
                    log,
                    logging.INFO,
                    "legacy_streams",
                    reason=f"SET_MESSAGE_INTERVAL answered {result.result_name}",
                )
        elif self.commands is None:
            self.command_long(mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, [msg_id, int(1e6 / hz)])
        stream = _LEGACY_STREAM.get(name)
        if stream is not None:
            rate = max(1, round(hz))
            self.send(
                lambda m: m.request_data_stream_send(
                    self.target_system, self.target_component, stream, rate, 1
                )
            )
        return False


_STREAM_GROUP = {
    "ATTITUDE": "attitude",
    "GLOBAL_POSITION_INT": "position",
    "GPS_RAW_INT": "gps",
    "SYS_STATUS": "battery",
    "VFR_HUD": "hud",
    "EKF_STATUS_REPORT": "ekf",
    "RC_CHANNELS": "rc",
}
