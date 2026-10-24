"""Faults in operation: the catalogue, the schedule, and where production falls in the window."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from platform_ops.common.config import load_settings
from platform_ops.simulation import faults, raw_data
from platform_ops.simulation.company import Timeline
from platform_ops.simulation.faults import CATALOGUE, schedule
from platform_ops.simulation.ingest import raw_columns

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_fault_targets_a_raw_table_and_a_real_column() -> None:
    for fault in CATALOGUE:
        assert fault.table in raw_data.TABLES, fault.fault_id
        columns = dict(raw_columns(fault.table))
        if fault.column:
            assert fault.column in columns, fault.fault_id
            # A null spike needs a column that can hold NULL.
            assert columns[fault.column].startswith("Nullable("), fault.fault_id


def test_every_fault_aims_at_a_check_the_project_has() -> None:
    """The test a fault should trip is declared on the staging model reading its table."""
    staging = yaml.safe_load((REPO_ROOT / "dbt/models/staging/_staging.yml").read_text("utf-8"))
    tests = {}
    for model in staging["models"]:
        found = {
            str(t) if isinstance(t, str) else next(iter(t)) for t in model.get("data_tests", [])
        }
        for column in model.get("columns", []):
            for test in column.get("data_tests", []):
                name = test if isinstance(test, str) else next(iter(test))
                found.add(f"{name} on {column['name']}")
        tests[model["name"]] = found
    expected = {
        "F1": ("stg_orders", "not_null on customer_id"),
        "F2": ("stg_support_tickets", "accepted_values on priority"),
        "F3": ("stg_payments", "relationships on order_id"),
        "F6": ("stg_orders", "row_count_in_range"),
        "F8": ("stg_payments", "accepted_values on payment_status"),
    }
    for fault_id, (model, check) in expected.items():
        assert check in tests[model], fault_id
    # F9 is the exception on purpose: no test on any model checks its column,
    # so only the column profile on the source can catch it.
    (f9,) = [f for f in CATALOGUE if f.fault_id == "F9"]
    assert f9.loads_nulls and not any(f9.column in str(t) for t in tests["stg_web_sessions"])
    sources = yaml.safe_load((REPO_ROOT / "dbt/models/sources.yml").read_text("utf-8"))
    (feed,) = [t for t in sources["sources"][0]["tables"] if t["name"] == f9.table]
    assert "column_profile" in feed["data_tests"]


def test_duplicate_keys_are_gone_because_raw_deduplicates() -> None:
    types = {f.fault_type for f in CATALOGUE}
    assert "orphan_keys" in types and "duplicate_keys" not in types
    assert {f.fault_id for f in CATALOGUE if f.stops_loader} == {"F4", "F5", "F7"}


def test_faults_come_two_a_day_and_none_too_late_to_fix() -> None:
    planned = schedule([2, 5, 9, 14, 17], 21)
    assert {day: [f.fault_id for f in pair] for day, pair in planned.items()} == {
        2: ["F1", "F2"],
        5: ["F3", "F4"],
        9: ["F5", "F6"],
        14: ["F7", "F8"],
        17: ["F9"],
    }
    # A fault on the last day could not be caught and fixed inside the window.
    assert list(schedule([2, 6], 7)) == [2]
    for day, pair in planned.items():
        assert len({f.table for f in pair}) == len(pair), day


def test_production_comes_after_the_parallel_run(
    settings_dict: dict[str, object], write_settings: Callable[[dict[str, object]], Path]
) -> None:
    settings_dict["simulation"]["weeks"] = 16  # type: ignore[index]
    settings_dict["company"] = {
        "parallel_run_weeks": 6,
        "fix_after_nights": 7,
        "production_weeks": 3,
    }
    timeline = Timeline.of(load_settings(write_settings(settings_dict)), 112)
    assert (timeline.parallel_start, timeline.production_start) == (49, 91)
    assert [n for n in range(112) if timeline.parallel(n)] == list(range(49, 91))
    assert all(timeline.production(n) for n in range(91, 112))
    assert timeline.fix_night == 56
    # The parallel run's one-off faults stay inside it, each repaired the next night.
    assert all(49 < night < 90 for night in timeline.faults)
    assert sorted(timeline.incidents) == [93, 96, 100, 105]


def test_without_production_the_parallel_run_ends_the_window(
    settings_dict: dict[str, object], write_settings: Callable[[dict[str, object]], Path]
) -> None:
    settings_dict["company"] = {"parallel_run_weeks": 1, "fix_after_nights": 2}
    timeline = Timeline.of(load_settings(write_settings(settings_dict)), 14)
    assert (timeline.parallel_start, timeline.production_start) == (7, 14)
    assert not timeline.incidents
    assert not any(timeline.production(n) for n in range(14))


def test_the_ground_truth_table_names_every_fault_column() -> None:
    assert faults.GROUND_TRUTH_COLUMNS[0] == "fault_id"
    assert "repaired_at" in faults.GROUND_TRUTH_COLUMNS
