"""Canonical row rendering and the cross-engine row hash (ADR 0009).

Two engines never agree on how to print a value: ``2.3`` against ``2.30``,
``2025-07-01 10:00:00-04`` against ``2025-07-01 14:00:00+00``, ``t`` against
``true``. So before anything is hashed, every value is rendered to one agreed
string, the same way in every engine:

- decimal: fixed scale, rounded half away from zero (what all three engines do
  when casting to a smaller scale), no negative zero;
- timestamp: UTC, ``YYYY-MM-DDTHH:MM:SS.ffffffZ``. A legacy local timestamp is
  first converted from the configured legacy zone;
- text: optionally right-trimmed, optionally lower-cased, then ``\\`` becomes
  ``\\\\`` and ``|`` becomes ``\\|``;
- boolean: ``true`` or ``false``;
- NULL: ``\\N``, which no escaped value can equal, so NULL never matches ``''``.

A row is its canonical values joined by ``|``. Its hash is the MD5 of the row's
UTF-8 bytes, reduced to two unsigned 32-bit integers taken from hex digits 1 to
8 and 9 to 16. A segment is summarised as its row count and the sums of both
integers. Sums, not XOR, because SQL Server has no XOR aggregate, and a sum is
additive: a parent segment's sums always equal the sum of its children's. Each
row adds under 2**32, so even SQL Server's BIGINT sum cannot overflow below two
billion rows per segment.

This module has one Python reference implementation and one SQL renderer per
engine. The golden-row test holds all of them to byte-identical output.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from platform_ops.common.config import Engine, ReconcileSettings
from platform_ops.reconcile.schema import Column, Dialect, TableSpec

NULL = "\\N"
SEPARATOR = "|"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class Rule:
    """How one column is rendered. Only text uses rtrim and casefold."""

    rtrim: bool = False
    casefold: bool = False
    scale: int = 0


def rules_for(table: TableSpec, engine: Engine, settings: ReconcileSettings) -> dict[str, Rule]:
    """The rule for every column when ``engine`` is the source of a comparison.

    Both sides of one comparison use the same rules: the source engine's
    policy decides what counts as equal, not the target's.
    """
    base = settings.canonical.engines.get(engine)
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


def canonical_value(
    column: Column, value: object, rule: Rule, local_zone: str | None = None
) -> str:
    """The canonical text of one value, exactly as the SQL renderers produce it.

    ``local_zone`` is the zone a ``local_ts`` value was recorded in on this
    side. ``None`` means the side stores it as a UTC instant already.
    """
    if value is None:
        return NULL
    kind = column.kind
    if kind == "int":
        return str(int(value))  # type: ignore[call-overload]
    if kind == "decimal":
        number = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        quantum = Decimal(1).scaleb(-rule.scale)
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)
        return f"{rounded:f}"
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("local_ts", "utc_ts"):
        assert isinstance(value, datetime)
        if value.tzinfo is None:
            zone = ZoneInfo(local_zone) if kind == "local_ts" and local_zone else UTC
            value = value.replace(tzinfo=zone)
        return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
    text = str(value)
    if rule.rtrim:
        text = text.rstrip(" ")
    if rule.casefold:
        text = text.lower()
    return escape(text)


def row_string(values: list[str]) -> str:
    return SEPARATOR.join(values)


def row_hash(row: str) -> tuple[int, int]:
    digest = hashlib.md5(row.encode("utf-8")).hexdigest()  # noqa: S324 - a checksum, not security
    return int(digest[:8], 16), int(digest[8:16], 16)


# -- SQL renderers ---------------------------------------------------------------------


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _text_sql(dialect: Dialect, expr: str, rule: Rule) -> str:
    if rule.rtrim:
        expr = f"RTRIM({expr})"
    if rule.casefold:
        expr = f"LOWER({expr})"
    return f"REPLACE(REPLACE({expr}, '\\', '\\\\'), '|', '\\|')"


def _value_sql(
    dialect: Dialect, column: Column, rule: Rule, local_zone: str | None, zone_names: dict[str, str]
) -> str:
    c = column.name
    kind = column.kind
    if dialect == "postgres":
        if kind == "int":
            return f"{c}::text"
        if kind == "decimal":
            return f"{c}::numeric(38, {rule.scale})::text"
        if kind == "text":
            return _text_sql(dialect, c, rule)
        if kind == "bool":
            return f"CASE {c} WHEN TRUE THEN 'true' WHEN FALSE THEN 'false' END"
        fmt = '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\''
        if kind == "local_ts" and local_zone:
            return f"to_char(({c} AT TIME ZONE {_quote(local_zone)}) AT TIME ZONE 'UTC', {fmt})"
        if kind == "local_ts":
            return f"to_char({c}, {fmt})"
        return f"to_char({c} AT TIME ZONE 'UTC', {fmt})"
    if dialect == "duckdb":
        if kind == "int":
            return f"CAST({c} AS VARCHAR)"
        if kind == "decimal":
            return f"CAST(CAST({c} AS DECIMAL(38, {rule.scale})) AS VARCHAR)"
        if kind == "text":
            return _text_sql(dialect, c, rule)
        if kind == "bool":
            return f"CASE {c} WHEN TRUE THEN 'true' WHEN FALSE THEN 'false' END"
        fmt = _quote(TIMESTAMP_FORMAT)
        # The session runs in UTC (connectors set it), so a TIMESTAMPTZ prints as UTC.
        if kind == "local_ts" and local_zone:
            return f"strftime({c} AT TIME ZONE {_quote(local_zone)}, {fmt})"
        return f"strftime({c}, {fmt})"
    # SQL Server
    if kind == "int":
        return f"CONVERT(VARCHAR(20), {c})"
    if kind == "decimal":
        return f"CONVERT(VARCHAR(50), CONVERT(DECIMAL(38, {rule.scale}), {c}))"
    if kind == "text":
        return _text_sql(dialect, c, rule)
    if kind == "bool":
        return f"CASE {c} WHEN 1 THEN 'true' WHEN 0 THEN 'false' END"
    if kind == "local_ts" and local_zone:
        zone = _quote(zone_names.get(local_zone, local_zone))
        instant = f"SWITCHOFFSET({c} AT TIME ZONE {zone}, '+00:00')"
    elif kind == "local_ts":
        instant = c
    else:
        instant = f"SWITCHOFFSET({c}, '+00:00')"
    # Style 126 drops the fraction when it is zero, so microseconds are added by hand.
    moment = f"CONVERT(DATETIME2(6), {instant})"
    micro = f"RIGHT('000000' + CONVERT(VARCHAR(6), DATEPART(MICROSECOND, {moment})), 6)"
    return f"CONVERT(VARCHAR(19), {moment}, 126) + '.' + {micro} + 'Z'"


def _hash_part_sql(dialect: Dialect, row: str, part: int) -> str:
    start = 1 if part == 1 else 9
    if dialect == "postgres":
        return f"('x' || substr(md5({row}), {start}, 8))::bit(32)::bigint"
    if dialect == "duckdb":
        return f"CAST('0x' || substr(md5({row}), {start}, 8) AS BIGINT)"
    byte = 1 if part == 1 else 5
    return f"CONVERT(BIGINT, SUBSTRING(HASHBYTES('MD5', {row}), {byte}, 4))"


@dataclass(frozen=True)
class RenderedTable:
    """SQL expressions for one table on one side of a comparison."""

    key: str
    values: tuple[str, ...]
    row: str
    h1: str
    h2: str


def render(
    dialect: Dialect,
    table: TableSpec,
    rules: dict[str, Rule],
    local_zone: str | None,
    zone_names: dict[str, str] | None = None,
) -> RenderedTable:
    names = zone_names or {}
    values = tuple(
        f"COALESCE({_value_sql(dialect, c, rules[c.name], local_zone, names)}, {_quote(NULL)})"
        for c in table.columns
    )
    if dialect == "sqlserver":
        row = "CONVERT(VARCHAR(MAX), CONCAT(" + ", '|', ".join(values) + "))"
    else:
        row = " || '|' || ".join(values)
    return RenderedTable(
        key=table.key,
        values=values,
        row=row,
        h1=_hash_part_sql(dialect, row, 1),
        h2=_hash_part_sql(dialect, row, 2),
    )
