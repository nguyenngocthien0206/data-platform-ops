"""Why a report differs: a copy defect upstream, or a difference in the logic.

A legacy report that differs from the mart replacing it has two possible
causes. Either the tables it is built from differ too (the copy is wrong, and
fixing the copy fixes the report), or they do not and the new model computes
something else (a logic difference, the one a finance user finds after the
cutover). Only the second blocks a night on its own.

Explaining by table, as version 2.0.0 did, was too loose: trimmed emails made
every report built on customers "explained", the revenue report included. So
a report difference is explained through the columns it reads
(``metadata.column_lineage``): each report column that differs needs an
upstream raw column it depends on that differs tonight, or a table it reads
with rows missing, extra or duplicated (a row-level difference changes joins
and counts, so it reaches any column built on that table). A report row only
one side has needs the same of the report's key columns. A report whose mart
cannot be traced column by column falls back to its tables, and says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from platform_ops.metadata.column_lineage import RawColumn
from platform_ops.reconcile.diff import TableDiff
from platform_ops.reconcile.schema import TableSpec


@dataclass(frozen=True)
class Explanation:
    explained: bool
    # What explains it: "payments.amount" for a column, "web_sessions (rows)"
    # for a table with row-level differences, or a table name when untraced.
    causes: tuple[str, ...]
    # Traced column by column, or by table because the lineage was unavailable.
    traced: bool


def explain(
    report: TableSpec,
    diff: TableDiff,
    lineage: Mapping[str, frozenset[RawColumn]] | None,
    cells: set[RawColumn],
    rows: set[str],
) -> Explanation:
    """Whether a report's differences tonight are explained by the tables upstream.

    ``lineage`` maps each mart column to the raw columns it depends on, or is
    None when the mart could not be traced. ``cells`` are the raw columns that
    differ tonight, ``rows`` the raw tables with row-level differences (or that
    could not be compared).
    """
    traced = lineage is not None
    if diff.clean:
        return Explanation(True, (), traced)
    if lineage is None:
        differing = rows | {table for table, _ in cells}
        hit = tuple(sorted(differing.intersection(report.upstream)))
        return Explanation(bool(hit), hit, False)
    needed = {report.column(cell.column).target for cell in diff.cells}
    if diff.missing or diff.extra or diff.duplicates:
        needed |= {report.column(key).target for key in report.key}
    explained = True
    causes: set[str] = set()
    for column in sorted(needed):
        depends = lineage[column]
        found = {f"{t}.{c}" for t, c in depends if (t, c) in cells}
        found |= {f"{t} (rows)" for t in {t for t, _ in depends} if t in rows}
        explained = explained and bool(found)
        causes |= found
    return Explanation(explained, tuple(sorted(causes)), True)


__all__ = ["Explanation", "explain"]
