"""Simulated time.

Six weeks of platform activity has to be generated in minutes of real time, and
two runs on a clean checkout must produce identical reports. Both requirements
come down to the same rule: nothing in the project reads the wall clock except
:class:`SystemClock`. Everything else takes a :class:`Clock` and asks it for the
time, so simulated timestamps are the ones that end up in every logged event,
every query record and every incident.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current time."""

    def now(self) -> datetime:  # pragma: no cover - protocol definition
        ...


class SimulationWindowLike(Protocol):
    """The slice of the settings that :class:`SimulatedClock` needs."""

    @property
    def start(self) -> datetime:  # pragma: no cover - protocol definition
        ...


class SystemClock:
    """Real wall-clock time. Used only for measuring how long something took."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class SimulatedClock:
    """A clock that only moves when it is told to.

    Deterministic by construction: two instances built with the same start and
    advanced by the same steps report identical sequences, no matter how long
    the real work between steps takes.
    """

    def __init__(self, start: datetime, step: timedelta | None = None) -> None:
        if step is not None and step <= timedelta(0):
            raise ValueError("step must be positive")
        self._start = start
        self._current = start
        self._step = step if step is not None else timedelta(hours=1)

    @classmethod
    def from_settings(cls, simulation: SimulationWindowLike) -> SimulatedClock:
        """Build a clock anchored at the configured simulated window start."""
        return cls(start=simulation.start)

    @property
    def start(self) -> datetime:
        return self._start

    @property
    def step(self) -> timedelta:
        return self._step

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta | None = None) -> datetime:
        """Move forward by ``delta``, or by the configured step. Never backwards."""
        moved = self._step if delta is None else delta
        if moved < timedelta(0):
            raise ValueError("a simulated clock cannot move backwards")
        self._current += moved
        return self._current

    def advance_to(self, when: datetime) -> datetime:
        """Jump to an absolute simulated time, which must not be in the past."""
        if when < self._current:
            raise ValueError(
                f"cannot move back from {self._current.isoformat()} to {when.isoformat()}"
            )
        self._current = when
        return self._current

    def tick(self, count: int = 1) -> datetime:
        """Advance ``count`` steps."""
        if count < 0:
            raise ValueError("count must not be negative")
        for _ in range(count):
            self.advance()
        return self._current

    def schedule(self, until: datetime, every: timedelta) -> Iterator[datetime]:
        """Yield simulated timestamps from now to ``until``, advancing the clock.

        This is how the workload generator walks a six-week window: the caller
        gets one timestamp per scheduled event and the clock stays in step with
        it, so anything logged inside the loop carries the right simulated time.
        """
        if every <= timedelta(0):
            raise ValueError("every must be positive")
        while self._current <= until:
            yield self._current
            self.advance(every)

    def daily_at(self, hour: int, days: int) -> Iterator[datetime]:
        """Yield one timestamp per day at ``hour``, for ``days`` days.

        Used for the scheduled dbt run, which the SPEC puts at 02:00 simulated
        time every day.
        """
        if not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        if days < 0:
            raise ValueError("days must not be negative")
        first = self._current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if first < self._current:
            first += timedelta(days=1)
        for day in range(days):
            self.advance_to(first + timedelta(days=day))
            yield self._current

    def reset(self) -> None:
        """Return to the start. Handy for running a scenario twice in one process."""
        self._current = self._start

    def __repr__(self) -> str:
        return f"SimulatedClock(now={self._current.isoformat()}, step={self._step})"
