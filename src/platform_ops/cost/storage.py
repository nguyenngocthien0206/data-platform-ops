"""What the company stores in ClickHouse, month by month (version 2.0, Phase 13).

Storage is the other half of a self-managed cluster's bill. At the end of each
simulated month, and on the window's last day, every active part of every
company table is summed from ``system.parts``: rows and uncompressed bytes.
Compressed size is not used, because ClickHouse can compress the same data
differently on two runs, and a bill should not move for a reason no owner can
act on; uncompressed bytes repeat exactly once raw is merged each night
(``simulation.company.optimize_raw``).
"""

from __future__ import annotations

from typing import Any

from platform_ops.common.config import Settings
from platform_ops.common.servers import clickhouse_client
from platform_ops.common.store import open_store, ops_table, replace_ops_table

SNAPSHOTS_DDL = """
    month text NOT NULL, database_name text NOT NULL, table_name text NOT NULL,
    rows bigint NOT NULL, bytes bigint NOT NULL"""
SNAPSHOTS_COLUMNS = ["month", "database_name", "table_name", "rows", "bytes"]


def snapshot(settings: Settings, client: Any, month: str) -> list[tuple[Any, ...]]:
    """Rows and uncompressed bytes of every company table, as of now."""
    from platform_ops.simulation.company import company_databases

    rows = client.query(
        "SELECT database, table, sum(rows), sum(data_uncompressed_bytes) FROM system.parts "
        "WHERE active AND database IN %(dbs)s GROUP BY database, table ORDER BY database, table",
        parameters={"dbs": company_databases(settings)},
    ).result_rows
    namespace = settings.company.namespace
    return [
        (month, str(db).removeprefix(namespace), str(table), int(n), int(b))
        for db, table, n, b in rows
    ]


class StorageWatch:
    """The company run's ``daily`` hook: one snapshot at each month's end."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        with open_store(settings) as store:
            replace_ops_table(
                store, settings, "storage_snapshots", SNAPSHOTS_DDL, SNAPSHOTS_COLUMNS, []
            )

    def __call__(self, day: Any) -> None:
        if not (day.last_of_month or day.last):
            return
        client = clickhouse_client(self.settings)
        try:
            rows = snapshot(self.settings, client, f"{day.run_at:%Y-%m}")
        finally:
            client.close()
        from psycopg import sql

        with open_store(self.settings) as store:
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                ops_table(self.settings, "storage_snapshots"),
                sql.SQL(", ").join(map(sql.Identifier, SNAPSHOTS_COLUMNS)),
            )
            with store.cursor().copy(statement) as copy:
                for row in rows:
                    copy.write_row(row)
