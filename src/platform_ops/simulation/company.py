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

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.common.servers import clickhouse_client
from platform_ops.simulation.access import DBT_LAYERS, drop_users, ensure_users
from platform_ops.simulation.ingest import Ingestion
from platform_ops.simulation.legacy_db import LegacyCompany


@dataclass
class CompanySummary:
    days: int = 0
    rows_seeded: int = 0
    rows_loaded: int = 0
    rows_changed: int = 0
    rows_ingested: int = 0
    builds: list[datetime] = field(default_factory=list)


def company_databases(settings: Settings) -> list[str]:
    """Every ClickHouse database the company has, namespaced, raw first."""
    company = settings.company
    return [company.name(layer) for layer in (company.raw_database, *DBT_LAYERS)]


def start_over(settings: Settings, legacy: LegacyCompany, ingestion: Ingestion) -> tuple[int, int]:
    """Users, then seed Postgres, empty ClickHouse, backfill raw.

    Returns (rows seeded, rows ingested). The users come first, so a missing
    password stops the run before anything is written.
    """
    ensure_users(settings)
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


def build(settings: Settings, *, full_refresh: bool = False) -> None:
    """``dbt build`` of the company's project on ClickHouse."""
    command = ["build", "--full-refresh"] if full_refresh else ["build"]
    run_dbt(invocation_from_settings(settings), command)


def run_company(settings: Settings, days: int | None = None) -> CompanySummary:
    """Seed the history, then replay ``days`` simulated days (the whole window by default)."""
    window = settings.simulation
    total_days = window.weeks * 7 if days is None else days
    every = settings.company.build_every_days
    workload = settings.workload
    scale = settings.scale_factor
    summary = CompanySummary()

    legacy = LegacyCompany(settings)
    ingestion = Ingestion(settings)
    try:
        summary.rows_seeded, summary.rows_ingested = start_over(settings, legacy, ingestion)
        loaded_until = window.start
        for day_index in range(total_days):
            day = window.start + timedelta(days=day_index)
            run_at = day + timedelta(hours=window.daily_run_hour)
            loaded, changed = legacy.arrive(
                day_index,
                after=loaded_until,
                until=run_at,
                changes_at=run_at - timedelta(hours=1),
                price_changes=max(1, round(workload.product_price_changes_per_day * scale)),
                profile_changes=max(1, round(workload.customer_profile_changes_per_day * scale)),
            )
            loaded_until = run_at
            summary.rows_loaded += sum(loaded.values())
            summary.rows_changed += sum(changed.values())
            summary.rows_ingested += sum(ingestion.load(run_at).values())
            if day_index % every == 0 or day_index == total_days - 1:
                build(settings, full_refresh=not summary.builds)
                summary.builds.append(run_at)
            summary.days += 1
    finally:
        legacy.close()
        ingestion.close()
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
