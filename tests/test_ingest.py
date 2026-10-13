"""The legacy Postgres and the incremental ingestion into ClickHouse (version 2.0).

One small company is seeded at scale 0.01 in its own namespace and backfilled
into ClickHouse. Each test then changes rows in Postgres the way the legacy
application would, relative to the current watermark, and loads again, so the
tests do not depend on their order. They need ``make up``; without the servers
they skip with the reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.servers import clickhouse_client, connect_postgres
from platform_ops.simulation import raw_data
from platform_ops.simulation.company import remove_company, start_over
from platform_ops.simulation.ingest import Ingestion
from platform_ops.simulation.legacy_db import LegacyCompany

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def company(
    tmp_path_factory: pytest.TempPathFactory,
    isolated_config: Callable[..., Path],
    servers_or_skip: Callable[[Settings], None],
) -> Iterator[tuple[Settings, Ingestion]]:
    root = tmp_path_factory.mktemp("ingest")
    settings = load_settings(isolated_config(root, 0.01, namespace="t_ingest_"))
    servers_or_skip(settings)
    legacy = LegacyCompany(settings)
    ingestion = Ingestion(settings)
    try:
        start_over(settings, legacy, ingestion)
        yield settings, ingestion
    finally:
        legacy.close()
        ingestion.close()
        remove_company(settings)


def _postgres(settings: Settings, query: sql.Composable, params: Any = ()) -> list[Any]:
    with connect_postgres(settings) as connection:
        rows = connection.execute(query, params).fetchall()
        connection.commit()
    return rows


def _customers(settings: Settings) -> sql.Composed:
    schema = settings.company.name(settings.company.legacy_schema)
    return sql.SQL("{}.customers").format(sql.Identifier(schema))


def _raw(settings: Settings, query: str) -> list[tuple[Any, ...]]:
    client = clickhouse_client(settings)
    try:
        return list(client.query(query).result_rows)
    finally:
        client.close()


def _change_email(settings: Settings, customer_id: int, email: str, at: datetime) -> None:
    _postgres(
        settings,
        sql.SQL(
            "UPDATE {} SET email = %s, updated_at = %s WHERE customer_id = %s RETURNING 1"
        ).format(_customers(settings)),
        [email, at, customer_id],
    )


def _raw_emails(settings: Settings, customer_id: int, final: bool = True) -> list[str]:
    raw = settings.company.name(settings.company.raw_database)
    rows = _raw(
        settings,
        f"SELECT email FROM `{raw}`.customers{' FINAL' if final else ''} "
        f"WHERE customer_id = {customer_id}",
    )
    return [email for (email,) in rows]


def _customer_ids(settings: Settings, count: int, offset: int) -> list[int]:
    rows = _postgres(
        settings,
        sql.SQL("SELECT customer_id FROM {} ORDER BY customer_id LIMIT %s OFFSET %s").format(
            _customers(settings)
        ),
        [count, offset],
    )
    return [customer_id for (customer_id,) in rows]


# -- the legacy database -------------------------------------------------------------------


def test_every_operational_table_has_a_primary_key_and_updated_at(
    company: tuple[Settings, Ingestion],
) -> None:
    settings, _ = company
    schema = settings.company.name(settings.company.legacy_schema)
    keys = _postgres(
        settings,
        sql.SQL(
            "SELECT tc.table_name, kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "USING (constraint_schema, constraint_name) "
            "WHERE tc.table_schema = %s AND tc.constraint_type = 'PRIMARY KEY'"
        ),
        [schema],
    )
    assert dict(keys) == raw_data.PRIMARY_KEYS
    for table in raw_data.TABLES:
        ((latest,),) = _postgres(
            settings,
            sql.SQL("SELECT max(updated_at) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
        )
        assert latest is None or latest <= settings.simulation.start, table


def test_the_backfill_copies_every_row_once(company: tuple[Settings, Ingestion]) -> None:
    settings, _ = company
    schema = settings.company.name(settings.company.legacy_schema)
    raw = settings.company.name(settings.company.raw_database)
    for table in raw_data.TABLES:
        ((legacy,),) = _postgres(
            settings,
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
        )
        ((ingested,),) = _raw(settings, f"SELECT count() FROM `{raw}`.`{table}` FINAL")
        assert ingested == legacy, table


# -- incremental loads ---------------------------------------------------------------------


def test_an_in_place_change_keeps_only_the_latest_version(
    company: tuple[Settings, Ingestion],
) -> None:
    settings, ingestion = company
    mark = ingestion.watermarks()["customers"]
    (customer,) = _customer_ids(settings, 1, offset=0)
    before = _raw_emails(settings, customer)

    _change_email(settings, customer, "changed@example.test", mark + timedelta(minutes=30))
    moved = ingestion.load(mark + timedelta(hours=1))

    assert moved["customers"] >= 1
    assert _raw_emails(settings, customer) == ["changed@example.test"] != before
    # Until ClickHouse merges the parts, both versions may still be stored.
    assert len(_raw_emails(settings, customer, final=False)) in (1, 2)
    assert ingestion.watermarks()["customers"] == mark + timedelta(hours=1)


def test_the_overlap_catches_a_late_commit_but_not_an_older_one(
    company: tuple[Settings, Ingestion],
) -> None:
    """A row that committed after the last load, stamped before it, is still read if it
    falls inside the overlap window. Older than that, it is missed: the limit of a
    watermark without change data capture, and why the overlap is a setting."""
    settings, ingestion = company
    mark = ingestion.watermarks()["customers"]
    overlap = timedelta(minutes=settings.company.ingest_overlap_minutes)
    late, too_late = _customer_ids(settings, 2, offset=1)
    old_email = _raw_emails(settings, too_late)

    _change_email(settings, late, "late@example.test", mark - overlap / 2)
    _change_email(settings, too_late, "too-late@example.test", mark - overlap * 2)
    ingestion.load(mark + timedelta(hours=1))

    assert _raw_emails(settings, late) == ["late@example.test"]
    assert _raw_emails(settings, too_late) == old_email
