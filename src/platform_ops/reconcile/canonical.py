"""Canonical row rendering and the cross-engine row hash (ADR 0009).

Two engines never agree on how to print a value: ``2.3`` against ``2.30``,
``2025-07-01 10:00:00`` against ``2025-07-01 10:00:00.000000``, ``t`` against
``true``. So before anything is hashed, every value is rendered to one agreed
string, the same way in Postgres, in ClickHouse and in Python:

- int: the integer, also when one side stores it as a ``Decimal``;
- decimal: fixed scale, rounded half away from zero, no negative zero;
- timestamp: UTC, ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (the company's timestamps
  are UTC instants stored without a zone);
- date: ``YYYY-MM-DD``;
- text: optionally right-trimmed, optionally lower-cased, then ``\\`` becomes
  ``\\\\`` and ``|`` becomes ``\\|``. A ClickHouse ``FixedString`` loses its
  ``\\0`` padding first;
- boolean: ``true`` or ``false``;
- NULL: ``\\N``, which no escaped value can equal, so NULL never matches ``''``.

A row is its canonical values joined by ``|``; a key is its key columns'
canonical values joined the same way. A row's hash is the MD5 of its UTF-8
bytes, reduced to two unsigned 32-bit integers from hex digits 1 to 8 and 9 to
16, and a segment is summarised as its row count and the sums of both. Sums,
because a sum is additive: a parent segment's sums always equal its children's.

Segments are buckets of the key's MD5: the first ``d`` hex digits at depth
``d``, so a bucket's parent is the bucket divided by 16, whatever the key's
type. Integer keys, dates and composite keys all segment the same way.

There is one Python reference and one SQL renderer per engine; the golden-row
test holds them all to byte-identical output.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from platform_ops.common.config import ReconcileSettings
from platform_ops.reconcile.schema import Column, TableSpec

Dialect = Literal["postgres", "clickhouse"]

NULL = "\\N"
SEPARATOR = "|"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class Rule:
    """How one column is rendered. Only text uses rtrim and casefold."""

    rtrim: bool = False
    casefold: bool = False
    scale: int = 0


def rules_for(table: TableSpec, settings: ReconcileSettings) -> dict[str, Rule]:
    """The rule for every column: the legacy engine's policy, then per-column overrides.

    Both sides of a comparison use the same rules: what counts as equal is the
    data owner's policy for the legacy system, not a property of either engine.
    """
    base = settings.canonical.engines.get("postgres")
    rules: dict[str, Rule] = {}
    for column in table.columns:
        override = settings.canonical.columns.get(f"{table.name}.{column.name}")
        layers = [rule for rule in (override, base) if rule is not None]
        rtrim = next((r.rtrim for r in layers if r.rtrim is not None), False)
        casefold = next((r.casefold for r in layers if r.casefold is not None), False)
        scale = next((r.scale for r in layers if r.scale is not None), column.scale)
        rules[column.name] = Rule(rtrim=rtrim, casefold=casefold, scale=scale)
    return rules


# -- Python reference ----------------------------------------------------------------


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(SEPARATOR, "\\|")


def canonical_value(column: Column, value: Any, rule: Rule) -> str:
    """The canonical text of one value, exactly as the SQL renderers produce it."""
    if value is None:
        return NULL
    kind = column.kind
    if kind == "int":
        return str(int(value))
    if kind == "decimal":
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        quantum = Decimal(1).scaleb(-rule.scale)
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)
        return f"{rounded:f}"
    if kind == "bool":
        return "true" if value else "false"
    if kind == "ts":
        assert isinstance(value, datetime)
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
    if kind == "date":
        assert isinstance(value, date)
        return value.isoformat()
    text = str(value).rstrip("\0")
    if rule.rtrim:
        text = text.rstrip(" ")
    if rule.casefold:
        text = text.lower()
    return escape(text)


def row_string(values: list[str]) -> str:
    return SEPARATOR.join(values)


def row_hash(row: str) -> tuple[int, int]:
    digest = hashlib.md5(row.encode("utf-8")).hexdigest()
    return int(digest[:8], 16), int(digest[8:16], 16)


def key_bucket(key: str, depth: int) -> int:
    """The segment a canonical key falls in at ``depth`` (its first ``depth`` hex digits)."""
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:depth], 16)


# -- SQL renderers ---------------------------------------------------------------------


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _value_postgres(column: Column, rule: Rule) -> str:
    c = f'"{column.name}"'
    kind = column.kind
    if kind == "int":
        return f"({c})::bigint::text"
    if kind == "decimal":
        # numeric rounds half away from zero; adding 0 turns -0.00 into 0.00.
        return f"(round(({c})::numeric, {rule.scale}) + 0)::numeric(38, {rule.scale})::text"
    if kind == "bool":
        return f"CASE {c} WHEN TRUE THEN 'true' WHEN FALSE THEN 'false' END"
    if kind == "ts":
        return f'to_char({c}, \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
    if kind == "date":
        return f"to_char({c}, 'YYYY-MM-DD')"
    expr = f"({c})::text"
    if rule.rtrim:
        expr = f"rtrim({expr}, ' ')"
    if rule.casefold:
        expr = f"lower({expr})"
    return f"replace(replace({expr}, '\\', '\\\\'), '|', '\\|')"


def _value_clickhouse(column: Column, rule: Rule) -> str:
    c = f"`{column.target}`"
    kind = column.kind
    if kind == "int":
        return f"toString(toInt64({c}))"
    if kind == "decimal":
        # round() on a Decimal rounds half away from zero; toDecimalString keeps
        # every fractional digit, where toString would drop trailing zeros.
        rounded = f"round(toDecimal128({c}, {max(rule.scale, 10)}), {rule.scale})"
        return (
            f"if({rounded} = 0, toDecimalString(toDecimal128(0, {rule.scale}), {rule.scale}), "
            f"toDecimalString({rounded}, {rule.scale}))"
        )
    if kind == "bool":
        # if() reads a NULL condition as false, which would make NULL equal false.
        return f"if(isNull({c}), NULL, if({c}, 'true', 'false'))"
    if kind == "ts":
        return f"formatDateTime(toDateTime64({c}, 6, 'UTC'), '%Y-%m-%dT%H:%i:%S.%fZ', 'UTC')"
    if kind == "date":
        return f"toString(toDate32({c}))"
    expr = f"toString({c})"
    if "FixedString" in column.target_type:
        expr = f"replaceRegexpOne({expr}, '\\\\x00+$', '')"
    if rule.rtrim:
        expr = f"trimRight({expr})"
    if rule.casefold:
        expr = f"lowerUTF8({expr})"
    return f"replaceAll(replaceAll({expr}, '\\\\', '\\\\\\\\'), '|', '\\\\|')"


def _coalesce(dialect: Dialect, expr: str) -> str:
    null = _quote(NULL) if dialect == "postgres" else "'\\\\N'"
    if dialect == "clickhouse":
        return f"ifNull({expr}, {null})"
    return f"COALESCE({expr}, {null})"


def _join(dialect: Dialect, values: list[str]) -> str:
    if dialect == "postgres":
        return " || '|' || ".join(values)
    return "concat(" + ", '|', ".join(values) + ")" if len(values) > 1 else values[0]


def _uint32(dialect: Dialect, text: str, offset: int) -> str:
    """32 bits of the MD5 of ``text``, from byte ``offset``, as an unsigned integer."""
    if dialect == "postgres":
        return f"('x' || substr(md5({text}), {offset * 2 + 1}, 8))::bit(32)::bigint"
    return f"reinterpretAsUInt32(reverse(substring(MD5({text}), {offset + 1}, 4)))"


@dataclass(frozen=True)
class RenderedTable:
    """SQL expressions for one table on one side of a comparison."""

    key: str  # the canonical key string
    values: tuple[str, ...]  # canonical value of each value column
    row: str  # the canonical row string
    h1: str
    h2: str
    bucket: str  # the first 32 bits of the key's MD5


def render(dialect: Dialect, table: TableSpec, rules: dict[str, Rule]) -> RenderedTable:
    value_sql = _value_postgres if dialect == "postgres" else _value_clickhouse
    canonical = {c.name: _coalesce(dialect, value_sql(c, rules[c.name])) for c in table.columns}
    key = _join(dialect, [canonical[name] for name in table.key])
    values = tuple(canonical[c.name] for c in table.value_columns)
    row = _join(dialect, [canonical[c.name] for c in table.columns])
    return RenderedTable(
        key=key,
        values=values,
        row=row,
        h1=_uint32(dialect, row, 0),
        h2=_uint32(dialect, row, 4),
        bucket=_uint32(dialect, key, 0),
    )
