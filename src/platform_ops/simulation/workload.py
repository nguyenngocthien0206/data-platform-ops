"""The simulated workload: 13 weeks of a data platform in a few minutes.

Every simulated day:

1. New raw data arrives up to the scheduled dbt run, and a few products and
   customers change in place.
2. The raw sources are measured (logical sizes) and checked for append-only
   growth.
3. dbt runs at 02:00. Every ``workload.real_build_every_days`` (28) it runs for
   real; on the other days the latest real build is replayed, stamped with that
   day's time and priced later on that day's table sizes (ADR 0007).
4. Dashboards refresh on their own schedule, reading every table they depend on.
5. A few people run ad hoc queries of mixed quality during office hours.

Everything that looks random is derived from the seed with a stable hash, never
from Python's ``hash()`` (randomised per process) or the wall clock. A
simulation always starts from a fresh seed, so running it twice gives the same
workload.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import duckdb

from platform_ops.common.clock import SimulatedClock
from platform_ops.common.config import ActorType, Settings
from platform_ops.common.db import begin, commit, connect, in_transaction
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.common.logging import get_logger, log_event
from platform_ops.cost import growth, sizes
from platform_ops.cost.collect import (
    DbtRun,
    LoggedConnection,
    QueryLog,
    catalog_from,
    dbt_run_records,
    read_dbt_run,
)
from platform_ops.cost.schema import reset_collection_tables
from platform_ops.metadata.lineage import build_graph, persist_edges
from platform_ops.metadata.manifest import Node, load_manifest
from platform_ops.metadata.registry import Registry
from platform_ops.simulation import raw_data

MODEL_SCHEMAS = ("staging", "intermediate", "marts")
_DATE_TYPES = ("DATE", "TIMESTAMP")


def stable_hash(*parts: object) -> int:
    """A 64-bit hash that is the same in every process and on every machine."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


# --- ad hoc queries --------------------------------------------------------------
#
# What people actually type, per team, from careful to careless. Placeholders:
# {d1}, {d7}, {d14}, {d30} are dates that many days before the query, {n} is a
# number. Only live tables appear here: nobody queries the abandoned models,
# which is exactly why they are waste.

ADHOC_TEMPLATES: dict[str, tuple[str, ...]] = {
    "sales": (
        "select * from marts.sales_fct_order_items",
        "select channel, count(*) as orders, sum(net_amount) as revenue "
        "from marts.sales_fct_orders where order_date >= date '{d30}' group by 1",
        "select period_start, orders, net_revenue from marts.sales_rpt_orders_weekly_by_channel "
        "where channel = 'web' order by period_start",
        "select c.value_segment, count(*) as orders from marts.sales_fct_orders o "
        "join marts.sales_dim_customers c using (customer_id) "
        "where o.order_date >= date '{d7}' group by 1",
    ),
    "marketing": (
        "select * from staging.stg_web_sessions",
        "select traffic_source, count(*) as sessions from marts.marketing_fct_sessions "
        "where session_date >= date '{d14}' group by 1",
        "select campaign_name, spend, attributed_revenue, return_on_ad_spend "
        "from marts.marketing_fct_campaign_performance order by return_on_ad_spend desc",
    ),
    "finance": (
        "select payment_status, count(*) as orders, sum(outstanding_amount) as outstanding "
        "from marts.finance_fct_payments_reconciliation "
        "where order_date >= date '{d30}' group by 1",
        "select * from marts.finance_fct_revenue",
        "select * from marts.finance_fct_revenue_monthly order by revenue_month",
    ),
    "product": (
        "select * from marts.marketing_fct_sessions",
        "select device, sum(converted_sessions) as converted "
        "from marts.product_fct_conversion_funnel "
        "where activity_date >= date '{d14}' group by 1",
        "select category, count(*) as tickets from marts.product_fct_support_tickets "
        "where created_at >= timestamp '{d7} 00:00:00' group by 1",
    ),
    "platform": (
        "select * from raw.orders where _loaded_at >= timestamp '{d1} 00:00:00'",
        "select count(*), max(_loaded_at) from raw.web_sessions",
        "select * from staging.stg_order_items where order_id = {n}",
    ),
}


@dataclass(frozen=True)
class Dashboard:
    exposure_id: str
    owner: str
    refresh_hours: int
    tables: tuple[str, ...]


@dataclass
class WorkloadSummary:
    days: int = 0
    real_builds: int = 0
    replayed_builds: int = 0
    queries: dict[str, int] = field(default_factory=dict)
    rows_loaded: int = 0
    rows_changed: int = 0


