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

3. **The parallel run** (Phase 12, ``company.parallel_run_weeks``): dbt builds
   every night and the migration is reconciled every night (``nightly``).
4. **Production** (Phase 14, ``company.production_weeks``), the last weeks:
   ClickHouse is what the business runs on. Every night runs ``dbt source
   freshness``, ``dbt run``, ``dbt snapshot`` and ``dbt test`` as separate
   invocations, the way a production schedule does, and hands each one's
   artifacts to the incident desk (``desk``). Faults happen in operation
   (``faults``), and the desk tells the company when each owner's fix lands.

Everything is seeded and runs on the simulated clock, so two runs leave the same
data in Postgres and ClickHouse. The cadence is a setting because a build per
day costs time and changes nothing the later phases measure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import DbtError, invocation_from_settings, run_dbt
from platform_ops.common.servers import (
    MissingPasswordError,
    clickhouse_client,
    connect_postgres,
    require_passwords,
)
from platform_ops.common.store import (
    dashboard_password,
    drop_dashboard_role,
    ensure_dashboard_role,
    open_store,
    ops_table,
    replace_ops_table,
)
from platform_ops.common.tagging import Tag, current_run, start_run
from platform_ops.simulation import faults, raw_data
from platform_ops.simulation.access import DBT_LAYERS, company_users, drop_users, ensure_users
from platform_ops.simulation.faults import Fault
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
    production_nights: int = 0
    faults: list[str] = field(default_factory=list)


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
    missing: list[str] = []
    for check in (
        lambda: dashboard_password(settings),
        lambda: require_passwords(settings, [u.name for u in company_users(settings)]),
    ):
        try:
            check()
        except MissingPasswordError as error:
            missing += error.variables
    if missing:
        raise MissingPasswordError(missing)
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
        (None, (company.legacy_schema, company.reporting_schema, company.frozen_reporting_schema)),
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


def merge_snapshots(settings: Settings) -> None:
    """Merge every snapshot table down to one part before dbt reads it again.

    A snapshot appends a part every night, and ClickHouse merges parts in the
    background at moments no one chooses, so the snapshot's next read would
    cost a different number of bytes from one run to the next (Phase 13's
    reason for merging raw every night; found here in Phase 14).
    """
    database = settings.company.name("snapshots")
    client = clickhouse_client(settings)
    try:
        tables = client.query(
            "SELECT name FROM system.tables WHERE database = %(db)s "
            "AND endsWith(engine, 'MergeTree') ORDER BY name",
            parameters={"db": database},
        ).result_rows
        for (name,) in tables:
            client.command(f"OPTIMIZE TABLE `{database}`.`{name}` FINAL")
    finally:
        client.close()


def build(settings: Settings, *, full_refresh: bool = False, at: datetime | None = None) -> None:
    """``dbt build`` of the company's project on ClickHouse, tagged with its simulated time."""
    command = ["build", "--full-refresh"] if full_refresh else ["build"]
    comment = Tag(at, "dbt").comment() if at is not None and current_run() else ""
    merge_snapshots(settings)
    run_dbt(invocation_from_settings(settings, log_comment=comment), command)


# A production night's schedule: separate invocations, not `dbt build`, so a
# failing test does not stop the models below it and the alert storm a real
# night produces is what the incident desk sees (ADR 0005).
NIGHTLY = (["source", "freshness"], ["run"], ["snapshot"], ["test"])
# Failing checks are expected here, so dbt's console would only be a wall of
# red; its log file under the target's logs/ keeps the detail.
QUIET = ["--log-level", "none", "--log-level-file", "info"]


def production_night(settings: Settings, at: datetime, desk: Desk | None, manifest: Any) -> str:
    """One night of production: every invocation, each handed to the desk as it finishes.

    ``manifest`` is the project parsed once for the production weeks: freshness
    reads ``simulated_now`` when it runs, not when dbt parses, so the saved
    parse still applies and no night pays for a full re-parse (ADR 0005).
    """
    run_id = f"night-{at:%Y-%m-%d}"
    comment = Tag(at, "dbt").comment() if current_run() else ""
    for command in NIGHTLY:
        simulated = at if command[0] == "source" else None
        invocation = invocation_from_settings(
            settings, simulated_now=simulated, log_comment=comment
        )
        # Each invocation leaves only its own result file for the desk: a
        # run_results.json from an earlier build is not part of tonight's run.
        for stale in ("run_results.json", "sources.json"):
            (invocation.target_path / stale).unlink(missing_ok=True)
        if command == ["snapshot"]:
            merge_snapshots(settings)
        outcome = run_dbt(invocation, [*command, *QUIET], check=False, manifest=manifest)
        if outcome.exception is not None:
            raise DbtError(f"dbt {' '.join(command)} crashed on {at:%Y-%m-%d}: {outcome.exception}")
        if desk is not None:
            desk.ingest(run_id, at, invocation.target_path)
    return run_id


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
    # Each legacy table's watermark after tonight's copy: the reconciliation
    # compares it as of this, and anything written since the next night.
    watermarks: dict[str, datetime] = field(default_factory=dict)
    # When the legacy reports were frozen for tonight (``Reconciliation.freeze``).
    frozen_at: datetime | None = None


@dataclass(frozen=True)
class Fix:
    """An owner's fix for an incident, landing at ``at``: the faulted sources it covers."""

    incident_id: str
    tables: tuple[str, ...]
    at: datetime


class Reconciliation(Protocol):
    """Whoever reconciles the parallel run: freezes the legacy reports, then compares a night."""

    def freeze(self, at: datetime) -> None:
        """Freeze the legacy reports, just before the copy reads up to ``at``."""
        ...

    def __call__(self, night: Night) -> None: ...


