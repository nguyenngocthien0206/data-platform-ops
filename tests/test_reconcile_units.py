"""Segmented diff, classification, sign-off rules and the parallel run's timeline.

The diff runs against a fake connector written in Python: it renders, hashes
and buckets rows with the Python reference (``canonical.py``), the same way
the engines do, so the algorithm is tested without a server.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from platform_ops.common.config import Settings
from platform_ops.reconcile.canonical import (
    NULL,
    Rule,
    canonical_value,
    key_bucket,
    row_hash,
    row_string,
)
from platform_ops.reconcile.classify import classify
from platform_ops.reconcile.connectors import Summary
from platform_ops.reconcile.diff import diff_table, leaf_depth
from platform_ops.reconcile.metrics import Discrepancy, grade, neutralized_by_policy, verdict
from platform_ops.reconcile.parallel import decide
from platform_ops.reconcile.schema import Column, TableSpec, kind_from_postgres
from platform_ops.simulation.company import Timeline
from platform_ops.simulation.migration import (
    CATALOGUE,
    TruthCell,
    _shifted,
    _through_float,
    fault_nights,
)

SPEC = TableSpec(
    "items",
    "table",
    ("id",),
    (
        Column("id", "int"),
        Column("name", "text"),
        Column("price", "decimal", scale=2),
        Column("seen", "ts"),
    ),
    source='"app"."items"',
    target="`raw`.`items`",
)
RULES = {c.name: Rule(scale=c.scale) for c in SPEC.columns}
TOLERANCE = Decimal("0.01")


class Fake:
    """A connector over Python rows, rendered and hashed by the reference."""

    engine = "fake"
    dialect = "postgres"

    def __init__(self, spec: TableSpec, rules: dict[str, Rule], rows: list[dict[str, Any]]):
        self.rows = []
        for row in rows:
            values = {c.name: canonical_value(c, row[c.name], rules[c.name]) for c in spec.columns}
            key = row_string([values[k] for k in spec.key])
            text = row_string([values[c.name] for c in spec.columns])
            h1, h2 = row_hash(text)
            self.rows.append(
                (key_bucket(key, 8), key, h1, h2, tuple(values[c.name] for c in spec.value_columns))
            )

    def summarize(
        self, table: Any, rendered: Any, depth: int, parents: Sequence[int]
    ) -> dict[int, Summary]:
        found: dict[int, list[int]] = {}
        for bucket, _, h1, h2, _ in self.rows:
            if depth > 1 and bucket >> (32 - 4 * (depth - 1)) not in parents:
                continue
            tally = found.setdefault(bucket >> (32 - 4 * depth), [0, 0, 0])
            tally[0] += 1
            tally[1] += h1
            tally[2] += h2
        return {b: Summary(*t) for b, t in found.items()}

    def fetch(
        self, table: Any, rendered: Any, depth: int, buckets: Sequence[int]
    ) -> dict[str, tuple[str, ...]]:
        return {
            key: values
            for bucket, key, _, _, values in self.rows
            if bucket >> (32 - 4 * depth) in buckets
        }

    def forget(self) -> None:
        return None


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "name": f"item {i}",
            "price": Decimal(f"{i}.50"),
            "seen": datetime(2025, 1, 1, i % 24),
        }
        for i in range(1, n + 1)
    ]


def _diff(left: list[dict[str, Any]], right: list[dict[str, Any]], leaf: int = 16) -> Any:
    return diff_table(
        SPEC, Fake(SPEC, RULES, left), Fake(SPEC, RULES, right), RULES, leaf_width=leaf
    )


# -- the segmented diff --------------------------------------------------------------------


def test_the_leaf_depth_grows_with_the_table() -> None:
    assert leaf_depth(100, 256) == 1
    assert leaf_depth(256 * 16, 256) == 1
    assert leaf_depth(256 * 16 + 1, 256) == 2
    assert leaf_depth(2_000_000, 256) == 4


def test_identical_tables_stop_at_the_first_level() -> None:
    result = _diff(_rows(1000), _rows(1000))
    assert result.clean
    assert [(depth, differing) for depth, _, differing in result.levels] == [(1, 0)]
    assert result.fetched_rows == 0
    assert result.transferred < result.naive_transferred / 50


def test_a_few_differences_are_found_and_little_is_moved() -> None:
    left = _rows(5000)
    right = [dict(r) for r in left if r["id"] != 17]
    right[99]["price"] = Decimal("0.00")
    right.append(
        {"id": 9999, "name": "new", "price": Decimal("1.00"), "seen": datetime(2025, 1, 1)}
    )
    result = _diff(left, right)
    assert result.missing == ["17"]
    assert result.extra == ["9999"]
    assert [(c.key, c.column) for c in result.cells] == [(str(right[99]["id"]), "price")]
    assert result.transferred < result.naive_transferred / 4


def test_every_row_different_fetches_everything() -> None:
    left = _rows(300)
    right = [{**r, "name": r["name"].upper()} for r in left]
    result = _diff(left, right)
    assert result.rows_with_cell_diffs == 300
    assert result.fetched_rows == 600


def test_an_empty_side_is_all_missing() -> None:
    result = _diff(_rows(50), [])
    assert sorted(result.missing, key=int) == [str(i) for i in range(1, 51)]


def test_a_composite_date_key_segments_like_any_other() -> None:
    spec = TableSpec(
        "daily",
        "report",
        ("day", "channel"),
        (Column("day", "date"), Column("channel", "text"), Column("orders", "int")),
        source='"reporting"."daily"',
        target="`marts`.`daily`",
    )
    rules = {c.name: Rule() for c in spec.columns}
    left = [
        {"day": date(2025, 1, 1) + timedelta(days=d), "channel": ch, "orders": d}
        for d in range(400)
        for ch in ("web", "mobile")
    ]
    right = [dict(r) for r in left]
    right[123]["orders"] = -1
    result = diff_table(
        spec, Fake(spec, rules, left), Fake(spec, rules, right), rules, leaf_width=16
    )
    key = row_string([left[123]["day"].isoformat(), left[123]["channel"]])
    assert [(c.key, c.column, c.target) for c in result.cells] == [(key, "orders", "-1")]


# -- classification ---------------------------------------------------------------------

TEXT = Column("t", "text")
DEC = Column("d", "decimal", scale=2)
TS = Column("ts", "ts")


@pytest.mark.parametrize(
    ("column", "source", "target", "expected"),
    [
        (DEC, "10.00", "10.01", "rounding"),
        (DEC, "10.00", "10.02", "value_mismatch"),
        (DEC, "10.00", NULL, "value_mismatch"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T18:00:00.000000Z", "timezone_shift"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T09:00:00.000000Z", "timezone_shift"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T14:30:00.000000Z", "value_mismatch"),
        (TEXT, "a@b.com ", "a@b.com", "whitespace"),
        (TEXT, "a@b.com", "A@B.COM", "case_only"),
        (TEXT, "card", "crypto", "value_mismatch"),
        (TEXT, "", NULL, "value_mismatch"),
    ],
)
def test_classification(column: Column, source: str, target: str, expected: str) -> None:
    assert classify(column, source, target, TOLERANCE) == expected


@pytest.mark.parametrize(
    ("data_type", "kind"),
    [
        ("bigint", "int"),
        ("numeric", "decimal"),
        ("double precision", "decimal"),
        ("timestamp without time zone", "ts"),
        ("date", "date"),
        ("boolean", "bool"),
        ("character varying", "text"),
    ],
)
def test_postgres_types_map_to_kinds(data_type: str, kind: str) -> None:
    assert kind_from_postgres(data_type) == kind


# -- verdict and grading ------------------------------------------------------------------


def test_verdict_fails_on_rates_and_class_limits(repo_settings: Settings) -> None:
    left = _rows(1000)
    right = [{**r, "price": Decimal("0.00")} if r["id"] <= 5 else r for r in left]
    diff = _diff(left, right)
    found = [
        Discrepancy("items", c.key, c.column, "value_mismatch", c.source, c.target)
        for c in diff.cells
    ]
    result = verdict(SPEC, diff, found, repo_settings.reconcile.thresholds)
    assert result.row_match_rate == pytest.approx(0.995)
    assert result.column_match_rates["price"] == pytest.approx(0.995)
    assert result.column_match_rates["name"] == 1.0
    assert not result.passed
    assert any("value_mismatch" in f for f in result.failures)


def test_a_clean_table_passes(repo_settings: Settings) -> None:
    diff = _diff(_rows(1000), _rows(1000))
    assert verdict(SPEC, diff, [], repo_settings.reconcile.thresholds).passed


def test_grading_counts_misses_false_alarms_and_wrong_classes() -> None:
    truth = [
        TruthCell("items", "1", "name", "case_only", "injected", "M4"),
        TruthCell("items", "2", "price", "rounding", "injected", "M6"),
        TruthCell("items", "3", "", "missing_in_target", "injected", "M2"),
    ]
    found = [
        Discrepancy("items", "1", "name", "case_only", "a", "A"),
        Discrepancy("items", "2", "price", "value_mismatch", "1.00", "1.50"),
        Discrepancy("items", "9", "name", "value_mismatch", "x", "y"),
    ]
    g = grade(truth, found, {"items": SPEC}, {"items": RULES})
    assert (g.expected, g.detected, g.true_positives, g.correctly_classified) == (3, 3, 2, 1)
    assert g.by_label == {"M2": (1, 0), "M4": (1, 1), "M6": (1, 1)}


def test_a_case_change_is_not_a_miss_when_the_owner_ignores_case() -> None:
    folded = {**RULES, "name": Rule(casefold=True)}
    cell = TruthCell("items", "1", "name", "case_only", "injected", "M4")
    assert neutralized_by_policy(cell, SPEC, folded)
    assert not neutralized_by_policy(cell, SPEC, RULES)
    g = grade([cell], [], {"items": SPEC}, {"items": folded})
    assert (g.expected, g.equivalent_under_policy, g.recall) == (0, 1, 1.0)


# -- the cutover rule ---------------------------------------------------------------------


def _nights(pattern: str) -> list[Any]:
    start = datetime(2026, 2, 23, 2)
    return [
        SimpleNamespace(
            clean=flag == "c",
            night=SimpleNamespace(index=49 + i, run_at=start + timedelta(days=i)),
        )
        for i, flag in enumerate(pattern)
    ]


def test_the_cutover_comes_on_the_night_that_completes_the_streak() -> None:
    decision = decide(_nights("xxccxccc" + "c" * 10), required=3)
    assert decision.recommended and decision.night == 49 + 7
    assert decision.run_at == datetime(2026, 3, 2, 2)


def test_no_streak_long_enough_means_no_cutover() -> None:
    decision = decide(_nights("xccxccxcc"), required=3)
    assert not decision.recommended and decision.night is None
    assert decision.clean_nights == 2


# -- the company's timeline and the planted discrepancies -----------------------------------


def test_the_parallel_run_is_the_last_weeks_and_the_fix_ships_inside_it(
    repo_settings: Settings,
) -> None:
    timeline = Timeline.of(repo_settings, 91)
    assert (timeline.parallel_start, timeline.fix_night) == (49, 56)
    assert not timeline.parallel(48) and timeline.parallel(49)
    assert timeline.delivered(0) and timeline.delivered(55) and not timeline.delivered(56)
    nights = sorted(timeline.faults)
    assert nights == [58, 60, 62, 64, 66, 68, 70]
    assert [timeline.faults[n].fault_id for n in nights] == [f.fault_id for f in CATALOGUE]


def test_faults_never_fall_on_the_last_night() -> None:
    assert sorted(fault_nights(11, 13)) == [11]
    assert fault_nights(13, 13) == {}


def test_a_wall_clock_read_as_new_york_time_moves_by_the_offset_of_the_day() -> None:
    assert _shifted(datetime(2025, 7, 1, 12), "America/New_York")
    assert _shifted(datetime(2025, 1, 15, 12), "America/New_York")
    assert not _shifted(datetime(2025, 1, 15, 12), "UTC")


def test_a_decimal_through_a_float_is_truncated_on_the_way_back() -> None:
    assert _through_float(Decimal("4.35")) == Decimal("4.34")
    assert _through_float(Decimal("4.50")) == Decimal("4.50")
    assert _through_float(Decimal("5385.72")) == Decimal("5385.72")
    # 0.03 is 0.029999... as a float, but 3.0 exactly once ClickHouse scales it.
    assert _through_float(Decimal("0.03")) == Decimal("0.03")
