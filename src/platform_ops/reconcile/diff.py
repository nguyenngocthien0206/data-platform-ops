"""The segmented diff: find differing rows without moving the table.

Both sides are split by key range into segments of one width. Each side reports
a row count and two hash sums per segment, computed inside its own engine. Only
segments whose summaries differ are split again, ``fanout`` ways, and so on
down to segments of ``leaf_width`` keys. Only the rows of differing leaves are
fetched, as canonical strings, and compared here.

Widths nest exactly: the leaf width times a power of ``fanout``. So a child's
bucket number divided by ``fanout`` is its parent's, and every level is one
``GROUP BY`` query per side, whatever the number of segments.

Everything that crosses from an engine to Python is counted: summary rows at
every level and fetched rows at the leaves. The naive alternative, fetching
both tables whole, is the benchmark the report compares against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from platform_ops.reconcile.canonical import Rule, render
from platform_ops.reconcile.connectors import SourceConnector, Summary
from platform_ops.reconcile.schema import TableSpec


@dataclass(frozen=True)
class CellDiff:
    key: int
    column: str
    source: str
    target: str


@dataclass
class TableDiff:
    table: str
    source_rows: int = 0
    target_rows: int = 0
    missing: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)
    cells: list[CellDiff] = field(default_factory=list)
    # (width, segments compared, segments differing) per level, top first.
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


def widths(span: int, fanout: int, leaf_width: int) -> list[int]:
    """Segment widths from the top level down to the leaf width.

    The top width is the smallest ``leaf_width * fanout**k`` for which
    ``fanout`` segments cover the whole key span.
    """
    width = leaf_width
    levels = [width]
    while width * fanout < span:
        width *= fanout
        levels.append(width)
    return list(reversed(levels))


def _differing(source: dict[int, Summary], target: dict[int, Summary]) -> list[int]:
    return sorted(b for b in source.keys() | target.keys() if source.get(b) != target.get(b))


def diff_table(
    table: TableSpec,
    source: SourceConnector,
    target: SourceConnector,
    rules: dict[str, Rule],
    *,
    fanout: int,
    leaf_width: int,
    zone_names: dict[str, str] | None = None,
) -> TableDiff:
    result = TableDiff(table.name)
    source_sql = render(source.dialect, table, rules, source.local_zone, zone_names)
    target_sql = render(target.dialect, table, rules, target.local_zone, zone_names)

    ranges = [r for r in (source.key_range(table), target.key_range(table)) if r is not None]
    result.queries += 2
    if not ranges:
        return result
    lo = min(r[0] for r in ranges)
    hi = max(r[1] for r in ranges)

    parents: list[int] = []
    parent_width: int | None = None
    level_widths = widths(hi - lo + 1, fanout, leaf_width)
    for width in level_widths:
        left = source.summarize(table, source_sql, lo, width, parent_width, parents)
        right = target.summarize(table, target_sql, lo, width, parent_width, parents)
        result.queries += 2
        result.summary_rows += len(left) + len(right)
        if parent_width is None:
            result.source_rows = sum(s.rows for s in left.values())
            result.target_rows = sum(s.rows for s in right.values())
        differing = _differing(left, right)
        result.levels.append((width, len(left.keys() | right.keys()), len(differing)))
        if not differing:
            return result
        parents, parent_width = differing, width

    leaf = level_widths[-1]
    left_rows = source.fetch(table, source_sql, lo, leaf, parents)
    right_rows = target.fetch(table, target_sql, lo, leaf, parents)
    result.queries += 2
    result.fetched_rows = len(left_rows) + len(right_rows)
    names = [c.name for c in table.columns]
    for key in sorted(left_rows.keys() | right_rows.keys()):
        a, b = left_rows.get(key), right_rows.get(key)
        if b is None:
            result.missing.append(key)
        elif a is None:
            result.extra.append(key)
        elif a != b:
            result.cells += [
                CellDiff(key, name, x, y) for name, x, y in zip(names, a, b, strict=True) if x != y
            ]
    return result
