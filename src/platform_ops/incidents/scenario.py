"""The multi-week incident scenario behind ``platform-ops incidents run``.

A fresh seed, a green baseline build, then one simulated day at a time:

1. Whatever happened since the last 02:00 run, in time order: faults injected
   at ``fault_hour`` on their day, and incidents resolved by their owners. A
   resolution repairs every active fault upstream of the incident's root.
2. The day's raw data arrives, except for sources that have gone stale.
3. The 02:00 run. dbt runs for real only when a fault is active, and only on
   the part of the graph that faulted sources reach (``dbt run`` then
   ``dbt test``, plus ``dbt source freshness`` for stale sources). A green day
   would find nothing, so nothing runs.
4. The results are ingested, grouped, scored, routed and notified.

``dbt run`` and ``dbt test`` are separate on purpose. ``dbt build`` skips
everything below a failed test, which hides the very alert storm grouping is
there to tame (ADR 0008).

The selection is ``@source:raw.<table>``: everything downstream of the faulted
source plus every ancestor of those models. Downstream alone is not enough.
Every model is a table, so a model that is not rebuilt keeps the data of the
last run that built it, and a test comparing a freshly built model with a stale
parent fails for no reason. With ``@`` every model the run builds or tests has
freshly built inputs, which is what makes a targeted run find exactly what a
full run would; the end-to-end test checks it on a fault day.

The scenario never reads ``ops.fault_ground_truth``. Only the metrics that grade
it do.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import duckdb
import networkx as nx

from platform_ops.common.clock import SimulatedClock
from platform_ops.common.config import Settings, Severity, Tier
from platform_ops.common.db import begin, commit, connect, in_transaction
from platform_ops.common.dbt_invoke import (
    DbtError,
    DbtInvocation,
    invocation_from_settings,
    run_dbt,
)
from platform_ops.common.logging import get_logger, log_event
from platform_ops.incidents import ingest
from platform_ops.incidents.grouping import Group, assign, group_failures
from platform_ops.incidents.ingest import CheckEvent, RunOutcome
from platform_ops.incidents.lifecycle import respond
from platform_ops.incidents.notify import (
    LocalNotifier,
    Notification,
    NotificationKind,
    Notifier,
    build_notifier,
)
from platform_ops.incidents.routing import naive_route, route
from platform_ops.incidents.severity import dataset_of, score_incident
from platform_ops.metadata.lineage import Consumer, build_graph, upstream
from platform_ops.metadata.manifest import Node, load_manifest
from platform_ops.metadata.registry import Registry
from platform_ops.simulation import faults, raw_data
from platform_ops.simulation.faults import CATALOGUE, Fault


def _checks(n: int) -> str:
    return f"{n} check" if n == 1 else f"{n} checks"


class ScenarioError(RuntimeError):
    """The scenario could not run as designed."""


@dataclass
class Incident:
    incident_id: str
    root: str
    dataset: str
    severity: Severity
    score: int
    root_tier: Tier
    owner: str
    team: str
    naive_owner: str
    naive_team: str
    routed_via: str
    opened_at: datetime
    acknowledged_at: datetime
    resolved_at: datetime
    consumers: tuple[Consumer, ...]
    previous_incident_id: str | None
    run_ids: list[str] = field(default_factory=list)
    last_seen_at: datetime | None = None
    checks: int = 0
    failed_nodes: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    check_ids: set[str] = field(default_factory=set)

    def is_open(self, at: datetime) -> bool:
        return self.opened_at <= at < self.resolved_at

    def add(self, run_id: str, at: datetime, group: Group) -> None:
        self.run_ids.append(run_id)
        self.last_seen_at = at
        self.checks += len(group.events)
        self.failed_nodes.update(group.failed_nodes)
        self.skipped.update(group.skipped)
        self.check_ids.update(event.check_id for event in group.events)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    run_at: datetime
    selected_sources: tuple[str, ...]
    checks_run: int
    failures: int
    opened: int
    appended: int


@dataclass
class ScenarioResult:
    incidents: list[Incident] = field(default_factory=list)
    # (event, incident id) for every failing check.
    events: list[tuple[CheckEvent, str]] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    # Day index to (targeted failing checks, full-run failing checks).
    equivalence: dict[int, tuple[frozenset[str], frozenset[str]]] = field(default_factory=dict)
    dbt_invocations: int = 0
    end: datetime | None = None


@dataclass
class _ActiveFault:
    fault: Fault
    injected_at: datetime
    stalled_since: datetime | None


class Scenario:
    """Runs the incident scenario against the warehouse described by ``settings``."""

    def __init__(
        self,
        settings: Settings,
        *,
        catalogue: Sequence[Fault] = CATALOGUE,
        full_run_on_days: Sequence[int] = (),
        allow_slack: bool = True,
    ) -> None:
        self.settings = settings
        self.catalogue = tuple(catalogue)
        self.full_run_on_days = frozenset(full_run_on_days)
        self.plan = raw_data.RawDataPlan.from_settings(settings)
        self.logger = get_logger("incidents")
        self.clock = SimulatedClock(settings.simulation.start)
        self.db_path = settings.resolve(settings.paths.duckdb)
        self.registry = Registry.from_config_dir(settings.root / "config")
        self.local = LocalNotifier()
        self.notifier: Notifier = (
            build_notifier(self.local, settings.root / ".env") if allow_slack else self.local
        )
        self.invocation = invocation_from_settings(settings)
        self.result = ScenarioResult()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._active: dict[str, _ActiveFault] = {}
        self._manifest: Any = None
        self._resolved_handled: set[str] = set()
        self.nodes: dict[str, Node] = {}
        self.graph: nx.DiGraph[str] = nx.DiGraph()
        self.tier_of: dict[str, Tier] = {}
        self.source_of: dict[str, str] = {}
        self._check_schedule()

    # -- setup --------------------------------------------------------------------

    def _check_schedule(self) -> None:
        days = self.settings.incidents.scenario_days
        if days > self.settings.simulation.weeks * 7:
            raise ScenarioError(
                f"incidents.scenario_days ({days}) is longer than the generated window "
                f"({self.settings.simulation.weeks} weeks)"
            )
        late = [f.fault_id for f in self.catalogue if not 0 <= f.day < days - 1]
        if late:
            raise ScenarioError(f"faults scheduled outside the scenario: {late}")

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The warehouse connection, with one transaction open between dbt runs."""
        if self._connection is None:
            self._connection = connect(path=self.db_path)
            begin(self._connection)
        return self._connection

    def _close(self) -> None:
        if self._connection is not None:
            if in_transaction(self._connection):
                commit(self._connection)
            self._connection.close()
            self._connection = None

    def _dbt(self, invocation: DbtInvocation, command: list[str]) -> None:
        self._close()
        self.result.dbt_invocations += 1
        manifest = self._manifest if invocation is self.invocation else None
        # Failing tests are the point here, so dbt's console output would only be
        # a wall of red. Its log file under the target's logs/ keeps the detail.
        quiet = ["--log-level", "none", "--log-level-file", "info"]
        outcome: Any = run_dbt(invocation, [*command, *quiet], check=False, manifest=manifest)
        if outcome.exception is not None:
            raise DbtError(f"dbt {' '.join(command)} crashed: {outcome.exception}")

    def _baseline(self) -> None:
        start = self.settings.simulation.start
        raw_data.seed(self.connection, self.settings)
        faults.reset_ground_truth(self.connection)
        self._close()
        self.result.dbt_invocations += 1
        run_dbt(self.invocation, ["build", "--quiet"])
        # Parsed once and reused by every later invocation (ADR 0008).
        self._manifest = run_dbt(self.invocation, ["parse", "--quiet"]).result
        self.nodes = load_manifest(self.invocation.manifest_path)
        self.graph = build_graph(self.nodes)
        for unique_id in self.nodes:
            resolution = self.registry.resolve(unique_id)
            if resolution.rule is not None:
                self.tier_of[unique_id] = resolution.rule.tier
        self.source_of = {
            node.name: unique_id
            for unique_id, node in self.nodes.items()
            if node.resource_type == "source"
        }
        missing = sorted({f.table for f in self.catalogue} - set(self.source_of))
        if missing:
            raise ScenarioError(f"faults target tables dbt does not declare: {missing}")
        log_event(self.logger, f"baseline build green at {start:%Y-%m-%d %H:%M}")

    # -- the scenario -------------------------------------------------------------

    def run(self) -> ScenarioResult:
        settings = self.settings
        start = settings.simulation.start
        run_hour = settings.simulation.daily_run_hour
        self._baseline()
        previous = start
        for day in range(settings.incidents.scenario_days):
            run_at = start + timedelta(days=day, hours=run_hour)
            self._between_runs(previous, run_at)
            tables = [t for t in raw_data.TABLES if not self._stalled(t)]
            raw_data.load_window(self.connection, self.plan, previous, run_at, tables=tables)
            previous = run_at
            self._scheduled_run(day, run_at)
        # Owners finish what is still open after the last run; no more runs follow.
        last = max((i.resolved_at for i in self.result.incidents), default=previous)
        self._between_runs(previous, max(last, previous), loaded_until=previous)
        self._close()
        self.result.end = max(last, previous)
        return self.result

    def _stalled(self, table: str) -> bool:
        active = self._active.get(table)
        return active is not None and active.stalled_since is not None

    def _between_runs(
        self, after: datetime, until: datetime, *, loaded_until: datetime | None = None
    ) -> None:
        """Injections and resolutions in ``(after, until]``, in time order."""
        loaded = loaded_until or after
        start = self.settings.simulation.start
        hour = self.settings.incidents.fault_hour
        happenings: list[tuple[datetime, int, str, Fault | Incident]] = []
        for fault in self.catalogue:
            at = start + timedelta(days=fault.day, hours=hour)
            if after < at <= until:
                happenings.append((at, 1, fault.fault_id, fault))
        for incident in self.result.incidents:
            if incident.incident_id in self._resolved_handled:
                continue
            if after < incident.resolved_at <= until:
                happenings.append((incident.resolved_at, 0, incident.incident_id, incident))
        for at, _, _, item in sorted(happenings, key=lambda h: h[:3]):
            if isinstance(item, Fault):
                self._inject(item, at, loaded)
            else:
                self._resolve(item, at, loaded)

    def _inject(self, fault: Fault, at: datetime, loaded_until: datetime) -> None:
        if fault.table in self._active:
            raise ScenarioError(
                f"{fault.fault_id} targets raw.{fault.table}, which already has an active fault"
            )
        rows = faults.inject(self.connection, fault, at, self.settings.seed)
        stalled = loaded_until if fault.fault_type == "stale_source" else None
        self._active[fault.table] = _ActiveFault(fault, at, stalled)
        self.clock.advance_to(at)
        log_event(self.logger, f"injected {fault.fault_id} ({fault.fault_type}) into "
                  f"{fault.target}, {rows:,} rows", clock=self.clock)  # fmt: skip

    def _resolve(self, incident: Incident, at: datetime, loaded_until: datetime) -> None:
        self.clock.advance_to(at)
        self._resolved_handled.add(incident.incident_id)
        reach = upstream(self.graph, incident.root) | {incident.root}
        repaired = []
        for table, active in sorted(self._active.items()):
            if self.source_of[table] in reach:
                faults.repair(self.connection, self.plan, active.fault, at, loaded_until,
                              active.stalled_since)  # fmt: skip
                repaired.append(table)
        for table in repaired:
            del self._active[table]
        self._notify(incident, "resolved", at, f"resolved; repaired {repaired or 'nothing'}")

    # -- a scheduled run ----------------------------------------------------------

    def _selectors(self, tables: Sequence[str]) -> list[str]:
        return [f"@source:{self._source_selector(t)}" for t in tables]

    def _source_selector(self, table: str) -> str:
        node = self.nodes[self.source_of[table]]
        # source unique ids are source.<package>.<source name>.<table>
        return ".".join(node.unique_id.split(".")[2:4])

    def _run_and_test(self, run_id: str, run_at: datetime, selectors: list[str]) -> RunOutcome:
        target = self.invocation.target_path
        select = ["--select", *selectors] if selectors else []
        self._dbt(self.invocation, ["run", *select])
        ran = ingest.parse_run_results(ingest.read_json(target / "run_results.json"), self.nodes,
                                       run_id=run_id, detected_at=run_at)  # fmt: skip
        self._dbt(self.invocation, ["test", *select])
        tested = ingest.parse_run_results(
            ingest.read_json(target / "run_results.json"), self.nodes,
            run_id=run_id, detected_at=run_at,
        )  # fmt: skip
        return ingest.merge([ran, tested])

    def _freshness(self, run_id: str, run_at: datetime, tables: Sequence[str]) -> RunOutcome:
        # Only freshness reads `simulated_now`. dbt resolves vars when it runs a
        # node, not when it parses, so the saved parse still applies: 0.6 s
        # instead of 5.2 s for a full re-parse on the development laptop.
        now = invocation_from_settings(self.settings, simulated_now=run_at).vars
        invocation = dataclasses.replace(self.invocation, vars=now)
        selectors = [f"source:{self._source_selector(t)}" for t in tables]
        self._dbt(invocation, ["source", "freshness", "--select", *selectors])
        data = ingest.read_json(invocation.target_path / "sources.json")
        return ingest.parse_freshness(data, run_id=run_id, detected_at=run_at)

    def _scheduled_run(self, day: int, run_at: datetime) -> None:
        self.clock.advance_to(run_at)
        tables = sorted(self._active)
        if not tables:
            return
        run_id = f"run:{run_at:%Y-%m-%d}"
        outcome = self._run_and_test(run_id, run_at, self._selectors(tables))
        stale = [t for t in tables if self._stalled(t)]
        if stale:
            outcome = ingest.merge([outcome, self._freshness(run_id, run_at, stale)])
        if day in self.full_run_on_days:
            full = self._run_and_test(f"{run_id}:full", run_at, [])
            self.result.equivalence[day] = (
                frozenset(e.check_id for e in outcome.events if e.check_type != "freshness"),
                frozenset(e.check_id for e in full.events),
            )
        opened, appended = self._handle(run_id, run_at, outcome)
        self.result.runs.append(
            RunRecord(run_id, run_at, tuple(tables), outcome.checks_run, len(outcome.events),
                      opened, appended)
        )  # fmt: skip
        log_event(self.logger, f"{run_id}: {len(tables)} sources, {outcome.checks_run} checks, "
                  f"{len(outcome.events)} failing, {opened} opened, {appended} appended",
                  clock=self.clock)  # fmt: skip

    def _handle(self, run_id: str, run_at: datetime, outcome: RunOutcome) -> tuple[int, int]:
        groups = group_failures(self.graph, outcome.events, outcome.skipped)
        open_by_root = {i.root: i.incident_id for i in self.result.incidents if i.is_open(run_at)}
        assignment = assign(groups, open_by_root)
        by_id = {i.incident_id: i for i in self.result.incidents}
        incident_of: dict[str, str] = {}
        for incident_id, group in assignment.appended:
            incident = by_id[incident_id]
            incident.add(run_id, run_at, group)
            incident_of.update({node: incident_id for node in group.failed_nodes})
            self._notify(incident, "update", run_at,
                         f"still failing: {_checks(len(group.events))} in {run_id}")  # fmt: skip
        for group in assignment.new:
            incident = self._open(group, run_at)
            incident.add(run_id, run_at, group)
            incident_of.update({node: incident.incident_id for node in group.failed_nodes})
            text = f"{_checks(len(group.events))} failing, rooted at {incident.root}"
            self._notify(incident, "page", run_at, text)
        self.result.events.extend((e, incident_of[e.subject]) for e in outcome.events)
        return len(assignment.new), len(assignment.appended)

    def _open(self, group: Group, run_at: datetime) -> Incident:
        incident_id = f"INC-{len(self.result.incidents) + 1:03d}"
        scored = score_incident(self.graph, group.root, self.nodes, self.tier_of, self.settings)
        page = route(group.root, self.nodes, self.registry)
        naive = naive_route(group.root, self.nodes, self.registry)
        busy = sum(1 for i in self.result.incidents if i.owner == page.owner and i.is_open(run_at))
        response = respond(incident_id, scored.severity, run_at, busy,
                           self.settings.incidents.lifecycle, self.settings.seed)  # fmt: skip
        previous = [i.incident_id for i in self.result.incidents if i.root == group.root]
        incident = Incident(
            incident_id=incident_id,
            root=group.root,
            dataset=dataset_of(group.root, self.nodes),
            severity=scored.severity,
            score=scored.score,
            root_tier=scored.root_tier,
            owner=page.owner,
            team=page.team,
            naive_owner=naive.owner,
            naive_team=naive.team,
            routed_via=page.routed_via,
            opened_at=run_at,
            acknowledged_at=response.acknowledged_at,
            resolved_at=response.resolved_at,
            consumers=scored.consumers,
            previous_incident_id=previous[-1] if previous else None,
        )
        self.result.incidents.append(incident)
        return incident

    def _notify(self, incident: Incident, kind: NotificationKind, at: datetime, text: str) -> None:
        notification = Notification(incident.incident_id, kind, incident.owner, incident.team,
                                    incident.severity, text, at)  # fmt: skip
        self.result.notifications.append(notification)
        self.notifier.send(notification)
