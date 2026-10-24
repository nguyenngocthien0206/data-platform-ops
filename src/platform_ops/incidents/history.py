"""The incident store read back as a history, for the report and its grading."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg import sql

from platform_ops.common.config import Settings, Severity, Tier
from platform_ops.incidents import store
from platform_ops.incidents.desk import Context
from platform_ops.incidents.ingest import CheckEvent
from platform_ops.incidents.notify import Notification
from platform_ops.incidents.severity import score_incident
from platform_ops.metadata.lineage import Consumer, downstream


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
    resolved_at: datetime | None
    previous_incident_id: str | None
    acknowledged_at: datetime | None = None
    fixed_at: datetime | None = None
    consumers: tuple[Consumer, ...] = ()
    run_ids: list[str] = field(default_factory=list)
    checks: int = 0
    skipped: set[str] = field(default_factory=set)
    # Failed nodes this incident shares with another incident's root above them.
    shared: set[str] = field(default_factory=set)
    # Who was paged: the owner, or the rule's backup when the owner was busy.
    paged: str = ""
    # Each raise: (at, from severity, to severity, reason), in order.
    escalations: list[tuple[datetime, str, str, str]] = field(default_factory=list)

    @property
    def handled_by(self) -> str:
        """The person paged, and on whose behalf when it was the backup."""
        if self.paged and self.paged != self.owner:
            return f"{self.paged} (backup for {self.owner})"
        return self.owner


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    run_at: datetime
    invocations: int
    checks_run: int
    failures: int
    opened: int
    appended: int
    resolved: int


@dataclass
class History:
    incidents: list[Incident] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    # (event, incident id) for every failing check.
    events: list[tuple[CheckEvent, str]] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)


def _rows(connection: Any, settings: Settings, name: str, query: str) -> list[tuple[Any, ...]]:
    return list(connection.execute(sql.SQL(query).format(store.table(settings, name))).fetchall())


def load_history(settings: Settings, context: Context) -> History:
    history = History()
    with store.transaction(settings) as c:
        incidents = _rows(
            c,
            settings,
            "incidents",
            "SELECT incident_id, root, dataset, severity, score, root_tier, owner, team, "
            "naive_owner, naive_team, routed_via, opened_at, resolved_at, previous_incident_id, "
            "merged_into, coalesce(paged, owner) FROM {} ORDER BY seq",
        )
        responses = {
            str(r[0]): (r[1], r[2])
            for r in _rows(
                c,
                settings,
                "incident_responses",
                "SELECT incident_id, acknowledged_at, fixed_at FROM {}",
            )
        }
        links = _rows(
            c,
            settings,
            "incident_run_links",
            "SELECT incident_id, run_id, checks FROM {} ORDER BY at, incident_id",
        )
        events = _rows(
            c,
            settings,
            "incident_check_events",
            "SELECT run_id, check_id, check_type, subject, status, message, detected_at, "
            "incident_id FROM {} ORDER BY detected_at, run_id, check_id",
        )
        skipped = _rows(
            c,
            settings,
            "incident_results",
            "SELECT DISTINCT run_id, unique_id FROM {} WHERE status = 'skipped' "
            "ORDER BY run_id, unique_id",
        )
        invocations = _rows(
            c,
            settings,
            "incident_invocations",
            "SELECT run_id, min(detected_at), count(*), sum(checks_run) FROM {} "
            "GROUP BY run_id ORDER BY min(detected_at), run_id",
        )
        raises = _rows(
            c,
            settings,
            "incident_escalations",
            "SELECT incident_id, at, from_severity, to_severity, reason FROM {} "
            "ORDER BY at, incident_id, reason",
        )
        shared = _rows(
            c,
            settings,
            "incident_shared_nodes",
            "SELECT incident_id, node FROM {} ORDER BY incident_id, node",
        )
        notes = _rows(
            c,
            settings,
            "incident_notifications",
            "SELECT incident_id, kind, run_id, recipient, team, severity, text, sent_at FROM {} "
            "ORDER BY sent_at, incident_id, kind",
        )
    skipped_by_run: dict[str, set[str]] = defaultdict(set)
    for run_id, unique_id in skipped:
        skipped_by_run[str(run_id)].add(str(unique_id))
    by_id: dict[str, Incident] = {}
    for row in incidents:
        (incident_id, root, dataset, severity, score, tier, owner, team, n_owner, n_team, via,
         opened, resolved, previous, merged, who) = row  # fmt: skip
        if merged is not None:
            history.merged.append(str(incident_id))
            continue
        ack, fixed = responses.get(str(incident_id), (None, None))
        scored = score_incident(
            context.graph, str(root), context.nodes, context.tier_of, context.policy
        )
        incident = Incident(
            str(incident_id), str(root), str(dataset), severity, int(score), tier,
            str(owner), str(team), str(n_owner), str(n_team), str(via),
            opened, resolved, previous, ack, fixed, scored.consumers, paged=str(who),
        )  # fmt: skip
        by_id[incident.incident_id] = incident
        history.incidents.append(incident)
    for incident_id, run_id, checks in links:
        linked_to = by_id.get(str(incident_id))
        if linked_to is not None:
            linked_to.run_ids.append(str(run_id))
            linked_to.checks += int(checks)
            below = downstream(context.graph, linked_to.dataset) | {linked_to.dataset}
            linked_to.skipped |= skipped_by_run[str(run_id)] & below
    for incident_id, at, before, after, reason in raises:
        raised_one = by_id.get(str(incident_id))
        if raised_one is not None:
            raised_one.escalations.append((at, str(before), str(after), str(reason)))
    for incident_id, node in shared:
        sharing = by_id.get(str(incident_id))
        if sharing is not None:
            sharing.shared.add(str(node))
    for run_id, check_id, kind, subject, status, message, detected_at, incident_id in events:
        event = CheckEvent(
            str(run_id),
            str(check_id),
            kind,
            str(subject),
            str(status),
            str(message),
            detected_at,
        )
        history.events.append((event, str(incident_id)))
    for run_id, run_at, n, checks_run in invocations:
        run = str(run_id)
        linked = [i for i in history.incidents if run in i.run_ids]
        history.runs.append(
            RunRecord(
                run,
                run_at,
                int(n),
                int(checks_run or 0),
                sum(1 for e, _ in history.events if e.run_id == run),
                sum(1 for i in linked if i.run_ids[0] == run),
                sum(1 for i in linked if i.run_ids[0] != run),
                sum(
                    1
                    for i in history.incidents
                    if i.resolved_at is not None and i.resolved_at == run_at
                ),
            )
        )
    history.notifications = [Notification(*row) for row in notes]
    return history
