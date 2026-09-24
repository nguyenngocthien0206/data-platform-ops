"""Match rates, the sign-off verdict, and grading against ground truth.

The verdict uses only what the diff found, judged against thresholds set in
``settings.yaml`` before the run. Ground truth is used only to grade the diff
itself: did it find every planted discrepancy, and did it name the right class?

A planted difference that the source's own canonical policy calls equal (a case
change, when the legacy system is case-insensitive) is not a miss. It is
counted separately as equivalent under policy, so the recall figure measures
the diff and not the policy.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from platform_ops.common.config import DISCREPANCY_CLASSES, DiscrepancyClass, Thresholds
from platform_ops.reconcile.canonical import Rule
from platform_ops.reconcile.diff import TableDiff
from platform_ops.reconcile.migrate import TruthCell
from platform_ops.reconcile.schema import TableSpec


@dataclass(frozen=True)
class Discrepancy:
    table: str
    key: int
    column: str  # empty for a whole missing or extra row
    klass: DiscrepancyClass
    source_value: str
    target_value: str

    @property
    def cell(self) -> tuple[str, int, str]:
        return (self.table, self.key, self.column)


@dataclass
class TableVerdict:
    table: str
    source_rows: int
    target_rows: int
    row_match_rate: float
    column_match_rates: dict[str, float]
    by_class: dict[DiscrepancyClass, int]
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Grade:
    expected: int
    detected: int
    true_positives: int
    correctly_classified: int
    equivalent_under_policy: int
    # label -> (planted, found)
    by_label: dict[str, tuple[int, int]]

    @property
    def recall(self) -> float:
        return self.true_positives / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.true_positives / self.detected if self.detected else 1.0

    @property
    def classification_accuracy(self) -> float:
        return self.correctly_classified / self.true_positives if self.true_positives else 1.0


def verdict(
    spec: TableSpec,
    diff: TableDiff,
    discrepancies: Sequence[Discrepancy],
    thresholds: Thresholds,
) -> TableVerdict:
    mine = [d for d in discrepancies if d.table == spec.name]
    by_class: dict[DiscrepancyClass, int] = {k: 0 for k in DISCREPANCY_CLASSES}
    for d in mine:
        by_class[d.klass] += 1
    in_both = diff.source_rows - len(diff.missing)
    rows_with_diffs = len({d.key for d in mine if d.column})
    matched = diff.source_rows - len(diff.missing) - rows_with_diffs
    row_rate = matched / diff.source_rows if diff.source_rows else 1.0
    per_column = Counter(d.column for d in mine if d.column)
    column_rates = {
        c.name: 1 - per_column.get(c.name, 0) / in_both if in_both else 1.0
        for c in spec.value_columns
    }
    result = TableVerdict(spec.name, diff.source_rows, diff.target_rows, row_rate, column_rates,
                          by_class)  # fmt: skip
    if row_rate < thresholds.min_row_match_rate:
        result.failures.append(
            f"row match rate {row_rate:.4%} is below {thresholds.min_row_match_rate:.2%}"
        )
    for name, rate in column_rates.items():
        if rate < thresholds.min_column_match_rate:
            result.failures.append(
                f"column {name} match rate {rate:.4%} is below "
                f"{thresholds.min_column_match_rate:.2%}"
            )
    for klass in DISCREPANCY_CLASSES:
        limit = thresholds.max_discrepancies[klass]
        if by_class[klass] > limit:
            result.failures.append(f"{by_class[klass]:,} {klass} over the limit of {limit:,}")
    return result


def neutralized_by_policy(cell: TruthCell, spec: TableSpec, rules: dict[str, Rule]) -> bool:
    """Whether the source's canonical policy says this planted difference is not one."""
    if not cell.column:
        return False
    rule = rules[cell.column]
    return (cell.klass == "case_only" and rule.casefold) or (
        cell.klass == "whitespace" and rule.rtrim
    )


def grade(
    truth: Sequence[TruthCell],
    discrepancies: Sequence[Discrepancy],
    specs: dict[str, TableSpec],
    rules: dict[str, dict[str, Rule]],
) -> Grade:
    expected: dict[tuple[str, int, str], TruthCell] = {}
    equivalent = 0
    for cell in truth:
        if neutralized_by_policy(cell, specs[cell.table], rules[cell.table]):
            equivalent += 1
        else:
            expected[(cell.table, cell.key, cell.column)] = cell
    found = {d.cell: d for d in discrepancies}
    hits = expected.keys() & found.keys()
    correct = sum(1 for cell in hits if found[cell].klass == expected[cell].klass)
    labels: dict[str, list[int]] = {}
    for key, cell in expected.items():
        tally = labels.setdefault(cell.label, [0, 0])
        tally[0] += 1
        tally[1] += key in found
    return Grade(
        expected=len(expected),
        detected=len(found),
        true_positives=len(hits),
        correctly_classified=correct,
        equivalent_under_policy=equivalent,
        by_label={label: (t[0], t[1]) for label, t in sorted(labels.items())},
    )
