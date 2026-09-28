"""Simulated time.

Thirteen weeks of workload and three weeks of incidents have to be generated in
minutes of real time, and two runs on a clean checkout must produce identical
reports. Both requirements come down to the same rule: nothing that ends up in
a report reads the wall clock. Everything takes a :class:`Clock` and asks it
for the time, so simulated timestamps are the ones in every logged event,
query record and incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current time."""

    def now(self) -> datetime:  # pragma: no cover - protocol definition
        ...


class SimulatedClock:
    """A clock that only moves when it is told to.

    Deterministic by construction: two instances built with the same start and
    moved the same way report identical times, no matter how long the real
    work in between takes.
    """

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._current = start

    @property
    def start(self) -> datetime:
        return self._start

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        """Move forward by ``delta``. Never backwards."""
        if delta < timedelta(0):
            raise ValueError("a simulated clock cannot move backwards")
        self._current += delta
        return self._current

    def advance_to(self, when: datetime) -> datetime:
        """Jump to an absolute simulated time, which must not be in the past."""
        if when < self._current:
            raise ValueError(
                f"cannot move back from {self._current.isoformat()} to {when.isoformat()}"
            )
        self._current = when
        return self._current

    def __repr__(self) -> str:
        return f"SimulatedClock(now={self._current.isoformat()})"
