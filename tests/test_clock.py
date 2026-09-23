from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from platform_ops.common.clock import Clock, SimulatedClock, SystemClock

START = datetime(2026, 1, 5, 0, 0, 0)


def test_simulated_clock_starts_where_it_was_told() -> None:
    clock = SimulatedClock(START)
    assert clock.now() == START


def test_two_clocks_advanced_identically_agree_step_for_step() -> None:
    """Determinism is the whole point: same inputs, same timestamps."""
    a = SimulatedClock(START, step=timedelta(minutes=15))
    b = SimulatedClock(START, step=timedelta(minutes=15))
    left = [a.tick() for _ in range(50)]
    right = [b.tick() for _ in range(50)]
    assert left == right


def test_real_time_passing_does_not_move_a_simulated_clock() -> None:
    clock = SimulatedClock(START)
    before = clock.now()
    time.sleep(0.05)
    assert clock.now() == before


def test_advance_is_monotonic_and_refuses_to_go_back() -> None:
    clock = SimulatedClock(START)
    seen = [clock.now()]
    for _ in range(10):
        seen.append(clock.advance(timedelta(hours=3)))
    assert seen == sorted(seen)
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance(timedelta(hours=-1))


def test_advance_to_rejects_a_time_in_the_past() -> None:
    clock = SimulatedClock(START)
    clock.advance_to(START + timedelta(days=3))
    with pytest.raises(ValueError, match="cannot move back"):
        clock.advance_to(START)


def test_schedule_walks_the_window_and_carries_the_clock_with_it() -> None:
    clock = SimulatedClock(START)
    until = START + timedelta(hours=5)
    stamps = list(clock.schedule(until=until, every=timedelta(hours=1)))
    assert stamps == [START + timedelta(hours=h) for h in range(6)]
    # The clock ends past the window, matching the last advance.
    assert clock.now() == START + timedelta(hours=6)


def test_daily_at_produces_one_run_per_day_at_the_configured_hour() -> None:
    """The SPEC schedules the dbt run at 02:00 simulated time, every day."""
    clock = SimulatedClock(START)
    runs = list(clock.daily_at(hour=2, days=6))
    assert len(runs) == 6
    assert {run.hour for run in runs} == {2}
    assert runs[0] == datetime(2026, 1, 5, 2)
    assert runs[-1] == datetime(2026, 1, 10, 2)
    assert runs == sorted(runs)


def test_six_weeks_of_daily_runs_costs_no_real_time() -> None:
    """Weeks of simulated time in milliseconds of real time."""
    clock = SimulatedClock(START)
    started = time.perf_counter()
    runs = list(clock.daily_at(hour=2, days=42))
    elapsed = time.perf_counter() - started
    assert len(runs) == 42
    assert runs[-1] - runs[0] == timedelta(days=41)
    assert elapsed < 1.0


def test_reset_returns_to_the_start() -> None:
    clock = SimulatedClock(START)
    clock.tick(10)
    clock.reset()
    assert clock.now() == START


def test_invalid_construction_and_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="step must be positive"):
        SimulatedClock(START, step=timedelta(0))
    clock = SimulatedClock(START)
    with pytest.raises(ValueError, match="count must not be negative"):
        clock.tick(-1)
    with pytest.raises(ValueError, match="every must be positive"):
        list(clock.schedule(until=START, every=timedelta(0)))
    with pytest.raises(ValueError, match="hour must be between"):
        list(clock.daily_at(hour=24, days=1))


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SimulatedClock(START), Clock)
    assert isinstance(SystemClock(), Clock)


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None
