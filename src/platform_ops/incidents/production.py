"""The incident desk for the simulated company's production weeks (Phase 14).

The company run (``simulation.company``) hands every production invocation to
:class:`CompanyDesk`, which ingests it exactly as ``platform-ops incidents
ingest`` would, as the night's run. For each incident a night opens, the desk
then plays the owner: it acknowledges and fixes on the modelled schedule
(``lifecycle``), and tells the company when the fix lands and which faulted
sources it covers, everything upstream of the incident's root. The company
resyncs those raw tables then, and the next night's run shows the root healthy
and resolves the incident.

The company's own notifications stay local: the simulated people are not on a
real Slack. A real ingest (``incidents ingest``) uses the configured channels.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.incidents import store
from platform_ops.incidents.desk import Context, ingest_target
from platform_ops.incidents.lifecycle import respond
from platform_ops.metadata.lineage import upstream
from platform_ops.simulation.company import Fix


class CompanyDesk:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._context: Context | None = None
        self._fixes: list[Fix] = []
        self._started = False

    def ingest(self, run_id: str, at: datetime, target: Path) -> None:
        if not self._started:
            # A new history starts with an empty store.
            store.reset(self.settings)
            self._started = True
        if self._context is None:
            self._context = Context.load(self.settings, target / "manifest.json")
        result = ingest_target(self.settings, target, run_id, at=at, context=self._context)
        for incident_id in result.opened:
            self._respond(incident_id)

    def fixes(self) -> list[Fix]:
        found, self._fixes = self._fixes, []
        return found

    def _respond(self, incident_id: str) -> None:
        context = self._context
        assert context is not None
        settings = self.settings
        with store.transaction(settings) as connection:
            row = connection.execute(
                sql.SQL(
                    "SELECT root, owner, severity, opened_at FROM {} WHERE incident_id = %s"
                ).format(store.table(settings, "incidents")),
                [incident_id],
            ).fetchone()
            assert row is not None
            root, owner, severity, opened_at = row
            # How many other incidents the owner already has open slows them down.
            (busy,) = connection.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE owner = %s AND incident_id <> %s "
                    "AND merged_into IS NULL AND opened_at <= %s "
                    "AND (resolved_at IS NULL OR resolved_at > %s)"
                ).format(store.table(settings, "incidents")),
                [owner, incident_id, opened_at, opened_at],
            ).fetchone() or (0,)
            response = respond(
                incident_id,
                severity,
                opened_at,
                int(busy),
                settings.incidents.lifecycle,
                settings.seed,
            )
            connection.execute(
                sql.SQL("INSERT INTO {} VALUES (%s, %s, %s) ON CONFLICT DO NOTHING").format(
                    store.table(settings, "incident_responses")
                ),
                [incident_id, response.acknowledged_at, response.fixed_at],
            )
        reach = upstream(context.graph, root) | {root}
        tables = tuple(
            sorted(
                context.nodes[node].name
                for node in reach
                if node in context.nodes and context.nodes[node].resource_type == "source"
            )
        )
        self._fixes.append(Fix(incident_id, tables, response.fixed_at))
