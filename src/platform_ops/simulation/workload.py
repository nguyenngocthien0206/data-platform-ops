"""The company's query workload on ClickHouse, day by day (version 2.0, Phase 13).

Besides dbt and the ingestion, two kinds of people use the warehouse, and each
one runs as its own ClickHouse user (ADR 0013), so the query log records who
did what:

- **Metabase** refreshes every dashboard on its own schedule (12 hours for a
  ``high`` maturity exposure, 24 otherwise, staggered after the 02:00 run),
  running exactly the cards ``bi.py`` generates, as the ``metabase`` user;
- **a few people** run ad hoc SQL during office hours, from careful to
  careless, as their team's user;
- **everyone opens dashboards** on working days, their own team's often and
  other teams' now and then (``views``). A view is served from the latest
  refresh, as Metabase's cache does, so it runs no query: the views decide who
  pays for the refreshes, not what they cost. They are recorded the way
  Metabase's own view log records them: when, which dashboard, who.

Every query is tagged with its simulated time (``common.tagging``), run to
completion, and its rows discarded (``FORMAT Null``): what a query costs is
what it reads, not what a client fetches. Queries run on one thread, so a
query with a ``LIMIT`` stops after reading the same rows every time and the
measured reads repeat across runs. Everything that looks random comes from the
seed through a stable hash, so two runs send the same queries.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.hashing import stable_hash
from platform_ops.common.servers import clickhouse_client
from platform_ops.common.tagging import Tag
from platform_ops.metadata.manifest import load_manifest
from platform_ops.metadata.registry import Registry
from platform_ops.simulation import bi

# What people actually type, per team, from careful to careless. Placeholders:
# {marts}, {staging}, {raw} are the namespaced databases; {d1}, {d7}, {d14},
# {d30} are dates that many days back; {n} is a number. The business teams
# read the marts (ADR 0013), the platform team every layer. Nobody queries the
# abandoned models, which is exactly why they are waste.
ADHOC_TEMPLATES: dict[str, tuple[str, ...]] = {
    "sales": (
        "SELECT * FROM {marts}.sales_fct_order_items",
        "SELECT channel, count() AS orders, sum(net_amount) AS revenue "
        "FROM {marts}.sales_fct_orders WHERE order_date >= toDate('{d30}') GROUP BY channel",
        "SELECT period_start, orders, net_revenue FROM {marts}.sales_rpt_orders_weekly_by_channel "
        "WHERE channel = 'web' ORDER BY period_start",
        "SELECT c.value_segment, count() AS orders FROM {marts}.sales_fct_orders AS o "
        "JOIN {marts}.sales_dim_customers AS c ON c.customer_id = o.customer_id "
        "WHERE o.order_date >= toDate('{d7}') GROUP BY c.value_segment",
    ),
    "marketing": (
        "SELECT * FROM {marts}.marketing_fct_sessions WHERE session_date >= toDate('{d30}')",
        "SELECT traffic_source, count() AS sessions FROM {marts}.marketing_fct_sessions "
        "WHERE session_date >= toDate('{d14}') GROUP BY traffic_source",
        "SELECT campaign_name, spend, attributed_revenue, return_on_ad_spend "
        "FROM {marts}.marketing_fct_campaign_performance ORDER BY return_on_ad_spend DESC",
    ),
    "finance": (
        "SELECT payment_status, count() AS orders, sum(outstanding_amount) AS outstanding "
        "FROM {marts}.finance_fct_payments_reconciliation "
        "WHERE order_date >= toDate('{d30}') GROUP BY payment_status",
        "SELECT * FROM {marts}.finance_fct_revenue",
        "SELECT * FROM {marts}.finance_fct_revenue_monthly ORDER BY revenue_month",
    ),
    "product": (
        "SELECT * FROM {marts}.marketing_fct_sessions",
        "SELECT device, sum(converted_sessions) AS converted "
        "FROM {marts}.product_fct_conversion_funnel "
        "WHERE activity_date >= toDate('{d14}') GROUP BY device",
        "SELECT category, count() AS tickets FROM {marts}.product_fct_support_tickets "
        "WHERE created_at >= toDateTime64('{d7} 00:00:00', 6) GROUP BY category",
    ),
    "platform": (
        "SELECT * FROM {raw}.orders FINAL WHERE _loaded_at >= toDateTime64('{d1} 00:00:00', 6)",
        "SELECT count(), max(_loaded_at) FROM {raw}.web_sessions FINAL",
        "SELECT * FROM {staging}.stg_order_items WHERE order_id = {n}",
    ),
}

# One thread per query, so a LIMIT reads the same rows on every run.
_QUERY_SETTINGS = {"max_threads": 1}


def refresh_hours(exposure: str, every: int, seed: int) -> list[int]:
    """Refresh times after the 02:00 dbt run, staggered per dashboard."""
    first = 3 + stable_hash(seed, "refresh", exposure) % min(every, 21)
    return list(range(first, 24, every))


def adhoc_sql(template: str, day: datetime, n: int, databases: dict[str, str]) -> str:
    dates = {f"d{k}": (day - timedelta(days=k)).date().isoformat() for k in (1, 7, 14, 30)}
    return template.format(n=n, **dates, **databases)


@dataclass
class WorkloadSummary:
    days: int = 0
    queries: Counter[str] = field(default_factory=Counter)


@dataclass
class Workload:
    """Sends one day's dashboard refreshes and ad hoc queries at a time."""

    settings: Settings
    summary: WorkloadSummary = field(default_factory=WorkloadSummary)
    _boards: list[bi.Dashboard] = field(default_factory=list)
    _maturity: dict[str, str] = field(default_factory=dict)
    _clients: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        registry = Registry.from_config_dir(self.settings.root / "config")
        self.member_team = {
            person: team.id for team in registry.teams.values() for person in team.members
        }
        self.person_team = {
            person: team.id
            for team in registry.teams.values()
            for person in team.members
            if person in self.settings.workload.adhoc_users
        }
        company = self.settings.company
        self.databases = {
            "marts": company.name("marts"),
            "staging": company.name("staging"),
            "raw": company.name(company.raw_database),
        }

    def _client(self, user: str) -> Any:
        if user not in self._clients:
            self._clients[user] = clickhouse_client(self.settings, user)
        return self._clients[user]

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def _dashboards(self) -> list[bi.Dashboard]:
        """The exposures' dashboards, as Metabase shows them, once the marts exist."""
        if not self._boards:
            manifest = invocation_from_settings(self.settings).manifest_path
            registry = Registry.from_config_dir(self.settings.root / "config")
            shapes = bi.model_shapes(
                self._client("metabase"),
                self.settings.company.name("marts"),
                bi.exposed_models(manifest),
            )
            self._boards = bi.dashboards(manifest, registry, shapes)
            self._maturity = {
                n.unique_id: n.maturity or "low"
                for n in load_manifest(manifest).values()
                if n.resource_type == "exposure"
            }
        return self._boards

    def _run(self, user: str, sql: str, tag: Tag) -> None:
        self._client(user).command(
            f"{sql} FORMAT Null", settings={**_QUERY_SETTINGS, **tag.settings()}
        )
        self.summary.queries[tag.actor] += 1

    def views(self, day: datetime) -> list[tuple[datetime, str, str]]:
        """Who opened which dashboard that day, and when: (viewed_at, exposure, person).

        Working days only, office hours, at most one view per person and
        dashboard a day. The chance is the team's (own or other) times the
        dashboard's popularity, from the seed: none, half, normal or one and a
        half, so a few dashboards are opened by nobody.
        """
        if day.weekday() >= 5:
            return []
        seed = self.settings.seed
        chance = self.settings.workload.dashboard_views
        found: list[tuple[datetime, str, str]] = []
        for board in self._dashboards():
            popularity = stable_hash(seed, "popularity", board.exposure) % 4
            for person in sorted(self.member_team):
                own = self.member_team[person] == board.team
                percent = chance.own_team_percent if own else chance.other_team_percent
                draw = stable_hash(seed, "view", board.exposure, person, day.date().isoformat())
                # Popularity counts in halves: compare against twice the percent.
                if draw % 200 < percent * popularity:
                    minute = 9 * 60 + stable_hash(seed, "view_at", draw) % (9 * 60)
                    found.append((day + timedelta(minutes=minute), board.exposure, person))
        return sorted(found)

    def run_day(self, day_index: int, day: datetime) -> None:
        """One simulated day: dashboard refreshes, then ad hoc SQL, in time order."""
        seed = self.settings.seed
        refresh = self.settings.workload.dashboard_refresh_hours
        events: list[tuple[datetime, str, str, Tag]] = []
        for board in self._dashboards():
            maturity = self._maturity.get(board.exposure, "low")
            every = refresh.get(maturity, refresh.get("low", 24))
            for hour in refresh_hours(board.exposure, every, seed):
                at = day + timedelta(hours=hour)
                for card in board.cards:
                    tag = Tag(at, "dashboard", subject=board.exposure)
                    events.append((at, board.exposure, card.sql, tag))
        maximum = self.settings.workload.adhoc_max_queries_per_day
        for person in sorted(self.person_team):
            team = self.person_team[person]
            templates = ADHOC_TEMPLATES[team]
            count = stable_hash(seed, "adhoc_count", person, day_index) % (maximum + 1)
            for k in range(count):
                pick = stable_hash(seed, "adhoc_pick", person, day_index, k)
                at = day + timedelta(minutes=9 * 60 + pick % (9 * 60))
                sql = adhoc_sql(
                    templates[pick % len(templates)], day, 1 + pick % 1000, self.databases
                )
                events.append((at, team, sql, Tag(at, "adhoc", person=person)))
        for _, owner, sql, tag in sorted(events, key=lambda e: (e[0], e[1], e[2])):
            user = "metabase" if tag.actor == "dashboard" else owner
            self._run(user, sql, tag)
        self.summary.days += 1
