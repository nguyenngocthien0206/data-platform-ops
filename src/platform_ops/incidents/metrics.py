"""Grading the production weeks' incidents against ground truth.

This is the only incident code that reads ``ops.incident_faults``, the ground
truth the company records as it plants faults in production. A fault maps to an
incident when the faulted source is the incident's root or one of its
ancestors, and the incident opened while the fault was active. Each fault
should map to exactly one incident, and each incident to exactly one fault;
anything else is reported, not hidden.

Routing is graded against the owner of the faulted source, for both the real
rule and the naive one (page whoever owns the root node), so the report shows
what the staging exception is worth.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import networkx as nx
from psycopg import sql

from platform_ops.common.config import SEVERITIES, Settings
from platform_ops.common.store import open_store, ops_table, replace_ops_table
from platform_ops.incidents.history import History, Incident
from platform_ops.incidents.routing import UNOWNED
from platform_ops.incidents.severity import dataset_of
from platform_ops.metadata.lineage import upstream
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Registry


@dataclass(frozen=True)
class FaultTruth:
    fault_id: str
    fault_type: str
    target: str
    source_id: str
    expected_check: str
    injected_at: datetime
    repaired_at: datetime | None
    owner: str
    team: str


@dataclass(frozen=True)
class FaultOutcome:
    truth: FaultTruth
    incident_ids: tuple[str, ...]


@dataclass(frozen=True)
class Accuracy:
    correct: int
    total: int

    @property
    def rate(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class IncidentMetrics:
    raw_alerts: int
    incidents: int
    pages: int
    faults: tuple[FaultOutcome, ...]
    unmatched_incidents: tuple[str, ...]
    routing_person: Accuracy
    routing_team: Accuracy
    naive_person: Accuracy
    naive_team: Accuracy
    mttd_hours: dict[str, float]
    mttr_hours: dict[str, float]
    unresolved: tuple[str, ...]
    weeks: float
    alerts_before: dict[str, int]
    alerts_after: dict[str, int]

    @property
    def one_incident_per_fault(self) -> bool:
        return all(len(f.incident_ids) == 1 for f in self.faults) and not self.unmatched_incidents

    def fault_of(self, incident_id: str) -> FaultTruth | None:
        for outcome in self.faults:
            if incident_id in outcome.incident_ids:
                return outcome.truth
        return None


def read_ground_truth(
    settings: Settings, nodes: Mapping[str, Node], registry: Registry
) -> list[FaultTruth]:
    source_of = {n.name: uid for uid, n in nodes.items() if n.resource_type == "source"}
    with open_store(settings) as connection:
        rows = connection.execute(
            sql.SQL(
                "SELECT fault_id, fault_type, table_name, expected_check, injected_at, "
                "repaired_at FROM {} ORDER BY injected_at, fault_id"
            ).format(ops_table(settings, "incident_faults"))
        ).fetchall()
    truths = []
    for fault_id, fault_type, table_name, expected, injected_at, repaired_at in rows:
        source_id = source_of[table_name]
        rule = registry.resolve(source_id).rule
        truths.append(
            FaultTruth(
                fault_id,
                fault_type,
                f"raw.{table_name}",
                source_id,
                expected,
                injected_at,
                repaired_at,
                rule.owner if rule else UNOWNED,
                rule.team if rule else UNOWNED,
            )
        )
    return truths


def _caused_by(
    incident: Incident, truth: FaultTruth, graph: nx.DiGraph[str], end: datetime
) -> bool:
    reach = upstream(graph, incident.root) | {incident.root}
    repaired = truth.repaired_at or end
    return truth.source_id in reach and truth.injected_at <= incident.opened_at <= repaired


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _mean_by_severity(values: Sequence[tuple[str, float]]) -> dict[str, float]:
    means: dict[str, float] = {}
    for severity in (*SEVERITIES, "all"):
        chosen = [v for s, v in values if severity in ("all", s)]
        if chosen:
            means[severity] = sum(chosen) / len(chosen)
    return means


def compute(
    result: History,
    truths: Sequence[FaultTruth],
    graph: nx.DiGraph[str],
    nodes: Mapping[str, Node],
    registry: Registry,
    days: int,
    end: datetime,
) -> IncidentMetrics:
    faults = tuple(
        FaultOutcome(
            t, tuple(i.incident_id for i in result.incidents if _caused_by(i, t, graph, end))
        )
        for t in truths
    )
    matched = {i for f in faults for i in f.incident_ids}
    unmatched = tuple(i.incident_id for i in result.incidents if i.incident_id not in matched)

    graded = [
        (f.truth, i) for f in faults for i in result.incidents if i.incident_id in f.incident_ids
    ]

    def accuracy(pairs: list[tuple[str, str]]) -> Accuracy:
        return Accuracy(sum(1 for got, want in pairs if got == want), len(pairs))

    mttd = [(i.severity, _hours(i.opened_at - t.injected_at)) for t, i in graded]
    mttr = [
        (i.severity, _hours(i.resolved_at - i.opened_at))
        for i in result.incidents
        if i.resolved_at is not None
    ]

    before: Counter[str] = Counter()
    for event, _ in result.events:
        rule = registry.resolve(dataset_of(event.subject, nodes)).rule
        before[rule.owner if rule else UNOWNED] += 1
    after: Counter[str] = Counter(n.recipient for n in result.notifications if n.kind == "page")

    return IncidentMetrics(
        raw_alerts=len(result.events),
        incidents=len(result.incidents),
        pages=sum(after.values()),
        faults=faults,
        unmatched_incidents=unmatched,
        routing_person=accuracy([(i.owner, t.owner) for t, i in graded]),
        routing_team=accuracy([(i.team, t.team) for t, i in graded]),
        naive_person=accuracy([(i.naive_owner, t.owner) for t, i in graded]),
        naive_team=accuracy([(i.naive_team, t.team) for t, i in graded]),
        mttd_hours=_mean_by_severity(mttd),
        mttr_hours=_mean_by_severity(mttr),
        unresolved=tuple(i.incident_id for i in result.incidents if i.resolved_at is None),
        weeks=days / 7,
        alerts_before=dict(sorted(before.items())),
        alerts_after=dict(sorted(after.items())),
    )


# -- persistence ---------------------------------------------------------------------


def metric_rows(metrics: IncidentMetrics) -> list[tuple[str, str, float]]:
    """``ops.incident_metrics`` in long form: (metric, dimension, value)."""
    rows: list[tuple[str, str, float]] = [
        ("raw_alerts", "all", metrics.raw_alerts),
        ("incidents", "all", metrics.incidents),
        ("pages", "all", metrics.pages),
        ("unmatched_incidents", "all", len(metrics.unmatched_incidents)),
        ("routing_accuracy", "person", metrics.routing_person.rate),
        ("routing_accuracy", "team", metrics.routing_team.rate),
        ("routing_accuracy_naive", "person", metrics.naive_person.rate),
        ("routing_accuracy_naive", "team", metrics.naive_team.rate),
    ]
    rows += [("mttd_hours", sev, round(value, 2)) for sev, value in metrics.mttd_hours.items()]
    rows += [("mttr_hours", sev, round(value, 2)) for sev, value in metrics.mttr_hours.items()]
    people = sorted(set(metrics.alerts_before) | set(metrics.alerts_after))
    for person in people:
        rows.append(
            (
                "alerts_per_week_before",
                person,
                round(metrics.alerts_before.get(person, 0) / metrics.weeks, 2),
            )
        )
        rows.append(
            (
                "alerts_per_week_after",
                person,
                round(metrics.alerts_after.get(person, 0) / metrics.weeks, 2),
            )
        )
    return rows


def persist(settings: Settings, metrics: IncidentMetrics) -> None:
    pairs = [(f.truth.fault_id, i) for f in metrics.faults for i in f.incident_ids]
    with open_store(settings) as connection:
        replace_ops_table(
            connection,
            settings,
            "incident_metrics",
            "metric text NOT NULL, dimension text NOT NULL, value double precision NOT NULL",
            ["metric", "dimension", "value"],
            metric_rows(metrics),
        )
        replace_ops_table(
            connection,
            settings,
            "fault_incidents",
            "fault_id text NOT NULL, incident_id text NOT NULL",
            ["fault_id", "incident_id"],
            pairs,
        )
