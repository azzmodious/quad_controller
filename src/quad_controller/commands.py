"""Command helper: COMMAND_LONG with retries, waiting for COMMAND_ACK.

Later phases call :meth:`CommandSender.send` and get a :class:`CommandResult`
that says clearly whether the autopilot accepted the command.

Safety: commands that can move the aircraft (arm, mode, takeoff, motor test,
mission start...) are refused unless the sender was built with
``allow_flight_commands=True``. Phase 1 only sets that for SITL; see
:func:`flight_commands_allowed`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from quad_controller.config import Config
from quad_controller.logging_setup import log_event
from quad_controller.mavlink import mavlink

log = logging.getLogger(__name__)

# Commands that arm, change mode or otherwise make the aircraft act.
FLIGHT_COMMANDS = frozenset(
    {
        mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        mavlink.MAV_CMD_DO_SET_MODE,
        mavlink.MAV_CMD_NAV_TAKEOFF,
        mavlink.MAV_CMD_NAV_LAND,
        mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
        mavlink.MAV_CMD_NAV_WAYPOINT,
        mavlink.MAV_CMD_NAV_LOITER_UNLIM,
        mavlink.MAV_CMD_DO_MOTOR_TEST,
        mavlink.MAV_CMD_DO_REPOSITION,
        mavlink.MAV_CMD_DO_CHANGE_SPEED,
        mavlink.MAV_CMD_DO_SET_SERVO,
        mavlink.MAV_CMD_DO_REPEAT_SERVO,
        mavlink.MAV_CMD_DO_SET_RELAY,
        mavlink.MAV_CMD_DO_FLIGHTTERMINATION,
        mavlink.MAV_CMD_MISSION_START,
        mavlink.MAV_CMD_DO_PARACHUTE,
        mavlink.MAV_CMD_PREFLIGHT_CALIBRATION,
        mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
    }
)

# Pre-4.0 ArduCopter does not always send this, but when it does we keep waiting.
_IN_PROGRESS = mavlink.MAV_RESULT_IN_PROGRESS


class CommandBlocked(PermissionError):
    """The command could move the aircraft and flight commands are not allowed."""


@dataclass(frozen=True)
class CommandResult:
    command: int
    ok: bool
    result: int | None  # MAV_RESULT value, None if no ACK arrived
    attempts: int
    elapsed_s: float

    @property
    def command_name(self) -> str:
        return command_name(self.command)

    @property
    def result_name(self) -> str:
        if self.result is None:
            return "NO_ACK"
        entry = mavlink.enums["MAV_RESULT"].get(self.result)
        return entry.name.removeprefix("MAV_RESULT_") if entry else str(self.result)


def command_name(command: int) -> str:
    entry = mavlink.enums["MAV_CMD"].get(command)
    return entry.name.removeprefix("MAV_CMD_") if entry else str(command)


def flight_commands_allowed(cfg: Config) -> bool:
    """Phase 1 rule: flight commands only against SITL, never over a serial port."""
    from quad_controller.link import is_serial

    return cfg.target.name == "sitl" and not is_serial(cfg.target.connection)


class CommandSender:
    """Sends commands over a :class:`~quad_controller.link.MavLink` and matches ACKs.

    COMMAND_ACK only carries the command id, so only one command with a given
    id is in flight at a time; different commands can overlap.
    """

    def __init__(self, link, *, allow_flight_commands: bool = False) -> None:
        self.link = link
        self.allow_flight_commands = allow_flight_commands
        self._waiters: dict[int, asyncio.Future] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        link.subscribe(self._on_message)
        link.commands = self

    def _on_message(self, msg) -> None:
        if msg.get_type() != "COMMAND_ACK":
            return
        # Ignore ACKs meant for another GCS on the same network (e.g. MAVProxy).
        target = getattr(msg, "target_system", 0)
        if target not in (0, self.link.source_system):
            return
        fut = self._waiters.get(msg.command)
        if fut is not None and not fut.done():
            fut.set_result(int(msg.result))

    async def send(
        self,
        command: int,
        *params: float,
        retries: int = 3,
        timeout_s: float = 1.0,
        in_progress_timeout_s: float = 10.0,
    ) -> CommandResult:
        """Send ``command`` and wait for its ACK, resending up to ``retries`` times.

        Raises :class:`CommandBlocked` for flight commands unless allowed.
        """
        if command in FLIGHT_COMMANDS and not self.allow_flight_commands:
            log_event(log, logging.WARNING, "command_blocked", command=command_name(command))
            raise CommandBlocked(
                f"{command_name(command)} is a flight command; not allowed on this target"
            )
        lock = self._locks.setdefault(command, asyncio.Lock())
        async with lock:
            return await self._send_locked(
                command, list(params), retries, timeout_s, in_progress_timeout_s
            )

    async def _send_locked(self, command, params, retries, timeout_s, in_progress_timeout_s):
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        result: int | None = None
        attempts = 0
        try:
            for attempt in range(retries + 1):
                attempts = attempt + 1
                fut = loop.create_future()
                self._waiters[command] = fut
                # MAVLink: confirmation counts resends so the autopilot can spot duplicates.
                self.link.command_long(command, params, confirmation=min(attempt, 255))
                try:
                    result = await asyncio.wait_for(fut, timeout_s)
                except TimeoutError:
                    continue
                while result == _IN_PROGRESS:
                    fut = loop.create_future()
                    self._waiters[command] = fut
                    try:
                        result = await asyncio.wait_for(fut, in_progress_timeout_s)
                    except TimeoutError:
                        break
                break
        finally:
            self._waiters.pop(command, None)
        res = CommandResult(
            command=command,
            ok=result == mavlink.MAV_RESULT_ACCEPTED,
            result=result,
            attempts=attempts,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        log_event(
            log,
            logging.DEBUG if res.ok else logging.INFO,
            "command_result",
            command=res.command_name,
            result=res.result_name,
            attempts=res.attempts,
            elapsed_s=res.elapsed_s,
        )
        return res

    # Convenience wrappers used by SITL tests and later phases.

    async def arm(self, arm: bool = True, force: bool = False) -> CommandResult:
        return await self.send(
            mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1 if arm else 0, 21196 if force else 0
        )

    async def set_mode(self, mode: str) -> CommandResult:
        from quad_controller.mavlink import mavutil

        mapping = mavutil.mode_mapping_bynumber(mavlink.MAV_TYPE_QUADROTOR) or {}
        by_name = {v: k for k, v in mapping.items()}
        if mode.upper() not in by_name:
            raise ValueError(f"unknown copter mode {mode!r}")
        return await self.send(
            mavlink.MAV_CMD_DO_SET_MODE,
            mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            by_name[mode.upper()],
        )

    async def takeoff(self, alt_m: float) -> CommandResult:
        return await self.send(mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, alt_m)
