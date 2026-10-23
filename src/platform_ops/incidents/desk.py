"""The incident desk: dbt's artifacts in, incidents out, one ingest at a time (Phase 14).

``platform-ops incidents ingest`` hands the desk one dbt target folder and the
run it belongs to: the orchestrator's run id, so the invocations of one
scheduled run (a production night's freshness, source tests, run, snapshot
and tests, or Cosmos's one invocation per model) form one run. Each ingest, in one
transaction holding the store's lock:

1. records the invocations it has not seen (by dbt's invocation id), with every
   node they reported on and every failing check;
2. regroups the whole run from everything it holds so far (ADR 0005: one
   incident per root, by lineage), so the order the invocations arrive in does
   not change the answer;
3. opens an incident for a new root, appends the run to the open incident of a
   root that fails again, of a root now below a new failure, or of a root below
   the new failure's root (a stale feed whose staging model already has an
   incident open is the same problem, seen from the source), or of a root above
   the new failure that this run did not check (a rerun after one fix rebuilds
   a model that also reads a source still broken by another), and merges an
   incident this run opened into another when a later invocation shows its root
   was not the root after all;
4. resolves an open incident the run did not fail on once the run shows its
   root healthy: the model ran, or the source is fresh again, and every test
   on it that ran passed (at least one must have, if it has tests). A run that
   did not cover the root resolves nothing;
5. queues one page per new incident, one update per run on an open one, one
   message per resolution.

Notifications go out after the commit, so a page is never sent for an incident
the store does not hold, and each is recorded with how it was delivered.
Ingesting the same artifacts again finds nothing new and changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import networkx as nx
import psycopg
from psycopg import sql

from platform_ops.common.config import Settings, Tier
from platform_ops.incidents import ingest, store
from platform_ops.incidents.grouping import Group, group_failures, roots_by_node
from platform_ops.incidents.ingest import CheckEvent, test_subject
from platform_ops.incidents.notify import Channel, Notification, deliver
from platform_ops.incidents.routing import naive_route, route
from platform_ops.incidents.severity import IncidentPolicy, dataset_of, score_incident
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import Node, load_manifest
from platform_ops.metadata.registry import Registry

HEALTHY_FRESHNESS = frozenset({"pass", "warn"})


@dataclass(frozen=True)
class Context:
    """What the desk needs to know about the project: lineage, owners, policy."""

    nodes: dict[str, Node]
    graph: nx.DiGraph[str]
    registry: Registry
    policy: IncidentPolicy
    tier_of: dict[str, Tier]
    tests_of: dict[str, frozenset[str]]

    @classmethod
    def load(
        cls, settings: Settings, manifest_path: Path, registry_dir: Path | None = None
    ) -> Context:
        nodes = load_manifest(manifest_path)
        folder = registry_dir or settings.root / "config"
        registry = Registry.from_config_dir(folder)
        tier_of: dict[str, Tier] = {}
        for unique_id in nodes:
            rule = registry.resolve(unique_id).rule
            if rule is not None:
                tier_of[unique_id] = rule.tier
        tests: dict[str, set[str]] = {}
        for unique_id, node in nodes.items():
            if node.resource_type != "test":
                continue
            # A model's tests by the node dbt attaches them to; a source's
            # tests (dbt attaches none) by the source they read.
            subject = node.attached_node or test_subject(node)
            if subject != unique_id:
                tests.setdefault(subject, set()).add(unique_id)
        return cls(
            nodes,
            build_graph(nodes),
            registry,
            IncidentPolicy.load(settings, folder),
            tier_of,
            {k: frozenset(v) for k, v in tests.items()},
        )


@dataclass(frozen=True)
class IngestResult:
    run_id: str
    new_invocations: int
    failing_checks: int
    opened: tuple[str, ...]
    appended: tuple[str, ...]
    resolved: tuple[str, ...]
    delivered: int


@dataclass
class _Incident:
    incident_id: str
    seq: int
    root: str
    owner: str
    opened_at: datetime
    opened_by_run: str
    resolved_at: datetime | None
    resolved_by_run: str | None
    merged_into: str | None


def ingest_target(
    settings: Settings,
    target: Path,
    run_id: str,
    *,
    at: datetime | None = None,
    context: Context | None = None,
    channels: Sequence[Channel] = (),
) -> IngestResult:
    """Ingest the artifacts in ``target`` as part of ``run_id`` (see the module docstring).

    ``at`` is when the run detected its failures; by default, when dbt wrote
    each file. ``context`` defaults to the manifest in ``target``.
    """
    context = context or Context.load(settings, target / "manifest.json")
    artifacts = ingest.read_target(target)
    with store.transaction(settings) as connection:
        known = {
            str(r[0])
            for r in connection.execute(
                sql.SQL("SELECT invocation_id FROM {}").format(
                    store.table(settings, "incident_invocations")
                )
            ).fetchall()
        }
        fresh = [a for a in artifacts if a.invocation_id not in known]
        if not fresh:
            return IngestResult(run_id, 0, 0, (), (), (), 0)
        detected = at or max(a.generated_at for a in fresh)
        for artifact in fresh:
            _record(connection, settings, context, artifact, run_id, at or artifact.generated_at)
        opened, appended, resolved, failing = _regroup(
            connection, settings, context, run_id, detected
        )
    delivered = _deliver(settings, context.registry, channels)
    return IngestResult(
        run_id, len(fresh), failing, tuple(opened), tuple(appended), tuple(resolved), delivered
    )


def _record(
    connection: psycopg.Connection[Any],
    settings: Settings,
    context: Context,
    artifact: ingest.Artifact,
    run_id: str,
    at: datetime,
) -> None:
    outcome = ingest.parse(artifact, context.nodes, run_id=run_id, detected_at=at)
    connection.execute(
        sql.SQL(
            "INSERT INTO {} (invocation_id, run_id, kind, detected_at, checks_run) "
            "VALUES (%s, %s, %s, %s, %s)"
        ).format(store.table(settings, "incident_invocations")),
        [artifact.invocation_id, run_id, artifact.kind, at, outcome.checks_run],
    )
    with connection.cursor().copy(
        sql.SQL(
            "COPY {} (invocation_id, run_id, unique_id, resource_type, status) FROM STDIN"
        ).format(store.table(settings, "incident_results"))
    ) as copy:
        for unique_id, kind, status in ingest.results(artifact, context.nodes):
            copy.write_row((artifact.invocation_id, run_id, unique_id, kind, status))
    for event in outcome.events:
        connection.execute(
            sql.SQL(
                "INSERT INTO {} (invocation_id, run_id, check_id, check_type, subject, status, "
                "message, detected_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id, check_id) DO NOTHING"
            ).format(store.table(settings, "incident_check_events")),
            [
                artifact.invocation_id,
                run_id,
                event.check_id,
                event.check_type,
                event.subject,
                event.status,
                event.message,
                event.detected_at,
            ],
        )


def _incidents(connection: psycopg.Connection[Any], settings: Settings) -> list[_Incident]:
    rows = connection.execute(
        sql.SQL(
            "SELECT incident_id, seq, root, owner, opened_at, opened_by_run, resolved_at, "
            "resolved_by_run, merged_into FROM {} ORDER BY seq"
        ).format(store.table(settings, "incidents"))
    ).fetchall()
    return [_Incident(*row) for row in rows]


def _healthy(
    root: str,
    context: Context,
    statuses: Mapping[str, str],
) -> bool:
    """Did this run cover ``root`` and find it healthy?"""
    dataset = dataset_of(root, context.nodes)
    if root.startswith("source."):
        # Fresh again, and its own tests (the volume test) pass when they ran.
        if statuses.get(root) not in HEALTHY_FRESHNESS:
            return False
    elif root.startswith("test."):
        return statuses.get(root) == "pass"
    elif statuses.get(dataset) != "success":
        return False
    tests = context.tests_of.get(dataset, frozenset())
    ran = [statuses[t] for t in tests if t in statuses]
    return (not tests or bool(ran)) and all(status == "pass" for status in ran)


def _covered(root: str, context: Context, statuses: Mapping[str, str]) -> bool:
    """Did this run check ``root`` at all, whatever it found?"""
    return root in statuses or dataset_of(root, context.nodes) in statuses


def _regroup(
    connection: psycopg.Connection[Any],
    settings: Settings,
    context: Context,
    run_id: str,
    at: datetime,
) -> tuple[list[str], list[str], list[str], int]:
    table = store.table

    events = [
        CheckEvent(run_id, check_id, check_type, subject, status, message, detected_at)
        for check_id, check_type, subject, status, message, detected_at in connection.execute(
            sql.SQL(
                "SELECT check_id, check_type, subject, status, message, detected_at FROM {} "
                "WHERE run_id = %s ORDER BY check_id"
            ).format(table(settings, "incident_check_events")),
            [run_id],
        ).fetchall()
    ]
    statuses: dict[str, str] = {}
    skipped: set[str] = set()
    for unique_id, status in connection.execute(
        sql.SQL(
            "SELECT unique_id, status FROM {} WHERE run_id = %s ORDER BY unique_id, status"
        ).format(table(settings, "incident_results")),
        [run_id],
    ).fetchall():
        if status == "skipped":
            skipped.add(unique_id)
        # A node reported twice in a run (a retry) counts by its worst status.
        elif statuses.get(unique_id) not in ("error", "fail", "runtime error"):
            statuses[unique_id] = status
    groups = group_failures(context.graph, events, sorted(skipped))
    root_of = roots_by_node(context.graph, {e.subject for e in events})

    incidents = _incidents(connection, settings)
    live = [i for i in incidents if i.merged_into is None]
    before = [
        i
        for i in live
        if i.opened_by_run != run_id
        and i.opened_at <= at
        and (i.resolved_at is None or i.resolved_by_run == run_id)
    ]
    mine = [i for i in live if i.opened_by_run == run_id]
    opened: list[str] = []
    appended: list[str] = []
    assignment: dict[str, str] = {}
    for group in sorted(groups, key=lambda g: g.root):
        incident = (
            next((i for i in before if i.root == group.root), None)
            or next((i for i in before if i.root in group.failed_nodes), None)
            or next((i for i in before if group.root in _above(context.graph, i.root)), None)
            or next(
                (
                    i
                    for i in before
                    if i.root in _above(context.graph, group.root)
                    and not _covered(i.root, context, statuses)
                ),
                None,
            )
            or next((i for i in mine if i.root == group.root), None)
        )
        if incident is None:
            incident = _open(connection, settings, context, group, run_id, at, incidents)
            incidents.append(incident)
            mine.append(incident)
            opened.append(incident.incident_id)
        elif incident in before:
            appended.append(incident.incident_id)
            if incident.resolved_by_run == run_id:
                # Healthy after one invocation of this run, failing after another.
                _set_resolution(connection, settings, incident, None, None)
        assignment[group.root] = incident.incident_id
    taken = set(assignment.values())
    for incident in mine:
        if incident.incident_id not in taken:
            # A later invocation found a failure above this root: one problem, not two.
            into = assignment[root_of.get(incident.root, incident.root)]
            connection.execute(
                sql.SQL("UPDATE {} SET merged_into = %s WHERE incident_id = %s").format(
                    table(settings, "incidents")
                ),
                [into, incident.incident_id],
            )
            incident.merged_into = into
    for event in events:
        connection.execute(
            sql.SQL("UPDATE {} SET incident_id = %s WHERE run_id = %s AND check_id = %s").format(
                table(settings, "incident_check_events")
            ),
            [assignment[root_of[event.subject]], run_id, event.check_id],
        )
    connection.execute(
        sql.SQL("DELETE FROM {} WHERE run_id = %s").format(table(settings, "incident_run_links")),
        [run_id],
    )
    checks: dict[str, int] = {}
    for event in events:
        incident_id = assignment[root_of[event.subject]]
        checks[incident_id] = checks.get(incident_id, 0) + 1
    for incident_id, n in sorted(checks.items()):
        connection.execute(
            sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s)").format(
                table(settings, "incident_run_links")
            ),
            [incident_id, run_id, at, n],
        )

    resolved: list[str] = []
    for incident in before:
        if incident.incident_id in taken:
            continue
        healthy = _healthy(incident.root, context, statuses)
        if healthy and incident.resolved_by_run != run_id:
            _set_resolution(connection, settings, incident, at, run_id)
            resolved.append(incident.incident_id)
        elif not healthy and incident.resolved_by_run == run_id:
            _set_resolution(connection, settings, incident, None, None)

    by_id = {i.incident_id: i for i in incidents}
    for incident_id in opened:
        n = checks.get(incident_id, 0)
        root = by_id[incident_id].root
        _queue(
            connection,
            settings,
            incident_id,
            "page",
            run_id,
            at,
            f"{_checks(n)} failing, rooted at {root}",
        )
    for incident_id in appended:
        n = checks.get(incident_id, 0)
        _queue(
            connection,
            settings,
            incident_id,
            "update",
            run_id,
            at,
            f"still failing: {_checks(n)} in {run_id}",
        )
    for incident_id in resolved:
        root = by_id[incident_id].root
        _queue(
            connection,
            settings,
            incident_id,
            "resolved",
            run_id,
            at,
            f"resolved: {root} healthy in {run_id}",
        )
    return opened, appended, resolved, len(events)


def _above(graph: nx.DiGraph[str], node: str) -> set[str]:
    return set(nx.ancestors(graph, node)) if node in graph else set()


def _checks(n: int) -> str:
    return f"{n} check" if n == 1 else f"{n} checks"


def _set_resolution(
    connection: psycopg.Connection[Any],
    settings: Settings,
    incident: _Incident,
    at: datetime | None,
    run_id: str | None,
) -> None:
    connection.execute(
        sql.SQL(
            "UPDATE {} SET resolved_at = %s, resolved_by_run = %s WHERE incident_id = %s"
        ).format(store.table(settings, "incidents")),
        [at, run_id, incident.incident_id],
    )
    incident.resolved_at, incident.resolved_by_run = at, run_id


def _open(
    connection: psycopg.Connection[Any],
    settings: Settings,
    context: Context,
    group: Group,
    run_id: str,
    at: datetime,
    incidents: Sequence[_Incident],
) -> _Incident:
    seq = max((i.seq for i in incidents), default=0) + 1
    incident_id = f"INC-{seq:03d}"
    scored = score_incident(
        context.graph, group.root, context.nodes, context.tier_of, context.policy
    )
    page = route(group.root, context.nodes, context.registry)
    naive = naive_route(group.root, context.nodes, context.registry)
    previous = [i.incident_id for i in incidents if i.root == group.root]
    connection.execute(
        sql.SQL(
            "INSERT INTO {} (incident_id, seq, root, dataset, severity, score, root_tier, owner, "
            "team, naive_owner, naive_team, routed_via, opened_at, opened_by_run, "
            "previous_incident_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s)"
        ).format(store.table(settings, "incidents")),
        [
            incident_id,
            seq,
            group.root,
            dataset_of(group.root, context.nodes),
            scored.severity,
            scored.score,
            scored.root_tier,
            page.owner,
            page.team,
            naive.owner,
            naive.team,
            page.routed_via,
            at,
            run_id,
            previous[-1] if previous else None,
        ],
    )
    return _Incident(incident_id, seq, group.root, page.owner, at, run_id, None, None, None)


def _queue(
    connection: psycopg.Connection[Any],
    settings: Settings,
    incident_id: str,
    kind: str,
    run_id: str,
    at: datetime,
    text: str,
) -> None:
    connection.execute(
        sql.SQL(
            "INSERT INTO {} (incident_id, kind, run_id, recipient, team, severity, text, sent_at) "
            "SELECT incident_id, %s, %s, owner, team, severity, %s, %s FROM {} "
            "WHERE incident_id = %s ON CONFLICT DO NOTHING"
        ).format(
            store.table(settings, "incident_notifications"), store.table(settings, "incidents")
        ),
        [kind, run_id, text, at, incident_id],
    )


def _deliver(settings: Settings, registry: Registry, channels: Sequence[Channel]) -> int:
    """Send what the last commit queued, then record how each one went."""
    with store.transaction(settings) as connection:
        rows = connection.execute(
            sql.SQL(
                "SELECT incident_id, kind, run_id, recipient, team, severity, text, sent_at "
                "FROM {} WHERE delivered IS NULL ORDER BY sent_at, incident_id, kind"
            ).format(store.table(settings, "incident_notifications"))
        ).fetchall()
        for row in rows:
            notification = Notification(*row)
            how = deliver(notification, registry.teams.get(notification.team), channels)
            connection.execute(
                sql.SQL(
                    "UPDATE {} SET delivered = %s WHERE incident_id = %s AND kind = %s "
                    "AND run_id = %s"
                ).format(store.table(settings, "incident_notifications")),
                [how, notification.incident_id, notification.kind, notification.run_id],
            )
    return len(rows)


def describe(result: IngestResult) -> str:
    return json.dumps(result.__dict__, default=str, sort_keys=True)
