"""Every query the company runs is tagged in ClickHouse's query log (version 2.0).

ClickHouse records each query with its user, what it read and wrote, and a
free-text ``log_comment`` a session can set. The company's runs set it to a
small JSON tag: which run the query belongs to, the simulated time it ran at,
the kind of work (``ActorType``), and what it was about (a model, a source, a
dashboard, a person). The cost module reads the query log back by that tag
(Phase 13), so a month means a simulated month, and a query is attributed to
what it was for, not to whoever happened to hold the connection.

The run id is set once per company run and only tells two runs' queries apart
in a query log that keeps both; it never appears in a report. Queries that
carry no tag (the toolkit's own setup, fingerprints, collection) are not the
company's workload and are left out of the cost.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from platform_ops.common.config import ActorType

# How dbt receives the tag: its profile reads this variable into custom_settings.
LOG_COMMENT_ENV_VAR = "PLATFORM_OPS_LOG_COMMENT"

# One company run per process at a time. A plain module value rather than a
# ContextVar: the reconciliation reconciles from a thread pool, and threads do
# not inherit a context.
_RUN = {"id": ""}


def start_run() -> str:
    """A new run id for this process's company run, and make it the current one."""
    _RUN["id"] = uuid.uuid4().hex[:12]
    return _RUN["id"]


def current_run() -> str:
    return _RUN["id"]


@dataclass(frozen=True)
class Tag:
    sim: datetime
    actor: ActorType
    # The dbt node, raw source, exposure or reconciled pair the query is about.
    subject: str = ""
    # For ad hoc SQL: the person who typed it.
    person: str = ""

    def comment(self) -> str:
        return json.dumps(
            {
                "run": current_run(),
                "sim": self.sim.isoformat(timespec="seconds"),
                "actor": self.actor,
                "subject": self.subject,
                "person": self.person,
            },
            sort_keys=True,
        )

    def settings(self) -> dict[str, Any]:
        """Per-query ClickHouse settings that carry the tag into the query log."""
        return {"log_comment": self.comment()} if current_run() else {}


def parse(comment: str) -> dict[str, str] | None:
    """A tag read back from the query log, or ``None`` when the query had none."""
    if not comment or not comment.startswith("{"):
        return None
    try:
        data = json.loads(comment)
    except ValueError:
        return None
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
