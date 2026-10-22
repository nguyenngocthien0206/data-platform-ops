"""Segmented diff, classification, sign-off rules and the parallel run's timeline.

The diff runs against a fake connector written in Python: it renders, hashes
and buckets rows with the Python reference (``canonical.py``), the same way
the engines do, so the algorithm is tested without a server.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
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
from platform_ops.reconcile.parallel import decide, discrepancies_of
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
    """A connector over Python rows, rendered and hashed by the reference.

    As the legacy side (``legacy=True``) it answers as of the pair's
    watermark, by the row's ``changed_at`` value, the way Postgres does.
    """

    engine = "fake"
    dialect = "postgres"

    def __init__(
        self,
        spec: TableSpec,
        rules: dict[str, Rule],
        rows: list[dict[str, Any]],
        legacy: bool = False,
    ):
        self.legacy = legacy
        self.rows = []
        for row in rows:
            values = {c.name: canonical_value(c, row[c.name], rules[c.name]) for c in spec.columns}
            key = row_string([values[k] for k in spec.key])
            text = row_string([values[c.name] for c in spec.columns])
            h1, h2 = row_hash(text)
            changed = row[spec.changed_at] if spec.changed_at else None
            self.rows.append(
                (
                    key_bucket(key, 8),
                    key,
                    h1,
                    h2,
                    tuple(values[c.name] for c in spec.value_columns),
                    changed,
                )
            )

    def _after(self, table: TableSpec, changed: datetime | None) -> bool:
        return (
            self.legacy
            and table.watermark is not None
            and changed is not None
            and changed > table.watermark
        )

    def _visible(self, table: TableSpec) -> list[Any]:
        return [r for r in self.rows if not self._after(table, r[5])]

    def summarize(
        self, table: Any, rendered: Any, depth: int, parents: Sequence[int]
    ) -> dict[int, Summary]:
        found: dict[int, list[int]] = {}
        for bucket, _, h1, h2, _, _ in self._visible(table):
            if depth > 1 and bucket >> (32 - 4 * (depth - 1)) not in parents:
                continue
            tally = found.setdefault(bucket >> (32 - 4 * depth), [0, 0, 0])
            tally[0] += 1
            tally[1] += h1
            tally[2] += h2
        return {b: Summary(*t) for b, t in found.items()}

    def fetch(
        self, table: Any, rendered: Any, depth: int, buckets: Sequence[int]
    ) -> list[tuple[str, tuple[str, ...]]]:
        return [
            (key, values)
            for bucket, key, _, _, values, _ in self._visible(table)
            if bucket >> (32 - 4 * depth) in buckets
        ]

    def count_after(self, table: TableSpec) -> int:
        return sum(1 for r in self.rows if self._after(table, r[5]))

    def keys_after(
        self, table: TableSpec, rendered: Any, depth: int, buckets: Sequence[int]
    ) -> list[str]:
        return [
            key
            for bucket, key, _, _, _, changed in self.rows
            if self._after(table, changed) and bucket >> (32 - 4 * depth) in buckets
        ]

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


def _diff(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    leaf: int = 16,
    spec: TableSpec = SPEC,
) -> Any:
    return diff_table(
        spec,
        Fake(spec, RULES, left, legacy=True),
        Fake(spec, RULES, right),
        RULES,
        leaf_width=leaf,
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
        spec, Fake(spec, rules, left, legacy=True), Fake(spec, rules, right), rules, leaf_width=16
    )
    key = row_string([left[123]["day"].isoformat(), left[123]["channel"]])
    assert [(c.key, c.column, c.target) for c in result.cells] == [(key, "orders", "-1")]


def test_a_row_doubled_in_the_target_is_a_duplicate_key(repo_settings: Settings) -> None:
    left = _rows(1000)
    right = [*left, dict(left[41])]
    diff = _diff(left, right)
    assert [(d.key, d.source, d.target) for d in diff.duplicates] == [("42", 1, 2)]
    assert not diff.missing and not diff.extra and not diff.cells
    assert not diff.clean
    found = discrepancies_of(SPEC, diff, TOLERANCE)
    assert [(d.key, d.klass, d.source_value, d.target_value) for d in found] == [
        ("42", "duplicate_key", "1 row", "2 rows")
    ]
    result = verdict(SPEC, diff, found, repo_settings.reconcile.thresholds)
    assert result.row_match_rate == pytest.approx(0.999)
    assert not result.passed
    assert result.failures == ["1 duplicate_key over the limit of 0"]


def test_a_row_doubled_in_the_source_is_found_too() -> None:
    left = _rows(500)
    diff = _diff([*left, dict(left[7])], left)
    assert [(d.key, d.source, d.target) for d in diff.duplicates] == [("8", 2, 1)]


def test_copies_that_differ_are_one_duplicate_and_no_value_mismatch() -> None:
    left = _rows(500)
    right = [*left, {**left[9], "price": Decimal("0.00")}]
    diff = _diff(left, right)
    assert [(d.key, d.source, d.target) for d in diff.duplicates] == [("10", 1, 2)]
    assert diff.cells == []


def test_row_counts_that_do_not_add_up_fail_the_pair(repo_settings: Settings) -> None:
    diff = _diff(_rows(1000), _rows(1000))
    lost = replace(diff, target_rows=diff.target_rows + 1)
    result = verdict(SPEC, lost, [], repo_settings.reconcile.thresholds)
    assert not result.passed
    assert result.failures == [
        "row counts differ (1,000 against 1,001) beyond the missing, extra, duplicated and "
        "in-flight rows found"
    ]


# -- as of the copy's watermark -------------------------------------------------------------

COPIED = datetime(2025, 1, 1, 23)  # every row of _rows changed at or before it
AS_OF = replace(SPEC, changed_at="seen", watermark=COPIED)
LATER = COPIED + timedelta(hours=1)


def test_a_row_written_after_the_watermark_waits_for_the_next_night() -> None:
    copy = _rows(1000)
    legacy = [*copy, {"id": 5000, "name": "new", "price": Decimal("1.00"), "seen": LATER}]
    diff = _diff(legacy, copy, spec=AS_OF)
    assert diff.clean and diff.after_watermark == 1
    assert diff.source_rows == 1000 and diff.fetched_rows == 0
    # Without the watermark, the same night reports it missing.
    assert _diff(legacy, copy).missing == ["5000"]


def test_a_row_changed_after_the_watermark_is_in_flight(repo_settings: Settings) -> None:
    copy = _rows(1000)
    legacy = [{**r, "name": "renamed", "seen": LATER} if r["id"] == 7 else r for r in copy]
    diff = _diff(legacy, copy, spec=AS_OF)
    assert diff.in_flight == ["7"] and diff.after_watermark == 1
    assert diff.clean and not diff.extra and not diff.cells
    found = discrepancies_of(AS_OF, diff, TOLERANCE)
    assert found == []
    result = verdict(AS_OF, diff, found, repo_settings.reconcile.thresholds, LATER)
    assert result.passed, result.failures
    assert result.row_match_rate == 1.0


def test_a_difference_before_the_watermark_is_still_found() -> None:
    legacy = _rows(1000)
    copy = [{**r, "price": Decimal("0.00")} if r["id"] == 3 else r for r in legacy]
    copy = [r for r in copy if r["id"] != 4]
    legacy.append({"id": 5000, "name": "new", "price": Decimal("1.00"), "seen": LATER})
    diff = _diff(legacy, copy, spec=AS_OF)
    assert diff.missing == ["4"]
    assert [(c.key, c.column) for c in diff.cells] == [("3", "price")]
    assert diff.in_flight == [] and diff.after_watermark == 1


def test_a_legacy_side_too_far_behind_fails_the_pair(repo_settings: Settings) -> None:
    diff = _diff(_rows(100), _rows(100), spec=AS_OF)
    thresholds = repo_settings.reconcile.thresholds
    assert verdict(AS_OF, diff, [], thresholds, COPIED + timedelta(hours=26)).passed
    late = verdict(AS_OF, diff, [], thresholds, COPIED + timedelta(hours=27))
    assert late.failures == [
        "the legacy side is as of 2025-01-01 23:00, 27.0 hours before the comparison, "
        "over the limit of 26"
    ]


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


def test_the_parallel_run_comes_before_production_and_the_fix_ships_inside_it(
    repo_settings: Settings,
) -> None:
    timeline = Timeline.of(repo_settings, 112)
    assert (timeline.parallel_start, timeline.fix_night) == (49, 56)
    assert not timeline.parallel(48) and timeline.parallel(49)
    assert timeline.parallel(90) and not timeline.parallel(91) and timeline.production(91)
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
