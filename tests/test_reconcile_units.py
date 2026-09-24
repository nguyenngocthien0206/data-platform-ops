"""Segmented diff, classification and sign-off rules, on small DuckDB tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest

from platform_ops.common.config import Settings
from platform_ops.reconcile.canonical import NULL, Rule
from platform_ops.reconcile.classify import classify
from platform_ops.reconcile.connectors import DuckDBConnector
from platform_ops.reconcile.diff import diff_table, widths
from platform_ops.reconcile.metrics import Discrepancy, grade, neutralized_by_policy, verdict
from platform_ops.reconcile.migrate import TruthCell
from platform_ops.reconcile.schema import Column, TableSpec

SPEC = TableSpec(
    "items",
    "id",
    (
        Column("id", "int"),
        Column("name", "text", length=20),
        Column("price", "decimal", precision=10, scale=2),
        Column("seen", "utc_ts"),
    ),
)
RULES = {c.name: Rule(scale=c.scale) for c in SPEC.columns}
TOLERANCE = Decimal("0.01")


def _connector(rows: list[tuple[int, str | None, str | None]]) -> DuckDBConnector:
    con = duckdb.connect()
    connector = DuckDBConnector(con, schema="side")
    table = pa.table(
        {
            "id": pa.array([r[0] for r in rows], pa.int64()),
            "name": pa.array([r[1] for r in rows], pa.string()),
            "price": pa.array([None if r[2] is None else Decimal(r[2]) for r in rows],
                              pa.decimal128(10, 2)),
            "seen": pa.array([datetime(2025, 1, 1, r[0] % 24) for r in rows],
                             pa.timestamp("us", tz="UTC")),
        }
    )  # fmt: skip
    connector.recreate({"items": table}, [SPEC])
    return connector


def _rows(n: int) -> list[tuple[int, str | None, str | None]]:
    return [(i, f"item {i}", f"{i}.50") for i in range(1, n + 1)]


def test_widths_nest_from_the_top_down_to_the_leaf() -> None:
    assert widths(100, 16, 256) == [256]
    assert widths(5000, 16, 256) == [4096, 256]
    assert widths(300_000, 16, 256) == [65536, 4096, 256]
    for level in (widths(10**7, 8, 100),):
        assert all(a == b * 8 for a, b in zip(level, level[1:], strict=False))


def test_identical_tables_stop_at_the_first_level() -> None:
    rows = _rows(5000)
    result = diff_table(SPEC, _connector(rows), _connector(rows), RULES, fanout=16, leaf_width=16)
    assert not (result.missing or result.extra or result.cells)
    assert len(result.levels) == 1 and result.levels[0][2] == 0
    assert result.fetched_rows == 0
    assert result.transferred < result.naive_transferred / 100


def test_a_few_differences_are_found_and_little_is_moved() -> None:
    left = _rows(5000)
    right = [r for r in left if r[0] != 42]  # missing
    right = [(r[0], r[1].upper() if r[0] == 1000 else r[1], r[2]) for r in right]  # type: ignore[union-attr]
    right = [(r[0], r[1], "3000.51" if r[0] == 3000 else r[2]) for r in right]
    right.append((5003, "invented", "1.00"))  # extra, beyond the source's key range
    result = diff_table(SPEC, _connector(left), _connector(right), RULES, fanout=16, leaf_width=16)
    assert result.missing == [42]
    assert result.extra == [5003]
    assert [(c.key, c.column) for c in result.cells] == [(1000, "name"), (3000, "price")]
    assert result.transferred < result.naive_transferred / 10


def test_every_row_different_fetches_everything() -> None:
    left = _rows(300)
    right = [(r[0], r[1], "0.00") for r in left]
    result = diff_table(SPEC, _connector(left), _connector(right), RULES, fanout=4, leaf_width=16)
    assert len(result.cells) == 300
    assert result.fetched_rows == 600
    assert result.transferred > result.naive_transferred


def test_an_empty_side_is_all_missing() -> None:
    left = _rows(50)
    empty = _connector([])
    result = diff_table(SPEC, _connector(left), empty, RULES, fanout=4, leaf_width=16)
    assert result.missing == list(range(1, 51))


# -- classification --------------------------------------------------------------------

TEXT = Column("t", "text")
DEC = Column("d", "decimal", precision=12, scale=2)
TS = Column("ts", "utc_ts")


@pytest.mark.parametrize(
    ("column", "source", "target", "expected"),
    [
        (DEC, "10.00", "10.01", "rounding"),
        (DEC, "10.00", "10.02", "value_mismatch"),
        (DEC, "10.00", NULL, "value_mismatch"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T15:00:00.000000Z", "timezone_shift"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T09:00:00.000000Z", "timezone_shift"),
        (TS, "2025-07-01T14:00:00.000000Z", "2025-07-01T14:30:00.000000Z", "value_mismatch"),
        (TEXT, "Acme   ", "Acme", "whitespace"),
        (TEXT, "Acme", " Acme", "whitespace"),
        (TEXT, "a@b.com", "A@B.COM", "case_only"),
        (TEXT, "card", "crypto", "value_mismatch"),
        (TEXT, "", NULL, "value_mismatch"),
    ],
)
def test_classification(column: Column, source: str, target: str, expected: str) -> None:
    assert classify(column, source, target, TOLERANCE) == expected


# -- verdict and grading ---------------------------------------------------------------


def test_verdict_fails_on_rates_and_class_limits(repo_settings: Settings) -> None:
    left = _rows(1000)
    right = [(r[0], r[1], "0.00" if r[0] <= 5 else r[2]) for r in left]
    diff = diff_table(SPEC, _connector(left), _connector(right), RULES, fanout=16, leaf_width=16)
    found = [Discrepancy("items", c.key, c.column, "value_mismatch", c.source, c.target)
             for c in diff.cells]  # fmt: skip
    result = verdict(SPEC, diff, found, repo_settings.reconcile.thresholds)
    assert result.row_match_rate == pytest.approx(0.995)
    assert result.column_match_rates["price"] == pytest.approx(0.995)
    assert result.column_match_rates["name"] == 1.0
    assert not result.passed
    assert any("value_mismatch" in f for f in result.failures)


def test_a_clean_table_passes(repo_settings: Settings) -> None:
    rows = _rows(1000)
    diff = diff_table(SPEC, _connector(rows), _connector(rows), RULES, fanout=16, leaf_width=16)
    assert verdict(SPEC, diff, [], repo_settings.reconcile.thresholds).passed


def test_grading_counts_misses_false_alarms_and_wrong_classes() -> None:
    truth = [
        TruthCell("items", 1, "name", "case_only", "injected", "M4"),
        TruthCell("items", 2, "price", "rounding", "injected", "M6"),
        TruthCell("items", 3, "", "missing_in_target", "injected", "M2"),
    ]
    found = [
        Discrepancy("items", 1, "name", "case_only", "a", "A"),
        Discrepancy("items", 2, "price", "value_mismatch", "1.00", "1.50"),
        Discrepancy("items", 9, "name", "value_mismatch", "x", "y"),
    ]
    g = grade(truth, found, {"items": SPEC}, {"items": RULES})
    assert (g.expected, g.detected, g.true_positives, g.correctly_classified) == (3, 3, 2, 1)
    assert g.by_label == {"M2": (1, 0), "M4": (1, 1), "M6": (1, 1)}


def test_a_case_change_is_not_a_miss_when_the_source_ignores_case() -> None:
    folded = {**RULES, "name": Rule(casefold=True)}
    cell = TruthCell("items", 1, "name", "case_only", "injected", "M4")
    assert neutralized_by_policy(cell, SPEC, folded)
    assert not neutralized_by_policy(cell, SPEC, RULES)
    g = grade([cell], [], {"items": SPEC}, {"items": folded})
    assert (g.expected, g.equivalent_under_policy, g.recall) == (0, 1, 1.0)
