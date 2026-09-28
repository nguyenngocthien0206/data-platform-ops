from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from platform_ops.common.clock import Clock, SimulatedClock

START = datetime(2026, 1, 5, 0, 0, 0)


def test_simulated_clock_starts_where_it_was_told() -> None:
    clock = SimulatedClock(START)
    assert clock.now() == START
    assert clock.start == START


def test_two_clocks_moved_identically_agree_step_for_step() -> None:
    """Determinism is the whole point: same inputs, same timestamps."""
    a, b = SimulatedClock(START), SimulatedClock(START)
    left = [a.advance(timedelta(minutes=15)) for _ in range(50)]
    right = [b.advance(timedelta(minutes=15)) for _ in range(50)]
    assert left == right


def test_real_time_passing_does_not_move_a_simulated_clock() -> None:
    clock = SimulatedClock(START)
    before = clock.now()
    time.sleep(0.05)
    assert clock.now() == before


def test_advance_is_monotonic_and_refuses_to_go_back() -> None:
    clock = SimulatedClock(START)
    seen = [clock.now()]
    seen += [clock.advance(timedelta(hours=3)) for _ in range(10)]
    assert seen == sorted(seen)
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance(timedelta(hours=-1))


def test_advance_to_rejects_a_time_in_the_past() -> None:
    clock = SimulatedClock(START)
    clock.advance_to(START + timedelta(days=3))
    assert clock.now() == START + timedelta(days=3)
    with pytest.raises(ValueError, match="cannot move back"):
        clock.advance_to(START)


def test_weeks_of_daily_runs_cost_no_real_time() -> None:
    """Weeks of simulated time in milliseconds of real time."""
    clock = SimulatedClock(START)
    started = time.perf_counter()
    runs = [clock.advance_to(START + timedelta(days=d, hours=2)) for d in range(91)]
    assert runs[-1] - runs[0] == timedelta(days=90)
    assert time.perf_counter() - started < 1.0


def test_the_simulated_clock_satisfies_the_protocol() -> None:
    assert isinstance(SimulatedClock(START), Clock)