def dashboards_from(nodes: dict[str, Node], settings: Settings) -> list[Dashboard]:
    """One dashboard per exposure, refreshing on the schedule for its maturity."""
    refresh = settings.workload.dashboard_refresh_hours
    boards = []
    for node in sorted(nodes.values(), key=lambda n: n.unique_id):
        if node.resource_type != "exposure":
            continue
        tables = tuple(sorted(nodes[d].relation for d in node.depends_on if d in nodes))
        boards.append(
            Dashboard(
                exposure_id=node.unique_id,
                owner=node.owner_name or "unknown",
                refresh_hours=refresh.get(node.maturity or "low", refresh.get("low", 24)),
                tables=tables,
            )
        )
    return boards


def dashboard_sql(table: str, catalog: dict[str, dict[str, str]], day: datetime) -> str:
    """What a BI tool sends for one tile: the last 90 days of a table, or all of it."""
    columns = catalog.get(table, {})
    date_column = next((c for c, t in columns.items() if t.upper().startswith(_DATE_TYPES)), None)
    if date_column is None:
        return f"select * from {table}"
    since = (day - timedelta(days=90)).date().isoformat()
    literal = (
        f"date '{since}'"
        if columns[date_column].upper() == "DATE"
        else f"timestamp '{since} 00:00:00'"
    )
    return f"select * from {table} where {date_column} >= {literal}"


def adhoc_sql(template: str, day: datetime, n: int) -> str:
    dates = {f"d{k}": (day - timedelta(days=k)).date().isoformat() for k in (1, 7, 14, 30)}
    return template.format(n=n, **dates)


def refresh_hours_for(board: Dashboard, seed: int) -> list[int]:
    """Refresh times after the 02:00 dbt run, staggered per dashboard."""
    every = board.refresh_hours
    first = 3 + stable_hash(seed, "refresh", board.exposure_id) % min(every, 21)
    return list(range(first, 24, every))


