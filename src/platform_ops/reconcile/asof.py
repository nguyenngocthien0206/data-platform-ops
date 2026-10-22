"""The legacy reports frozen at the copy's watermark, so a night compares like with like.

A legacy report is a view over the live operational tables, and the legacy
system keeps taking writes after the copy has read it. A table can be compared
as of the copy's watermark (``TableSpec.watermark``, by its ``changed_at``
column), but a view cannot: Postgres keeps no older version of a row changed
in place, so no filter on the tables gives the report as it was.

So each report is frozen into a table of its own, all of them from one
snapshot, at the moment the copy records its watermark: freeze first, then
copy up to the time of the freeze. The reconciliation compares the frozen
reports with the marts built from that copy. A row that commits after the
snapshot with an earlier ``updated_at`` (the late commits the copy's overlap
is there for) reaches the marts and not the frozen report; that night shows it
as a difference, and the next freeze has it.

The reports are frozen in parallel, the way ``pg_dump --jobs`` dumps: one
transaction exports its snapshot (``pg_export_snapshot``) and every worker
reads in that same snapshot (``SET TRANSACTION SNAPSHOT``). One after the
other, the 12 reports took about a minute a night at scale 1.0.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from psycopg import sql


def freeze_reports(
    connect: Callable[[], Any],
    reports_schema: str,
    frozen_schema: str,
    names: Sequence[str],
    workers: int = 1,
) -> None:
    """Recreate ``frozen_schema`` with a table per report, all from one snapshot.

    ``connect`` opens a new psycopg connection to the legacy database; the
    freeze opens one, plus one per worker. Every report is read in the same
    ``REPEATABLE READ`` snapshot, so they agree with each other whatever the
    application writes meanwhile.
    """
    frozen = sql.Identifier(frozen_schema)
    with connect() as leader:
        leader.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(frozen))
        leader.execute(sql.SQL("CREATE SCHEMA {}").format(frozen))
        leader.commit()
        with leader.transaction():
            leader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            (snapshot,) = leader.execute("SELECT pg_export_snapshot()").fetchone()

            def freeze(name: str) -> None:
                with connect() as connection, connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    connection.execute(
                        sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot))
                    )
                    connection.execute(
                        sql.SQL("CREATE TABLE {}.{} AS SELECT * FROM {}.{}").format(
                            frozen,
                            sql.Identifier(name),
                            sql.Identifier(reports_schema),
                            sql.Identifier(name),
                        )
                    )

            # The snapshot lives as long as the leader's transaction: every
            # worker has to import it before the leader commits.
            with ThreadPoolExecutor(max(1, workers)) as pool:
                list(pool.map(freeze, names))


__all__ = ["freeze_reports"]