class Desk(Protocol):
    """Whoever watches production: reads each dbt invocation, schedules the fixes."""

    def ingest(self, run_id: str, at: datetime, target: Path) -> None: ...

    def fixes(self) -> list[Fix]:
        """Fixes scheduled since the last call."""
        ...


@dataclass(frozen=True)
class Timeline:
    """When the parallel run and production start, when the fix ships, and the fault nights."""

    parallel_start: int
    production_start: int
    fix_night: int
    faults: dict[int, OneOffFault]
    # Day index -> the production faults that happen during that day.
    incidents: dict[int, tuple[Fault, ...]]

    @classmethod
    def of(cls, settings: Settings, total_days: int) -> Timeline:
        company = settings.company
        production = min(total_days, company.production_weeks * 7)
        production_start = total_days - production
        nights = min(production_start, company.parallel_run_weeks * 7)
        start = production_start - nights
        fix = min(start + company.fix_after_nights, production_start) if nights else start
        planted = faults.schedule(settings.incidents.fault_days, production) if production else {}
        return cls(
            start,
            production_start,
            fix,
            fault_nights(fix + 2, production_start - 1) if nights else {},
            {production_start + day: pair for day, pair in planted.items()},
        )

    def parallel(self, night: int) -> bool:
        return self.parallel_start <= night < self.production_start

    def production(self, night: int) -> bool:
        return night >= self.production_start

    def delivered(self, night: int) -> bool:
        """The job runs as delivered until the fix ships (only when there is a parallel run)."""
        return self.parallel_start < self.fix_night and night < self.fix_night


def run_company(
    settings: Settings,
    days: int | None = None,
    nightly: Reconciliation | None = None,
    daily: Callable[[Day], None] | None = None,
    desk: Desk | None = None,
) -> CompanySummary:
    """Seed the history, then replay ``days`` simulated days (the whole window by default).

    Every day: the day's data reaches Postgres, the ingestion loads it, the
    platform merges raw, dbt builds on its cadence, and the dashboards and
    people query the marts. During the parallel run dbt builds every night;
    ``nightly`` (the reconciliation) freezes the legacy reports before the copy
    reads, the application keeps writing after it
    (``company.writes_after_ingest_minutes``), and ``nightly`` compares the
    night after its build. In production,
    every night runs freshness, run, snapshot and test, each handed to ``desk``,
    and faults are planted and fixed on the desk's schedule. ``daily`` is called
    at the end of every day. Every query is tagged for the cost module.
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
        faults.reset_ground_truth(settings)
        loaded_until = window.start
        active: dict[str, Fault] = {}
        pending: list[Fix] = []
        manifest: Any = None
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
            last_load, loaded_until = loaded_until, run_at
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
            # Production faults during yesterday's working day, and the fixes
            # owners landed since the last run, in time order.
            happened: list[tuple[datetime, int, str, Fault | Fix]] = []
            if day_index - 1 in timeline.incidents:
                when = day - timedelta(days=1) + timedelta(hours=settings.incidents.fault_hour)
                happened += [(when, 1, f.fault_id, f) for f in timeline.incidents[day_index - 1]]
            happened += [(fix.at, 0, fix.incident_id, fix) for fix in pending if fix.at <= run_at]
            pending = [fix for fix in pending if fix.at > run_at]
            for at, _, _, item in sorted(happened, key=lambda h: h[:3]):
                if isinstance(item, Fault):
                    if item.table in active:
                        raise RuntimeError(f"{item.fault_id}: raw.{item.table} is already faulted")
                    faults.plant(settings, admin, item, at)
                    active[item.table] = item
                    summary.faults.append(item.fault_id)
                    continue
                for table in item.tables:
                    if table in active:
                        summary.rows_ingested += ingestion.resync(table, last_load)
                        faults.repaired(settings, active.pop(table), at)
            parallel = timeline.parallel(day_index)
            frozen_at = None
            if parallel and nightly is not None:
                nightly.freeze(run_at)
                frozen_at = run_at
            if backfill:
                # The fixed job ships: raw is reloaded in full, then rebuilt.
                ingestion.delivered = False
                summary.rows_ingested += sum(ingestion.backfill(run_at).values())
            else:
                stopped = {table for table, f in active.items() if f.stops_loader}
                summary.rows_ingested += sum(ingestion.load(run_at, skip=stopped).values())
            fault = timeline.faults.get(day_index)
            if fault is not None:
                planted = plant(settings, admin, fault)
            _record(
                settings,
                "raw_maintenance",
                ["sim_at", "table_name", "rows_rewritten", "bytes_rewritten"],
                optimize_raw(settings, admin, run_at),
            )
            late = timedelta(minutes=settings.company.writes_after_ingest_minutes)
            if parallel and late:
                # The next day's first writes: tomorrow's copy reads them.
                arrived = legacy.arrive_new(after=run_at, until=run_at + late)
                summary.rows_loaded += sum(arrived.values())
                loaded_until = run_at + late
            if timeline.production(day_index):
                if manifest is None:
                    manifest = run_dbt(invocation_from_settings(settings), ["parse", *QUIET]).result
                production_night(settings, run_at, desk, manifest)
                summary.production_nights += 1
                summary.builds.append(run_at)
                if desk is not None:
                    pending += desk.fixes()
            elif parallel or day_index % every == 0 or day_index == total_days - 1:
                build(settings, full_refresh=not summary.builds or backfill, at=run_at)
                summary.builds.append(run_at)
            workload.run_day(day_index, day)
            if parallel and nightly is not None:
                truth: list[TruthCell] = []
                if ingestion.delivered:
                    with connect_postgres(settings) as connection:
                        truth += delivered_truth(settings, connection, as_of=run_at)
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
                        watermarks=ingestion.watermarks(),
                        frozen_at=frozen_at,
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
