"""Canonical rendering and the row hash, byte-identical across engines (SPEC Phase 4).

The golden rows hold every value that has bitten a migration somewhere: NULL
next to an empty string, trailing spaces, mixed case, non-ASCII names, the
separator and escape characters inside text, a literal ``\\N``, negative
half-way decimals, a value that rounds to zero from below, and local times on
both sides of daylight saving. Python, DuckDB, Postgres and SQL Server must
render each value to the same string and sum the same hashes. Postgres and
SQL Server run when they are reachable (``make up``, and the ``sqlserver``
Compose profile); otherwise their cases are skipped with the reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from platform_ops.reconcile.canonical import (
    NULL,
    Rule,
    canonical_value,
    render,
    row_hash,
    row_string,
)
from platform_ops.reconcile.connectors import (
    DuckDBConnector,
    PostgresConnector,
    SqlServerConnector,
    postgres_params,
    sqlserver_params,
)
from platform_ops.reconcile.schema import Column, TableSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
ZONE = "America/New_York"
ZONE_NAMES = {ZONE: "Eastern Standard Time"}

GOLDEN = TableSpec(
    "golden",
    "id",
    (
        Column("id", "int"),
        Column("t", "text", length=60),
        Column("d2", "decimal", precision=12, scale=2),
        Column("d6", "decimal", precision=20, scale=6),
        Column("b", "bool"),
        Column("lt", "local_ts"),
        Column("ut", "utc_ts"),
    ),
)

ROWS: list[dict[str, Any]] = [
    {"id": 1, "t": "plain", "d2": Decimal("1.00"), "d6": Decimal("0.000001"), "b": True,
     "lt": datetime(2025, 1, 15, 10, 0, 0, 500000), "ut": datetime(2025, 1, 15, 15, tzinfo=UTC)},
    {"id": 2, "t": "", "d2": Decimal("-2.35"), "d6": Decimal("-123456789012.345678"), "b": False,
     "lt": datetime(2025, 7, 1, 10, 0), "ut": datetime(2025, 7, 1, 14, 0, 0, 123456, tzinfo=UTC)},
    {"id": 3, "t": None, "d2": None, "d6": None, "b": None, "lt": None, "ut": None},
    {"id": 4, "t": "trailing   ", "d2": Decimal("2.35"), "d6": Decimal("12345678901.234567"),
     "b": True, "lt": datetime(2025, 3, 9, 6, 0), "ut": datetime(2025, 11, 2, 6, tzinfo=UTC)},
    {"id": 5, "t": "MiXeD Case", "d2": Decimal("-0.04"), "d6": Decimal("0"), "b": False,
     "lt": datetime(2025, 11, 2, 6, 0),
     "ut": datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=UTC)},
    {"id": 6, "t": "pipe|and\\backslash", "d2": Decimal("0.05"), "d6": Decimal("-0.000999"),
     "b": True, "lt": datetime(2025, 12, 31, 23, 59, 59, 999999), "ut": None},
    {"id": 7, "t": "Nguyễn Zoë José Łukasz", "d2": Decimal("999.99"), "d6": Decimal("1.5"),
     "b": None, "lt": datetime(2025, 6, 15, 12, 30),
     "ut": datetime(2025, 6, 15, 16, 30, tzinfo=UTC)},
    {"id": 8, "t": "\\N", "d2": Decimal("0.00"), "d6": Decimal("0.000000"), "b": False,
     "lt": datetime(2025, 1, 1, 0, 0), "ut": datetime(2025, 1, 1, 5, tzinfo=UTC)},
    {"id": 9, "t": "it's", "d2": Decimal("-999.95"), "d6": Decimal("-1"), "b": True,
     "lt": datetime(2025, 4, 1, 9, 15, 0, 1), "ut": datetime(2025, 4, 1, 13, 15, 0, 1, tzinfo=UTC)},
]  # fmt: skip

# Two policies: as stored, and the lenient one a case-insensitive legacy system
# implies. d2 is re-rendered at scale 1 to exercise rounding half away from
# zero and the no-negative-zero rule.
POLICIES = {
    "strict": {c.name: Rule(scale=c.scale) for c in GOLDEN.columns},
    "lenient": {
        c.name: Rule(rtrim=True, casefold=True, scale=1 if c.name == "d2" else c.scale)
        for c in GOLDEN.columns
    },
}


def _arrow() -> pa.Table:
    schema = pa.schema([
        ("id", pa.int64()), ("t", pa.string()), ("d2", pa.decimal128(12, 2)),
        ("d6", pa.decimal128(20, 6)), ("b", pa.bool_()), ("lt", pa.timestamp("us")),
        ("ut", pa.timestamp("us", tz="UTC")),
    ])  # fmt: skip
    return pa.Table.from_pylist(ROWS, schema=schema)


def _expected(policy: str) -> dict[int, tuple[str, ...]]:
    rules = POLICIES[policy]
    return {
        row["id"]: tuple(
            canonical_value(c, row[c.name], rules[c.name], ZONE) for c in GOLDEN.columns
        )
        for row in ROWS
    }


def _hash_sums(values: dict[int, tuple[str, ...]]) -> tuple[int, int]:
    hashes = [row_hash(row_string(list(v))) for v in values.values()]
    return sum(h[0] for h in hashes), sum(h[1] for h in hashes)


# -- the reference itself ------------------------------------------------------------


def test_reference_renders_the_agreed_strings() -> None:
    strict = _expected("strict")
    lenient = _expected("lenient")
    assert strict[3] == ("3",) + (NULL,) * 6
    assert strict[2][1] == "" and strict[3][1] == NULL
    assert strict[8][1] == "\\\\N", "a literal \\N is escaped, so it never equals NULL"
    assert strict[6][1] == "pipe\\|and\\\\backslash"
    assert strict[1][5] == "2025-01-15T15:00:00.500000Z"
    assert strict[2][5] == "2025-07-01T14:00:00.000000Z", "summer is UTC-4"
    assert strict[2][3] == "-123456789012.345678"
    assert strict[5][2] == "-0.04"
    assert lenient[5][2] == "0.0", "rounds to zero from below: no negative zero"
    assert lenient[2][2] == "-2.4" and lenient[4][2] == "2.4", "half away from zero"
    assert lenient[4][1] == "trailing" and lenient[5][1] == "mixed case"
    assert strict[1][4] == "true" and strict[7][4] == NULL


def test_row_hash_is_md5_split_into_two_unsigned_32_bit_halves() -> None:
    assert row_hash("abc") == (0x90015098, 0x3CD24FB0)


# -- engines ---------------------------------------------------------------------------


def _check(connector: Any, policy: str) -> None:
    rules = POLICIES[policy]
    rendered = render(connector.dialect, GOLDEN, rules, ZONE, ZONE_NAMES)
    expected = _expected(policy)
    got = connector.fetch(GOLDEN, rendered, 0, 1_000_000, [0])
    for key in sorted(expected):
        assert got[key] == expected[key], f"{connector.engine} {policy} row {key}"
    summaries = connector.summarize(GOLDEN, rendered, 0, 1_000_000, None, [])
    (summary,) = summaries.values()
    assert summary.rows == len(ROWS)
    assert (summary.h1, summary.h2) == _hash_sums(expected), f"{connector.engine} {policy} sums"


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_duckdb_matches_the_reference(policy: str) -> None:
    connector = DuckDBConnector(duckdb.connect(), local_zone=ZONE, schema="golden_check")
    connector.recreate({"golden": _arrow()}, [GOLDEN])
    _check(connector, policy)


@pytest.fixture(scope="module")
def postgres() -> Iterator[PostgresConnector]:
    try:
        connector = PostgresConnector(
            postgres_params(REPO_ROOT / ".env"), local_zone=ZONE, schema="golden_check"
        )
    except Exception as error:  # noqa: BLE001 - any failure to connect means skip
        pytest.skip(f"Postgres not reachable ({type(error).__name__}); run `make up`")
    connector.recreate({"golden": _arrow()}, [GOLDEN])
    yield connector
    connector.connection.execute("DROP SCHEMA golden_check CASCADE")
    connector.close()


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_postgres_matches_the_reference(postgres: PostgresConnector, policy: str) -> None:
    _check(postgres, policy)


@pytest.fixture(scope="module")
def sqlserver() -> Iterator[SqlServerConnector]:
    pytest.importorskip("pymssql", reason="install with `uv sync --extra sqlserver`")
    try:
        connector = SqlServerConnector(
            sqlserver_params(REPO_ROOT / ".env", database="golden_check"),
            local_zone=ZONE,
            zone_names=ZONE_NAMES,
        )
    except Exception as error:  # noqa: BLE001 - any failure to connect means skip
        pytest.skip(f"SQL Server not reachable ({type(error).__name__}); see the sqlserver profile")
    connector.recreate({"golden": _arrow()}, [GOLDEN])
    yield connector
    cursor = connector.connection.cursor()
    cursor.execute("USE master")
    cursor.execute("ALTER DATABASE golden_check SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
    cursor.execute("DROP DATABASE golden_check")
    connector.close()


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_sqlserver_matches_the_reference(sqlserver: SqlServerConnector, policy: str) -> None:
    _check(sqlserver, policy)
