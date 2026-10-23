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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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

    A test with one parent is about that parent: the model it is attached to,
    or the source it reads (dbt attaches no node to a source's tests). A test
    with several parents checks how they fit together, so it is about neither
    alone; the test itself is the subject, and lineage places it below every
    model it reads.
    """
    parents = [p for p in test.depends_on if not p.startswith("macro.")]
    if len(parents) <= 1 and test.attached_node:
        return test.attached_node
    if len(parents) == 1 and parents[0].startswith("source."):
        return parents[0]
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
                CheckEvent(
                    run_id,
                    unique_id,
                    "model",
                    unique_id,
                    status,
                    _message(result.get("message")),
                    detected_at,
                )
            )
        elif node.resource_type == "test" and status in FAILED_TEST_STATUSES:
            events.append(
                CheckEvent(
                    run_id,
                    unique_id,
                    "test",
                    test_subject(node),
                    status,
                    _message(result.get("message")),
                    detected_at,
                )
            )
    return RunOutcome(tuple(sorted(events)), tuple(sorted(skipped)), len(results))


def parse_freshness(data: Mapping[str, Any], *, run_id: str, detected_at: datetime) -> RunOutcome:
    """Failures from one ``sources.json``. Only ``error`` counts; ``warn`` does not."""
    events = [
        CheckEvent(
            run_id,
            f"freshness:{result['unique_id']}",
            "freshness",
            str(result["unique_id"]),
            str(result["status"]),
            _message(
                f"max_loaded_at {result.get('max_loaded_at')}, "
                f"age {float(result.get('age') or 0) / 3600:.1f} h"
            ),
            detected_at,
        )
        for result in data.get("results", [])
        if str(result.get("status")) in FAILED_FRESHNESS_STATUSES
    ]
    return RunOutcome(tuple(sorted(events)), (), len(data.get("results", [])))


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def merge(outcomes: Sequence[RunOutcome]) -> RunOutcome:
    """One run's outcome from its ``dbt run``, ``dbt test`` and freshness parts."""
    events = sorted({event for outcome in outcomes for event in outcome.events})
    skipped = sorted({node for outcome in outcomes for node in outcome.skipped})
    return RunOutcome(tuple(events), tuple(skipped), sum(o.checks_run for o in outcomes))


@dataclass(frozen=True)
class Artifact:
    """One dbt invocation's result file, as found in a target folder."""

    invocation_id: str
    kind: Literal["run_results", "sources"]
    generated_at: datetime
    data: dict[str, Any]


def _generated_at(data: Mapping[str, Any]) -> datetime:
    """When dbt wrote the file, as a naive UTC time (the store's convention)."""
    raw = str((data.get("metadata") or {}).get("generated_at") or "")
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime(1970, 1, 1)
    return (
        stamp.replace(tzinfo=None)
        if stamp.tzinfo is None
        else stamp.astimezone(UTC).replace(tzinfo=None)
    )


def read_target(target: Path) -> list[Artifact]:
    """The result files in a dbt target folder: ``run_results.json`` and ``sources.json``.

    Either may be left over from an earlier invocation; the store recognises
    those by their invocation id and ignores them.
    """
    found = []
    for name, kind in (("sources.json", "sources"), ("run_results.json", "run_results")):
        path = target / name
        if path.is_file():
            data = read_json(path)
            invocation = str((data.get("metadata") or {}).get("invocation_id") or "")
            if invocation:
                found.append(Artifact(invocation, kind, _generated_at(data), data))  # type: ignore[arg-type]
    return found


def results(artifact: Artifact, nodes: Mapping[str, Node]) -> list[tuple[str, str, str]]:
    """Every node an invocation reported on: (unique id, resource type, status)."""
    out = []
    for result in artifact.data.get("results", []):
        unique_id = str(result.get("unique_id", ""))
        node = nodes.get(unique_id)
        kind = node.resource_type if node is not None else unique_id.split(".", 1)[0]
        out.append((unique_id, kind, str(result.get("status", ""))))
    return sorted(out)


def parse(
    artifact: Artifact, nodes: Mapping[str, Node], *, run_id: str, detected_at: datetime
) -> RunOutcome:
    if artifact.kind == "sources":
        return parse_freshness(artifact.data, run_id=run_id, detected_at=detected_at)
    return parse_run_results(artifact.data, nodes, run_id=run_id, detected_at=detected_at)
