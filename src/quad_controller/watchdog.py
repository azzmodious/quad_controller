"""Link watchdog: heartbeat age in both directions, and events when the link changes.

The watchdog never reads the clock on its own; the link calls :meth:`check`
several times a second, which also makes it simple to test with a fake clock.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


class LinkStatus(enum.StrEnum):
    WAITING = "waiting"  # no autopilot heartbeat seen yet
    UP = "up"
    DEGRADED = "degraded"
    LOST = "lost"


@dataclass(frozen=True)
class LinkEvent:
    """A change in link status. ``previous`` is None for the first event."""

    status: LinkStatus
    previous: LinkStatus | None
    rx_age_s: float | None
    tx_age_s: float | None
    reason: str

    @property
    def name(self) -> str:
        if self.status is LinkStatus.UP:
            return "link_up" if self.previous in (None, LinkStatus.WAITING) else "link_restored"
        return f"link_{self.status.value}"


Listener = Callable[[LinkEvent], None]


class LinkWatchdog:
    """Tracks what we last received from the autopilot and what we last sent it.

    rx: the newest message from the autopilot (heartbeat or any stream). The
        autopilot's heartbeat is only 1 Hz, so on its own it would sit right at
        a 1 s threshold; the 20 Hz attitude stream keeps this age small while
        the link is healthy. If everything stops, the Pixhawk, cable or SITL is gone.
    tx: how late our own heartbeat is (age minus our send period). If sending
        fails (USB unplugged, socket error) it grows.

    The link is degraded once either passes ``degraded_after_s`` and lost once
    either passes ``lost_after_s``.
    """

    def __init__(
        self,
        degraded_after_s: float = 1.0,
        lost_after_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        tx_period_s: float = 1.0,
    ) -> None:
        if degraded_after_s >= lost_after_s:
            raise ValueError("degraded_after_s must be less than lost_after_s")
        self.degraded_after_s = degraded_after_s
        self.lost_after_s = lost_after_s
        self._clock = clock
        self.tx_period_s = tx_period_s
        self.status = LinkStatus.WAITING
        self.last_rx: float | None = None
        self.last_rx_heartbeat: float | None = None
        self.last_tx: float | None = None
        self.rx_count = 0
        self.tx_count = 0
        self.changed_at = clock()
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` on every status change. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def heartbeat_received(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.last_rx = self.last_rx_heartbeat = now
        self.rx_count += 1

    def message_received(self, now: float | None = None) -> None:
        """Any other message from the autopilot. Only counts once a heartbeat was seen."""
        if self.last_rx_heartbeat is not None:
            self.last_rx = self._clock() if now is None else now

    def heartbeat_sent(self, now: float | None = None) -> None:
        self.last_tx = self._clock() if now is None else now
        self.tx_count += 1

    def rx_age(self, now: float | None = None) -> float | None:
        if self.last_rx is None:
            return None
        return (self._clock() if now is None else now) - self.last_rx

    def tx_age(self, now: float | None = None) -> float | None:
        if self.last_tx is None:
            return None
        return (self._clock() if now is None else now) - self.last_tx

    def evaluate(self, now: float | None = None) -> tuple[LinkStatus, str]:
        """The status the ages imply right now, and why."""
        now = self._clock() if now is None else now
        rx = self.rx_age(now)
        if rx is None:
            return LinkStatus.WAITING, "no heartbeat from the autopilot yet"
        tx_age = self.tx_age(now)
        tx_late = 0.0 if tx_age is None else max(0.0, tx_age - self.tx_period_s)
        worst = max(rx, tx_late)
        what = "nothing from the autopilot" if rx >= tx_late else "our heartbeat not sent"
        if worst > self.lost_after_s:
            return LinkStatus.LOST, f"{what} for {worst:.1f}s"
        if worst > self.degraded_after_s:
            return LinkStatus.DEGRADED, f"{what} for {worst:.1f}s"
        return LinkStatus.UP, "heartbeats fresh"

    def check(self, now: float | None = None) -> LinkEvent | None:
        """Re-evaluate; on a change, notify listeners and return the event."""
        now = self._clock() if now is None else now
        status, reason = self.evaluate(now)
        if status is self.status:
            return None
        # Once seen, the link never goes back to WAITING.
        if status is LinkStatus.WAITING:
            return None
        event = LinkEvent(
            status=status,
            previous=self.status,
            rx_age_s=_r(self.rx_age(now)),
            tx_age_s=_r(self.tx_age(now)),
            reason=reason,
        )
        self.status = status
        self.changed_at = now
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a broken listener must not stop the watchdog
                log.exception("link listener failed")
        return event


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)
