"""The segmented diff: find differing rows without moving the table.

Both sides are split into segments by the MD5 of each row's canonical key: 16
segments at depth 1 (the first hex digit), 256 at depth 2, and so on. Each
side reports a row count and two hash sums per segment, computed inside its
own engine. Only segments whose summaries differ are split again, down to the
depth at which a segment holds about ``leaf_width`` rows. Only the rows of
differing leaves are fetched, as canonical strings, and compared here.

A key found more than once on either side is reported as a duplicated key, with
how many times each side holds it, and its cells are not compared: which copy
would be "the" row is unknown. A duplicate always shows in the summaries (one
more row, a different hash sum), so the descent reaches its leaf.

With a watermark (the copy's, ``TableSpec.watermark``), the legacy side is
compared as of it: a row written after it is in no summary, so it makes no
segment differ and costs nothing; ``after_watermark`` counts it. A row changed
in place after it still has its old version in the copy, which shows as a row
only the copy has. The legacy side is asked which keys of the leaves changed
after the watermark, and those are in flight, not extra: the next night, once
the copy has read them, compares them.

A child segment's number divided by 16 is its parent's, so every level is one
``GROUP BY`` per side whatever the number of segments, and the same descent
works for an integer key, a date or a key of several columns.

Everything that crosses from an engine to Python is counted: summary rows at
every level and fetched rows at the leaves. The naive alternative, fetching
both tables whole, is the benchmark the report compares against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from platform_ops.reconcile.canonical import Rule, render
from platform_ops.reconcile.connectors import Connector, Source, Summary
from platform_ops.reconcile.schema import TableSpec

FANOUT = 16
MAX_DEPTH = 8


@dataclass(frozen=True)
class CellDiff:
    key: str
    column: str
    source: str
    target: str


@dataclass(frozen=True)
class DuplicateKey:
    key: str
    source: int  # rows with this key on each side
    target: int


@dataclass
class TableDiff:
    table: str
    source_rows: int = 0
    target_rows: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    cells: list[CellDiff] = field(default_factory=list)
    duplicates: list[DuplicateKey] = field(default_factory=list)
    # Keys only the copy has whose legacy row changed after the watermark.
    in_flight: list[str] = field(default_factory=list)
    # Legacy rows changed after the watermark, left for the next night.
    after_watermark: int = 0
    # (depth, segments compared, segments differing) per level, top first.
    levels: list[tuple[int, int, int]] = field(default_factory=list)
    summary_rows: int = 0
    fetched_rows: int = 0
    queries: int = 0

    @property
    def transferred(self) -> int:
        return self.summary_rows + self.fetched_rows

    @property
    def naive_transferred(self) -> int:
        return self.source_rows + self.target_rows

    @property
    def rows_with_cell_diffs(self) -> int:
        return len({cell.key for cell in self.cells})

    @property
    def clean(self) -> bool:
        return not (self.missing or self.extra or self.cells or self.duplicates)


def leaf_depth(rows: int, leaf_width: int) -> int:
    """The depth at which a segment holds at most about ``leaf_width`` rows."""
    if rows <= leaf_width:
        return 1
    return min(MAX_DEPTH, max(1, math.ceil(math.log(rows / leaf_width, FANOUT))))


def _differing(source: dict[int, Summary], target: dict[int, Summary]) -> list[int]:
    return sorted(b for b in source.keys() | target.keys() if source.get(b) != target.get(b))


def diff_table(
    table: TableSpec,
    source: Source,
    target: Connector,
    rules: dict[str, Rule],
    *,
    leaf_width: int,
) -> TableDiff:
    result = TableDiff(table.name, after_watermark=source.count_after(table))
    source_sql = render(source.dialect, table, rules)
    target_sql = render(target.dialect, table, rules)

    parents: list[int] = []
    depth, last = 1, 1
    while True:
        left = source.summarize(table, source_sql, depth, parents)
        right = target.summarize(table, target_sql, depth, parents)
        result.queries += 2
        result.summary_rows += len(left) + len(right)
        if depth == 1:
            result.source_rows = sum(s.rows for s in left.values())
            result.target_rows = sum(s.rows for s in right.values())
            last = leaf_depth(max(result.source_rows, result.target_rows), leaf_width)
        differing = _differing(left, right)
        result.levels.append((depth, len(left.keys() | right.keys()), len(differing)))
        if not differing:
            return result
        parents = differing
        if depth >= last:
            break
        depth += 1

    left_rows = source.fetch(table, source_sql, depth, parents)
    right_rows = target.fetch(table, target_sql, depth, parents)
    result.queries += 2
    result.fetched_rows = len(left_rows) + len(right_rows)
    source_by_key, target_by_key = _by_key(left_rows), _by_key(right_rows)
    names = [c.name for c in table.value_columns]
    changed: set[str] = set()
    if result.after_watermark and target_by_key.keys() - source_by_key.keys():
        changed = set(source.keys_after(table, source_sql, depth, parents))
        result.queries += 1
        result.fetched_rows += len(changed)
    for key in sorted(source_by_key.keys() | target_by_key.keys()):
        a, b = source_by_key.get(key, []), target_by_key.get(key, [])
        if len(a) > 1 or len(b) > 1:
            result.duplicates.append(DuplicateKey(key, len(a), len(b)))
        elif not b:
            result.missing.append(key)
        elif not a:
            (result.in_flight if key in changed else result.extra).append(key)
        elif a[0] != b[0]:
            result.cells += [
                CellDiff(key, name, x, y)
                for name, x, y in zip(names, a[0], b[0], strict=True)
                if x != y
            ]
    return result


def _by_key(rows: list[tuple[str, tuple[str, ...]]]) -> dict[str, list[tuple[str, ...]]]:
    """Each key's rows, every copy kept."""
    grouped: dict[str, list[tuple[str, ...]]] = {}
    for key, values in rows:
        grouped.setdefault(key, []).append(values)
    return grouped
