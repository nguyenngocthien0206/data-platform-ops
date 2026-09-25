"""What the vendor collectors share: who a principal is, and reading recorded rows."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_ops.common.config import ActorType

# A declared vendor schema: column name to the JSON types a value may take.
Schema = Mapping[str, tuple[type, ...]]
NULLABLE = type(None)


@dataclass(frozen=True)
class Principals:
    """How a vendor principal (a login or service account) maps onto the toolkit.

    ``workloads`` says which workload a principal runs (dbt, dashboards);
    anything not listed is a person running ad hoc SQL. ``actors`` renames a
    principal; by default a person is recorded by the part of their login
    before ``@``, lower-cased, which is how ``config/teams.yaml`` names people.
    """

    workloads: Mapping[str, ActorType] = field(default_factory=dict)
    actors: Mapping[str, str] = field(default_factory=dict)

    def actor_type(self, principal: str) -> ActorType:
        return self.workloads.get(principal, "adhoc")

    def actor(self, principal: str) -> str:
        return self.actors.get(principal) or principal.split("@", 1)[0].lower()


@dataclass
class Skipped:
    """Rows a collector read but did not turn into records, by reason."""

    reasons: Counter[str] = field(default_factory=Counter)

    def add(self, reason: str) -> None:
        self.reasons[reason] += 1

    @property
    def total(self) -> int:
        return sum(self.reasons.values())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Recorded vendor rows, one JSON object per line."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def utc_naive(value: str) -> datetime:
    """An ISO 8601 timestamp as naive UTC, the toolkit's convention for query times."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def schema_problems(row: Mapping[str, Any], schema: Schema) -> list[str]:
    """Why ``row`` does not match ``schema``: missing, unexpected or mistyped columns."""
    problems = [f"missing {name}" for name in schema if name not in row]
    problems += [f"unexpected {name}" for name in row if name not in schema]
    for name, allowed in schema.items():
        if name not in row:
            continue
        value = row[name]
        # bool is a subclass of int in Python, but not in either vendor's schema.
        mistyped = not isinstance(value, allowed) or (
            isinstance(value, bool) and bool not in allowed
        )
        if mistyped:
            problems.append(f"{name} is {type(value).__name__}")
    return problems