class Workload:
    """Runs the simulation against the warehouse described by ``settings``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.plan = raw_data.RawDataPlan.from_settings(settings)
        self.clock = SimulatedClock(settings.simulation.start)
        self.logger = get_logger("workload")
        self.db_path = settings.resolve(settings.paths.duckdb)
        registry = Registry.from_config_dir(settings.root / "config")
        self.user_team = {user: registry.team_of(user) for user in settings.workload.adhoc_users}
        unknown = [u for u, team in self.user_team.items() if team is None]
        if unknown:
            raise ValueError(f"ad hoc users not in config/teams.yaml: {unknown}")
        self.log = QueryLog(catalog={})
        self.summary = WorkloadSummary()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._raw_sizes: dict[str, tuple[int, dict[str, tuple[str, int]]]] = {}
        self._growth: dict[str, tuple[datetime, int]] = {}
        self._last_run: DbtRun | None = None
        self._nodes: dict[str, Node] = {}
        self._catalog: dict[str, dict[str, str]] = {}

    # -- connection handling ------------------------------------------------------

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The warehouse connection, with one batch transaction open on it.

        Every write between two real dbt builds (a simulated week of loads,
        snapshots and query logs) shares that transaction, so it costs one
        commit instead of a couple of thousand. Commits are what is slow: each
        one forces DuckDB's write-ahead log to disk.
        """
        if self._connection is None:
            self._connection = connect(path=self.db_path)
            begin(self._connection)
        return self._connection

    def _close(self) -> None:
        """Flush, commit the batch, and let go of the file (dbt needs it)."""
        if self._connection is not None:
            self.log.flush(self._connection)
            if in_transaction(self._connection):
                commit(self._connection)
            self._connection.close()
            self._connection = None

    # -- the simulation -----------------------------------------------------------

    def run(self) -> WorkloadSummary:
        start = self.settings.simulation.start
        days = self.settings.simulation.weeks * 7
        seeded = raw_data.seed(self.connection, self.settings)
        self.summary.rows_loaded += sum(seeded.values())
        reset_collection_tables(self.connection)
        loaded_until = start
        for day_index in range(days):
            day = start + timedelta(days=day_index)
            run_at = day + timedelta(hours=self.settings.simulation.daily_run_hour)
            self._arrive(day_index, loaded_until, run_at)
            loaded_until = run_at
            self._observe_sources(run_at)
            self._dbt_run(day_index, day, run_at)
            self._daytime(day_index, day)
            self.log.flush(self.connection)
            self.summary.days += 1
        self._close()
        return self.summary

    def _arrive(self, day_index: int, after: datetime, until: datetime) -> None:
        self.clock.advance_to(until - timedelta(hours=1))
        loaded = raw_data.load_window(self.connection, self.plan, after=after, until=until)
        scale = self.settings.scale_factor
        workload = self.settings.workload
        changed = raw_data.apply_daily_changes(
            self.connection,
            self.plan,
            day_index,
            at=self.clock.now(),
            price_changes=max(1, round(workload.product_price_changes_per_day * scale)),
            profile_changes=max(1, round(workload.customer_profile_changes_per_day * scale)),
        )
        self.summary.rows_loaded += sum(loaded.values())
        self.summary.rows_changed += sum(changed.values())

    def _observe_sources(self, at: datetime) -> None:
        measured = []
        for table in raw_data.TABLES:
            name = f"raw.{table}"
            previous = self._growth.get(table)
            _, fingerprint, _ = growth.observe(
                self.connection, table, raw_data.PRIMARY_KEYS[table], at, previous
            )
            self._growth[table] = (at, fingerprint)
            base = self._raw_sizes.get(name)
            if base is None or table in raw_data.MUTABLE_TABLES:
                current = sizes.measure(self.connection, name)
            else:
                since = previous[0] if previous else at
                where = f"_loaded_at > TIMESTAMP '{since:%Y-%m-%d %H:%M:%S}'"
                current = sizes.add(base, sizes.measure(self.connection, name, where))
            self._raw_sizes[name] = current
            measured.append((name, current[0], current[1]))
        sizes.record_snapshot(self.connection, at, measured)

    def _dbt_run(self, day_index: int, day: datetime, run_at: datetime) -> None:
        self.clock.advance_to(run_at)
        real = day_index % self.settings.workload.real_build_every_days == 0
        if real:
            self._real_build(run_at)
            self.summary.real_builds += 1
        else:
            self.summary.replayed_builds += 1
        assert self._last_run is not None, "the first simulated day always builds for real"
        run_id = f"dbt:{day:%Y-%m-%d}"
        for record in dbt_run_records(self._last_run, run_id, run_at, replayed=not real):
            self.log.add(record)
        self._count("dbt", len(self._last_run.nodes))

    def _real_build(self, run_at: datetime) -> None:
        self._close()  # dbt opens the same DuckDB file
        invocation = invocation_from_settings(self.settings)
        run_dbt(invocation, ["build", "--quiet"])
        self._nodes = load_manifest(invocation.manifest_path)
        self._last_run = read_dbt_run(invocation.target_path, self._nodes)
        persist_edges(self.connection, build_graph(self._nodes))
        self._catalog = catalog_from(self.connection)
        self.log.set_catalog(self._catalog)
        models = [t for t in sorted(self._catalog) if t.split(".", 1)[0] in MODEL_SCHEMAS]
        measured = [(t, *sizes.measure(self.connection, t)) for t in models]
        sizes.record_snapshot(self.connection, run_at, measured)
        log_event(self.logger, f"real dbt build at {run_at:%Y-%m-%d %H:%M}", clock=self.clock)

    def _daytime(self, day_index: int, day: datetime) -> None:
        """Dashboard refreshes and ad hoc queries, run in simulated-time order."""
        seed = self.settings.seed
        # (time, query id, run id, actor, actor type, node id, sql)
        events: list[tuple[datetime, str, str, str, ActorType, str | None, str]] = []
        for board in dashboards_from(self._nodes, self.settings):
            for hour in refresh_hours_for(board, seed):
                at = day + timedelta(hours=hour)
                run_id = f"dash:{board.exposure_id}:{at:%Y-%m-%dT%H}"
                for index, table in enumerate(board.tables):
                    sql = dashboard_sql(table, self._catalog, day)
                    query_id = f"{run_id}:{index:02d}"
                    events.append(
                        (at, query_id, run_id, board.owner, "dashboard", board.exposure_id, sql)
                    )
        maximum = self.settings.workload.adhoc_max_queries_per_day
        for user in sorted(self.user_team):
            team = self.user_team[user]
            assert team is not None
            templates = ADHOC_TEMPLATES[team]
            count = stable_hash(seed, "adhoc_count", user, day_index) % (maximum + 1)
            for k in range(count):
                pick = stable_hash(seed, "adhoc_pick", user, day_index, k)
                minute = 9 * 60 + pick % (9 * 60)
                at = day + timedelta(minutes=minute)
                n = 1 + pick % max(1, self.plan.counts["orders"])
                sql = adhoc_sql(templates[pick % len(templates)], day, n)
                query_id = f"adhoc:{user}:{day:%Y-%m-%d}:{k}"
                events.append((at, query_id, query_id, user, "adhoc", None, sql))

        logged = LoggedConnection(
            self.connection, self.log, self.clock, self.settings.workload.adhoc_result_page_rows
        )
        for at, query_id, run_id, actor, actor_type, node_id, sql in sorted(
            events, key=lambda e: (e[0], e[1])
        ):
            self.clock.advance_to(max(at, self.clock.now()))
            logged.execute(
                sql,
                query_id=query_id,
                run_id=run_id,
                actor=actor,
                actor_type=actor_type,
                node_id=node_id,
            )
            self._count(actor_type, 1)

    def _count(self, kind: str, n: int) -> None:
        self.summary.queries[kind] = self.summary.queries.get(kind, 0) + n


def run_workload(settings: Settings) -> WorkloadSummary:
    return Workload(settings).run()
