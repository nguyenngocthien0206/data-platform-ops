"""Turn dbt's result files into check events.

dbt reports three kinds of failure, in two files: a model that errors and a test
that fails or errors (``run_results.json``), and a source that is too stale
(``sources.json``). They are normalised here into :class:`CheckEvent`, one per
failing check, each tied to the node it is about. Grouping only ever sees these
events, never dbt's files, so its tests can use hand-built events.

Only real failures become events. A freshness ``warn`` is logged by dbt and
ignored here; nobody is paged for it. A skipped model is not a failure either:
it is collateral damage from a failure upstream, and is kept as impact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from platform_ops.common.db import OPS_SCHEMA, insert_rows
from platform_ops.metadata.manifest import Node

CheckType = Literal["model", "test", "freshness"]

FAILED_TEST_STATUSES = frozenset({"fail", "error"})
FAILED_FRESHNESS_STATUSES = frozenset({"error", "runtime error"})
MESSAGE_LIMIT = 300


@dataclass(frozen=True, order=True)
class CheckEvent:
    """One failing check in one run."""

    run_id: str
    check_id: str
    check_type: CheckType
    # The node the failure is about: the model that errored, the source that is
    # stale, or the model a test is attached to. A test that spans several models
    # (a relationships test) is its own subject, downstream of all of them.
    subject: str
    status: str
    message: str
    detected_at: datetime


@dataclass(frozen=True)
class RunOutcome:
    """Everything one scheduled run said: failures, and nodes it skipped."""

    events: tuple[CheckEvent, ...]
    skipped: tuple[str, ...]
    checks_run: int


def test_subject(test: Node) -> str:
    """The node a failing test is about.

    A test with one parent is about that parent. A test with several parents
    checks how they fit together, so it is about neither alone; the test itself
    is the subject, and lineage places it below every model it reads.
    """
    parents = [p for p in test.depends_on if not p.startswith("macro.")]
    if len(parents) <= 1 and test.attached_node:
        return test.attached_node
    return test.unique_id


def _message(raw: object) -> str:
    text = " ".join(str(raw or "").split())
    return text[:MESSAGE_LIMIT]


def parse_run_results(
    data: Mapping[str, Any],
    nodes: Mapping[str, Node],
    *,
    run_id: str,
    detected_at: datetime,
) -> RunOutcome:
    """Failures and skips from one ``run_results.json`` (``dbt run`` or ``dbt test``)."""
    events: list[CheckEvent] = []
    skipped: list[str] = []
    results = data.get("results", [])
    for result in results:
        unique_id = str(result["unique_id"])
        status = str(result["status"])
        node = nodes.get(unique_id)
        if node is None:
            continue
        if status == "skipped":
            skipped.append(unique_id)
        elif node.resource_type == "model" and status == "error":
            events.append(
                CheckEvent(run_id, unique_id, "model", unique_id, status,
                           _message(result.get("message")), detected_at)
            )  # fmt: skip
        elif node.resource_type == "test" and status in FAILED_TEST_STATUSES:
            events.append(
                CheckEvent(run_id, unique_id, "test", test_subject(node), status,
                           _message(result.get("message")), detected_at)
            )  # fmt: skip
    return RunOutcome(tuple(sorted(events)), tuple(sorted(skipped)), len(results))


def parse_freshness(data: Mapping[str, Any], *, run_id: str, detected_at: datetime) -> RunOutcome:
    """Failures from one ``sources.json``. Only ``error`` counts; ``warn`` does not."""
    events = [
        CheckEvent(run_id, f"freshness:{result['unique_id']}", "freshness",
                   str(result["unique_id"]), str(result["status"]),
                   _message(f"max_loaded_at {result.get('max_loaded_at')}, "
                            f"age {float(result.get('age') or 0) / 3600:.1f} h"),
                   detected_at)
        for result in data.get("results", [])
        if str(result.get("status")) in FAILED_FRESHNESS_STATUSES
    ]  # fmt: skip
    return RunOutcome(tuple(sorted(events)), (), len(data.get("results", [])))


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def merge(outcomes: Sequence[RunOutcome]) -> RunOutcome:
    """One run's outcome from its ``dbt run``, ``dbt test`` and freshness parts."""
    events = sorted({event for outcome in outcomes for event in outcome.events})
    skipped = sorted({node for outcome in outcomes for node in outcome.skipped})
    return RunOutcome(tuple(events), tuple(skipped), sum(o.checks_run for o in outcomes))


CHECK_EVENTS_DDL = """
    run_id VARCHAR NOT NULL,
    check_id VARCHAR NOT NULL,
    check_type VARCHAR NOT NULL,
    subject VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    detected_at TIMESTAMP NOT NULL,
    incident_id VARCHAR"""

CHECK_EVENT_COLUMNS = (
    "run_id", "check_id", "check_type", "subject", "status", "message", "detected_at",
    "incident_id",
)  # fmt: skip


def reset_check_events(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.check_events ({CHECK_EVENTS_DDL})")


def persist_events(
    connection: duckdb.DuckDBPyConnection, events: Sequence[tuple[CheckEvent, str | None]]
) -> int:
    """Append events, each tagged with the incident it was grouped into."""
    rows = [
        (e.run_id, e.check_id, e.check_type, e.subject, e.status, e.message, e.detected_at,
         incident_id)
        for e, incident_id in events
    ]  # fmt: skip
    return insert_rows(connection, f"{OPS_SCHEMA}.check_events", CHECK_EVENT_COLUMNS, rows)
