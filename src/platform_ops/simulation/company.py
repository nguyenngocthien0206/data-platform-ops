"""The simulated company over its window, on Postgres and ClickHouse (version 2.0).

``platform-ops company run`` replays the company's days the way its platform
lives them:

1. **History.** The company's ClickHouse users are created (``access``), the
   legacy Postgres is seeded with everything up to the window
   start, every company database in ClickHouse is dropped (a dbt snapshot
   survives ``--full-refresh``, so nothing less starts clean), and the raw
   layer is backfilled from Postgres.
2. **Every simulated day**, in order:

   - the day's new rows reach Postgres (orders, payments, sessions...), and at
     01:00 some products and customers change in place;
   - at the daily run hour (02:00), the incremental ingestion pulls everything
     with a newer ``updated_at`` into ClickHouse;
   - on the build cadence (``company.build_every_days``) and on the last day,
     dbt builds the project on ClickHouse. The first build is a full refresh;
     later ones are incremental where a model is.

Everything is seeded and runs on the simulated clock, so two runs leave the same
data in Postgres and ClickHouse. The cadence is a setting because a build per
day costs time and changes nothing the later phases measure; the incident
scenario (Phase 14) runs its own builds on the nights it needs them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.common.servers import clickhouse_client, connect_postgres
from platform_ops.common.store import (
    dashboard_password,
    drop_dashboard_role,
    ensure_dashboard_role,
    open_store,
    ops_table,
    replace_ops_table,
)
from platform_ops.common.tagging import Tag, current_run, start_run
from platform_ops.simulation import raw_data
from platform_ops.simulation.access import DBT_LAYERS, drop_users, ensure_users
from platform_ops.simulation.ingest import Ingestion
from platform_ops.simulation.legacy_db import LegacyCompany
from platform_ops.simulation.migration import (
    OneOffFault,
    Planted,
    TruthCell,
    delivered_truth,
    fault_nights,
    plant,
    repair,
)
from platform_ops.simulation.workload import Workload


@dataclass
class CompanySummary:
    days: int = 0
    rows_seeded: int = 0
    rows_loaded: int = 0
    rows_changed: int = 0
    rows_ingested: int = 0
    builds: list[datetime] = field(default_factory=list)
    queries: dict[str, int] = field(default_factory=dict)


def company_databases(settings: Settings) -> list[str]:
    """Every ClickHouse database the company has, namespaced, raw first."""
    company = settings.company
    return [company.name(layer) for layer in (company.raw_database, *DBT_LAYERS)]


def start_over(settings: Settings, legacy: LegacyCompany, ingestion: Ingestion) -> tuple[int, int]:
    """Users, then seed Postgres, empty ClickHouse, backfill raw.

    Returns (rows seeded, rows ingested). The users come first, so a missing
    password stops the run before anything is written: the dashboards' read-only
    role on the ops store, then the company's ClickHouse users.
    """
    dashboard_password(settings)
    ensure_users(settings)
    ensure_dashboard_role(settings)
    seeded = sum(legacy.seed().values())
    client = clickhouse_client(settings)
    try:
        for database in company_databases(settings):
            client.command(f"DROP DATABASE IF EXISTS `{database}`")
    finally:
        client.close()
    ingestion.recreate()
    ingested = sum(ingestion.load(settings.simulation.start).values())
    return seeded, ingested


def remove_company(settings: Settings) -> None:
    """Drop everything the company has on both servers: its ClickHouse databases,
    its users, its legacy and reporting schemas, and the toolkit's ops schema.

    The tests use it to clean up their namespace; it is never part of a run.
    """
    from psycopg import sql

    from platform_ops.common.servers import connect_postgres

    company = settings.company
    client = clickhouse_client(settings)
    try:
        for database in company_databases(settings):
            client.command(f"DROP DATABASE IF EXISTS `{database}`")
    finally:
        client.close()
    drop_users(settings)
    drop_dashboard_role(settings)
    for dbname, schemas in (
        (None, (company.legacy_schema, company.reporting_schema)),
        (company.ops_database, (company.ops_schema,)),
    ):
        with connect_postgres(settings, dbname) as connection:
            for schema in schemas:
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(company.name(schema))
                    )
                )
            connection.commit()


def build(settings: Settings, *, full_refresh: bool = False, at: datetime | None = None) -> None:
    """``dbt build`` of the company's project on ClickHouse, tagged with its simulated time."""
    command = ["build", "--full-refresh"] if full_refresh else ["build"]
    comment = Tag(at, "dbt").comment() if at is not None and current_run() else ""
    run_dbt(invocation_from_settings(settings, log_comment=comment), command)


