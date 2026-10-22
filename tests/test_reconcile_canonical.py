"""Canonical rendering and the row hash, byte-identical across engines (ADR 0006).

The golden rows hold every value that has bitten a migration somewhere: NULL
next to an empty string, trailing spaces, mixed case, non-ASCII names, the
separator and escape characters inside text, a literal ``\\N``, negative
half-way decimals, a value that rounds to zero from below, microseconds,
dates, and a ClickHouse ``FixedString`` padded with zero bytes. Python,
Postgres and ClickHouse must render each value to the same string, hash each
row the same way, and put each key in the same segment. The engine cases need
``make up`` and skip with the reason otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from platform_ops.common.config import Settings, load_settings
from platform_ops.reconcile.canonical import (
    NULL,
    Rule,
    canonical_value,
    key_bucket,
    render,
    row_hash,
    row_string,
)
from platform_ops.reconcile.diff import diff_table
from platform_ops.reconcile.schema import Column, TableSpec

COLUMNS = (
    Column("id", "int"),
    Column("t", "text"),
    Column("code", "text", target_type="Nullable(FixedString(8))"),
    Column("d2", "decimal", scale=2),
    Column("d6", "decimal", scale=6),
    Column("b", "bool"),
    Column("ts", "ts"),
    Column("day", "date"),
)

ROWS: list[dict[str, Any]] = [
    {
        "id": 1,
        "t": "plain",
        "code": "AB",
        "d2": Decimal("1.00"),
        "d6": Decimal("0.000001"),
        "b": True,
        "ts": datetime(2025, 1, 15, 10, 0, 0, 500000),
        "day": date(2025, 1, 15),
    },
    {
        "id": 2,
        "t": "",
        "code": "",
        "d2": Decimal("-2.35"),
        "d6": Decimal("-123456789012.345678"),
        "b": False,
        "ts": datetime(2025, 7, 1, 14, 0, 0, 123456),
        "day": date(1999, 12, 31),
    },
    {"id": 3, "t": None, "code": None, "d2": None, "d6": None, "b": None, "ts": None, "day": None},
    {
        "id": 4,
        "t": "trailing   ",
        "code": "X  ",
        "d2": Decimal("2.35"),
        "d6": Decimal("12345678901.234567"),
        "b": True,
        "ts": datetime(2025, 11, 2, 6),
        "day": date(2024, 2, 29),
    },
    {
        "id": 5,
        "t": "MiXeD Case",
        "code": "Zz",
        "d2": Decimal("-0.04"),
        "d6": Decimal("0"),
        "b": False,
        "ts": datetime(2024, 2, 29, 23, 59, 59, 999999),
        "day": date(2026, 4, 5),
    },
    {
        "id": 6,
        "t": "pipe|and\\backslash",
        "code": "a|b",
        "d2": Decimal("0.05"),
        "d6": Decimal("-0.000999"),
        "b": True,
        "ts": None,
        "day": date(2025, 6, 30),
    },
    {
        "id": 7,
        "t": "Nguyễn Zoë José Łukasz",
        "code": "ok",
        "d2": Decimal("999.99"),
        "d6": Decimal("1.5"),
        "b": None,
        "ts": datetime(2025, 6, 15, 16, 30),
        "day": None,
    },
    {
        "id": 8,
        "t": "\\N",
        "code": "N",
        "d2": Decimal("0.00"),
        "d6": Decimal("0.000000"),
        "b": False,
        "ts": datetime(2025, 1, 1, 5),
        "day": date(2025, 1, 1),
    },
    {
        "id": 9,
        "t": "it's",
        "code": "q'",
        "d2": Decimal("-999.95"),
        "d6": Decimal("-1"),
        "b": True,
        "ts": datetime(2025, 4, 1, 13, 15, 0, 1),
        "day": date(2025, 4, 1),
    },
]

# Two policies: as stored, and a lenient one (trailing spaces and case do not
# count). d2 is re-rendered at scale 1 to exercise rounding half away from
# zero and the no-negative-zero rule.
POLICIES = {
    "strict": {c.name: Rule(scale=c.scale) for c in COLUMNS},
    "lenient": {
        c.name: Rule(rtrim=True, casefold=True, scale=1 if c.name == "d2" else c.scale)
        for c in COLUMNS
    },
}

PG_TYPES = {
    "id": "bigint PRIMARY KEY",
    "t": "varchar",
    "code": "varchar",
    "d2": "numeric(12, 2)",
    "d6": "numeric(20, 6)",
    "b": "boolean",
    "ts": "timestamp",
    "day": "date",
}
CH_TYPES = {
    "id": "Int64",
    "t": "Nullable(String)",
    "code": "Nullable(FixedString(8))",
    "d2": "Nullable(Decimal(12, 2))",
    "d6": "Nullable(Decimal(20, 6))",
    "b": "Nullable(Bool)",
    "ts": "Nullable(DateTime64(6, 'UTC'))",
    "day": "Nullable(Date32)",
}
SCHEMA = "t_golden"


def _spec() -> TableSpec:
    return TableSpec(
        "golden",
        "table",
        ("id",),
        COLUMNS,
        source=f'"{SCHEMA}"."golden"',
        target=f"`{SCHEMA}`.`golden`",
    )


def _expected(policy: str) -> dict[str, tuple[str, ...]]:
    rules = POLICIES[policy]
    return {
        str(row["id"]): tuple(canonical_value(c, row[c.name], rules[c.name]) for c in COLUMNS)
        for row in ROWS
    }


# -- the reference itself ------------------------------------------------------------------


def test_reference_renders_the_agreed_strings() -> None:
    strict, lenient = _expected("strict"), _expected("lenient")
    assert strict["3"] == ("3",) + (NULL,) * 7
    assert strict["2"][1] == "" and strict["3"][1] == NULL
    assert strict["8"][1] == "\\\\N", "a literal \\N is escaped, so it never equals NULL"
    assert strict["6"][1] == "pipe\\|and\\\\backslash"
    assert strict["1"][6] == "2025-01-15T10:00:00.500000Z"
    assert strict["2"][4] == "-123456789012.345678"
    assert strict["5"][3] == "-0.04"
    assert lenient["5"][3] == "0.0", "rounds to zero from below: no negative zero"
    assert lenient["2"][3] == "-2.4" and lenient["4"][3] == "2.4", "half away from zero"
    assert lenient["4"][1] == "trailing" and lenient["5"][1] == "mixed case"
    assert strict["1"][5] == "true" and strict["7"][5] == NULL
    assert strict["5"][7] == "2026-04-05"


def test_row_hash_is_md5_split_into_two_unsigned_32_bit_halves() -> None:
    assert row_hash("abc") == (0x90015098, 0x3CD24FB0)
    assert key_bucket("abc", 1) == 0x9 and key_bucket("abc", 3) == 0x900


# -- the engines -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_settings(
    tmp_path_factory: pytest.TempPathFactory,
    isolated_config: Callable[..., Path],
    servers_or_skip: Callable[[Settings], None],
) -> Settings:
    settings = load_settings(isolated_config(tmp_path_factory.mktemp("golden"), 0.01))
    servers_or_skip(settings)
    return settings


@pytest.fixture(scope="module")
def engines(golden_settings: Settings) -> Iterator[tuple[Any, Any]]:
    from platform_ops.common.servers import clickhouse_client, connect_postgres

    settings = golden_settings
    pg = connect_postgres(settings)
    pg.autocommit = True
    ch = clickhouse_client(settings)
    pg.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    pg.execute(f"CREATE SCHEMA {SCHEMA}")
    columns = ", ".join(f"{name} {t}" for name, t in PG_TYPES.items())
    pg.execute(f"CREATE TABLE {SCHEMA}.golden ({columns})")
    with pg.cursor() as cursor:
        cursor.executemany(
            f"INSERT INTO {SCHEMA}.golden VALUES ({', '.join(['%s'] * len(COLUMNS))})",
            [[row[c.name] for c in COLUMNS] for row in ROWS],
        )
    ch.command(f"DROP DATABASE IF EXISTS {SCHEMA}")
    ch.command(f"CREATE DATABASE {SCHEMA}")
    ch_columns = ", ".join(f"`{name}` {t}" for name, t in CH_TYPES.items())
    ch.command(f"CREATE TABLE {SCHEMA}.golden ({ch_columns}) ENGINE = MergeTree ORDER BY id")
    ch_rows = [
        [
            row[c.name].replace(tzinfo=UTC) if c.name == "ts" and row[c.name] else row[c.name]
            for c in COLUMNS
        ]
        for row in ROWS
    ]
    ch.insert(f"{SCHEMA}.golden", ch_rows, column_names=[c.name for c in COLUMNS])
    yield pg, ch
    pg.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
    ch.command(f"DROP DATABASE {SCHEMA}")
    pg.close()
    ch.close()


def _query(dialect: str, engine: Any, sql_text: str) -> list[tuple[Any, ...]]:
    if dialect == "postgres":
        return list(engine.execute(sql_text).fetchall())
    return list(engine.query(sql_text).result_rows)


@pytest.mark.integration
@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_each_engine_matches_the_reference(
    engines: tuple[Any, Any], dialect: str, policy: str
) -> None:
    engine = engines[0] if dialect == "postgres" else engines[1]
    spec = _spec()
    rendered = render(dialect, spec, POLICIES[policy])  # type: ignore[arg-type]
    relation = spec.source if dialect == "postgres" else spec.target
    rows = _query(
        dialect,
        engine,
        f"SELECT {rendered.key}, {', '.join(rendered.values)}, {rendered.h1}, {rendered.h2}, "
        f"{rendered.bucket} FROM {relation} ORDER BY id",
    )
    expected = _expected(policy)
    for row in rows:
        key, values, h1, h2, bucket = str(row[0]), tuple(str(v) for v in row[1:-3]), *row[-3:]
        assert (key, *values) == expected[key], f"{dialect} {policy} row {key}"
        assert (int(h1), int(h2)) == row_hash(row_string(list(expected[key])))
        assert int(bucket) == key_bucket(key, 8)


@pytest.mark.integration
def test_a_row_inserted_twice_in_clickhouse_is_a_duplicate_key(
    engines: tuple[Any, Any],
) -> None:
    """A join that doubles a row: the real connectors keep both copies, the diff names the key."""
    from platform_ops.reconcile.connectors import ClickHouseConnector, PostgresConnector

    pg, ch = engines
    ch.command(f"CREATE TABLE {SCHEMA}.golden_dup AS {SCHEMA}.golden")
    try:
        ch.command(f"INSERT INTO {SCHEMA}.golden_dup SELECT * FROM {SCHEMA}.golden")
        ch.command(f"INSERT INTO {SCHEMA}.golden_dup SELECT * FROM {SCHEMA}.golden WHERE id = 7")
        source, target = PostgresConnector(pg), ClickHouseConnector(ch)
        rules = POLICIES["strict"]
        assert diff_table(_spec(), source, target, rules, leaf_width=4).clean
        doubled = replace(_spec(), target=f"`{SCHEMA}`.`golden_dup`")
        result = diff_table(doubled, source, target, rules, leaf_width=4)
        source.forget()
    finally:
        ch.command(f"DROP TABLE {SCHEMA}.golden_dup")
    assert (result.source_rows, result.target_rows) == (len(ROWS), len(ROWS) + 1)
    assert [(d.key, d.source, d.target) for d in result.duplicates] == [("7", 1, 2)]
    assert not result.missing and not result.extra and not result.cells


@pytest.mark.integration
def test_rows_written_after_the_watermark_wait_for_the_next_night(
    engines: tuple[Any, Any],
) -> None:
    """As of a watermark on ``ts``: rows 2 and 4 changed after it, so the copy's older
    versions are in flight; a row written after it is invisible. Both are counted."""
    from platform_ops.reconcile.connectors import ClickHouseConnector, PostgresConnector

    pg, ch = engines
    watermark = datetime(2025, 7, 1)
    pg.execute(f"CREATE TABLE {SCHEMA}.golden_late AS SELECT * FROM {SCHEMA}.golden")
    try:
        pg.execute(
            f"INSERT INTO {SCHEMA}.golden_late (id, t, ts) "
            "VALUES (10, 'written later', TIMESTAMP '2025-07-02 09:00:00')"
        )
        spec = replace(
            _spec(), source=f'"{SCHEMA}"."golden_late"', changed_at="ts", watermark=watermark
        )
        source = PostgresConnector(pg)
        result = diff_table(spec, source, ClickHouseConnector(ch), POLICIES["strict"], leaf_width=4)
        source.forget()
    finally:
        pg.execute(f"DROP TABLE {SCHEMA}.golden_late")
    assert result.clean
    assert sorted(result.in_flight) == ["2", "4"]
    assert result.after_watermark == 3
    assert (result.source_rows, result.target_rows) == (len(ROWS) - 2, len(ROWS))


@pytest.mark.integration
def test_frozen_reports_keep_the_rows_they_were_frozen_with(
    engines: tuple[Any, Any], golden_settings: Settings
) -> None:
    """Two reports frozen by two workers, each in its own connection, in one snapshot."""
    from platform_ops.common.servers import connect_postgres
    from platform_ops.reconcile.asof import freeze_reports

    pg, _ = engines
    frozen = f"{SCHEMA}_asof"
    names = ["counted", "summed"]

    def freeze() -> None:
        connect = lambda: connect_postgres(golden_settings)  # noqa: E731
        freeze_reports(connect, SCHEMA, frozen, names, workers=2)

    def read(schema: str) -> list[Any]:
        return [pg.execute(f"SELECT * FROM {schema}.{n}").fetchone() for n in names]

    pg.execute(f"CREATE TABLE {SCHEMA}.written (id bigint)")
    pg.execute(f"CREATE VIEW {SCHEMA}.counted AS SELECT count(*) AS n FROM {SCHEMA}.written")
    pg.execute(f"CREATE VIEW {SCHEMA}.summed AS SELECT sum(id) AS s FROM {SCHEMA}.written")
    try:
        pg.execute(f"INSERT INTO {SCHEMA}.written VALUES (1), (2)")
        freeze()
        pg.execute(f"INSERT INTO {SCHEMA}.written VALUES (3)")
        assert read(frozen) == [(2,), (3,)]
        assert read(SCHEMA) == [(3,), (6,)]
        freeze()
        assert read(frozen) == [(3,), (6,)]
    finally:
        pg.execute(f"DROP SCHEMA IF EXISTS {frozen} CASCADE")
        pg.execute(f"DROP VIEW {SCHEMA}.counted, {SCHEMA}.summed")
        pg.execute(f"DROP TABLE {SCHEMA}.written")