# What the company's run records about itself in the ops store, for the cost
# module: its run id (the tag in the query log), the platform's nightly
# maintenance, and which sources changed rows in place (Phase 13).
COMPANY_RUN_DDL = "run_id text NOT NULL, window_start timestamp NOT NULL, days integer NOT NULL"
MAINTENANCE_DDL = """
    sim_at timestamp NOT NULL, table_name text NOT NULL,
    rows_rewritten bigint NOT NULL, bytes_rewritten bigint NOT NULL"""
SOURCE_CHANGES_DDL = (
    "sim_at timestamp NOT NULL, table_name text NOT NULL, changed_rows bigint NOT NULL"
)


def _record(settings: Settings, name: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    with open_store(settings) as store:
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
            ops_table(settings, name), sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        with store.cursor().copy(statement) as copy:
            for row in rows:
                copy.write_row(row)


def optimize_raw(settings: Settings, admin: Any, at: datetime) -> list[tuple[Any, ...]]:
    """The platform's nightly maintenance: merge every raw table down to one version per key.

    A ``ReplacingMergeTree`` keeps every version of a row until ClickHouse
    merges it in the background, at a moment no one chooses, so reading raw
    with ``FINAL`` costs a different amount from one run to the next.
    ``OPTIMIZE ... FINAL`` after the day's load makes every later read, and so
    every measured cost, repeat exactly (Phase 13). The merge runs in a
    background thread the query log does not measure, so what it rewrote is
    recorded from the merged part: its rows and uncompressed bytes.
    """
    raw = settings.company.name(settings.company.raw_database)
    records: list[tuple[Any, ...]] = []
    for table in raw_data.TABLES:
        admin.command(f"OPTIMIZE TABLE `{raw}`.`{table}` FINAL")
        rows, size = admin.query(
            "SELECT sum(rows), sum(data_uncompressed_bytes) FROM system.parts "
            "WHERE database = %(db)s AND table = %(t)s AND active",
            parameters={"db": raw, "t": table},
        ).result_rows[0]
        records.append((at, table, int(rows or 0), int(size or 0)))
    return records


@dataclass(frozen=True)
class Day:
    """A simulated day after its build and workload, for whoever watches the run."""

    index: int
    run_at: datetime
    last_of_month: bool
    last: bool


@dataclass(frozen=True)
class Night:
    """One night of the parallel run, after its build: what the reconciliation is told.

    ``truth`` is every discrepancy planted and not yet repaired, which only the
    grading reads; the reconciliation itself compares the two sides blind.
    """

    index: int
    run_at: datetime
    delivered: bool  # the ingestion still runs as first delivered
    backfilled: bool  # the fixed job shipped tonight and reloaded raw
    planted: str  # the fault planted tonight, if any
    repaired: str  # the fault repaired tonight, if any
    truth: tuple[TruthCell, ...]


@dataclass(frozen=True)
class Timeline:
    """When the parallel run starts, when the fix ships, and which fault falls on which night."""

    parallel_start: int
    fix_night: int
    faults: dict[int, OneOffFault]

    @classmethod
    def of(cls, settings: Settings, total_days: int) -> Timeline:
        company = settings.company
        nights = min(total_days, company.parallel_run_weeks * 7)
        start = total_days - nights if nights else total_days
        fix = min(start + company.fix_after_nights, total_days) if nights else total_days
        return cls(start, fix, fault_nights(fix + 2, total_days - 1) if nights else {})

    def parallel(self, night: int) -> bool:
        return night >= self.parallel_start

    def delivered(self, night: int) -> bool:
        """The job runs as delivered until the fix ships (only when there is a parallel run)."""
        return self.parallel_start < self.fix_night and night < self.fix_night


def run_company(
    settings: Settings,
    days: int | None = None,
    nightly: Callable[[Night], None] | None = None,
    daily: Callable[[Day], None] | None = None,
) -> CompanySummary:
    """Seed the history, then replay ``days`` simulated days (the whole window by default).

    Every day: the day's data reaches Postgres, the ingestion loads it, the
    platform merges raw, dbt builds on its cadence, and the dashboards and
    people query the marts. During the parallel run dbt builds every night and
    ``nightly`` (the reconciliation) is called after each build; ``daily`` is
    called at the end of every day. Every query is tagged for the cost module.
    """
    window = settings.simulation
    total_days = window.weeks * 7 if days is None else days
    every = settings.company.build_every_days
    workload_settings = settings.workload
    scale = settings.scale_factor
    timeline = Timeline.of(settings, total_days)
    summary = CompanySummary()
    run_id = start_run()

    legacy = LegacyCompany(settings)
    ingestion = Ingestion(settings, delivered=timeline.delivered(0))
    admin = clickhouse_client(settings)
    workload = Workload(settings)
    planted: Planted | None = None
    try:
        summary.rows_seeded, summary.rows_ingested = start_over(settings, legacy, ingestion)
        with open_store(settings) as store:
            replace_ops_table(
                store,
                settings,
                "company_run",
                COMPANY_RUN_DDL,
                ["run_id", "window_start", "days"],
                [(run_id, window.start, total_days)],
            )
            replace_ops_table(store, settings, "raw_maintenance", MAINTENANCE_DDL, [], [])
            replace_ops_table(store, settings, "source_changes", SOURCE_CHANGES_DDL, [], [])
        loaded_until = window.start
        for day_index in range(total_days):
            day = window.start + timedelta(days=day_index)
            run_at = day + timedelta(hours=window.daily_run_hour)
            loaded, changed = legacy.arrive(
                day_index,
                after=loaded_until,
                until=run_at,
                changes_at=run_at - timedelta(hours=1),
                price_changes=max(
                    1, round(workload_settings.product_price_changes_per_day * scale)
                ),
                profile_changes=max(
                    1, round(workload_settings.customer_profile_changes_per_day * scale)
                ),
            )
            loaded_until = run_at
            summary.rows_loaded += sum(loaded.values())
            summary.rows_changed += sum(changed.values())
            _record(
                settings,
                "source_changes",
                ["sim_at", "table_name", "changed_rows"],
                [(run_at - timedelta(hours=1), t, n) for t, n in sorted(changed.items()) if n],
            )
            backfill = ingestion.delivered and not timeline.delivered(day_index)
            repaired = ""
            if planted is not None:
                repair(settings, admin, planted, run_at)
                repaired, planted = planted.fault.fault_id, None
            if backfill:
                # The fixed job ships: raw is reloaded in full, then rebuilt.
                ingestion.delivered = False
                summary.rows_ingested += sum(ingestion.backfill(run_at).values())
            else:
                summary.rows_ingested += sum(ingestion.load(run_at).values())
            fault = timeline.faults.get(day_index)
            if fault is not None:
                planted = plant(settings, admin, fault)
            _record(
                settings,
                "raw_maintenance",
                ["sim_at", "table_name", "rows_rewritten", "bytes_rewritten"],
                optimize_raw(settings, admin, run_at),
            )
            parallel = timeline.parallel(day_index)
            if parallel or day_index % every == 0 or day_index == total_days - 1:
                build(settings, full_refresh=not summary.builds or backfill, at=run_at)
                summary.builds.append(run_at)
            workload.run_day(day_index, day)
            if parallel and nightly is not None:
                truth: list[TruthCell] = []
                if ingestion.delivered:
                    with connect_postgres(settings) as connection:
                        truth += delivered_truth(settings, connection)
                if planted is not None:
                    truth += planted.truth()
                nightly(
                    Night(
                        index=day_index,
                        run_at=run_at,
                        delivered=ingestion.delivered,
                        backfilled=backfill,
                        planted=fault.fault_id if fault is not None else "",
                        repaired=repaired,
                        truth=tuple(truth),
                    )
                )
            if daily is not None:
                tomorrow = day + timedelta(days=1)
                daily(
                    Day(
                        index=day_index,
                        run_at=run_at,
                        last_of_month=tomorrow.month != day.month,
                        last=day_index == total_days - 1,
                    )
                )
            summary.days += 1
        summary.queries = dict(workload.summary.queries)
    finally:
        legacy.close()
        ingestion.close()
        admin.close()
        workload.close()
    return summary


def fingerprints(settings: Settings) -> dict[str, tuple[int, int]]:
    """Row count and a content hash of every table and view the company has in ClickHouse.

    Raw tables are read with ``FINAL``: until ClickHouse merges them they may
    hold a row twice (an overlap re-read), and when that merge happens is not
    up to the toolkit, so only the deduplicated content is comparable.
    """
    company = settings.company
    databases = company_databases(settings)
    client = clickhouse_client(settings)
    try:
        tables = client.query(
            "SELECT database, name FROM system.tables WHERE database IN %(dbs)s "
            "ORDER BY database, name",
            parameters={"dbs": databases},
        ).result_rows
        found: dict[str, tuple[int, int]] = {}
        raw = company.name(company.raw_database)
        for database, name in tables:
            final = " FINAL" if database == raw else ""
            count, digest = client.query(
                f"SELECT count(), coalesce(sum(cityHash64(*)), 0) FROM `{database}`.`{name}`{final}"
            ).result_rows[0]
            layer = database.removeprefix(company.namespace)
            found[f"{layer}.{name}"] = (int(count), int(digest))
        return found
    finally:
        client.close()
