"""The incident desk for the simulated company's production weeks (Phase 14).

The company run (``simulation.company``) hands every production invocation to
:class:`CompanyDesk`, which ingests it exactly as ``platform-ops incidents
ingest`` would, as the night's run. For each incident a night opens, the desk
then plays the owner: it acknowledges and fixes on the modelled schedule
(``lifecycle``), and tells the company when the fix lands, which faulted
sources it covers (everything upstream of the incident's root), and what to
rerun then: the root and everything below it (``rerun_commands``). The company
resyncs those raw tables and reruns at the time of the fix, and that run,
handed back to the desk, shows the root healthy and resolves the incident the
same day instead of at the next night's run.

The company's own notifications stay local: the simulated people are not on a
real Slack. A real ingest (``incidents ingest``) uses the configured channels.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.servers import clickhouse_client
from platform_ops.incidents import store
from platform_ops.incidents.desk import Context, ingest_target
from platform_ops.incidents.lifecycle import respond
from platform_ops.metadata.column_lineage import clickhouse_catalogue
from platform_ops.metadata.lineage import upstream
from platform_ops.metadata.manifest import Node
from platform_ops.simulation.access import DBT_LAYERS
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
            # The company's warehouse is at hand, so routing can follow column lineage.
            client = clickhouse_client(self.settings)
            try:
                company = self.settings.company
                databases = [company.name(d) for d in (company.raw_database, *DBT_LAYERS)]
                catalogue = clickhouse_catalogue(client, databases)
            finally:
                client.close()
            self._context = Context.load(
                self.settings, target / "manifest.json", catalogue=catalogue
            )
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
                    "SELECT root, coalesce(paged, owner), severity, opened_at FROM {} "
                    "WHERE incident_id = %s"
                ).format(store.table(settings, "incidents")),
                [incident_id],
            ).fetchone()
            assert row is not None
            # The person paged answers: the owner, or the backup when the owner was busy.
            root, person, severity, opened_at = row
            # How many other incidents that person already has open slows them down.
            (busy,) = connection.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE coalesce(paged, owner) = %s "
                    "AND incident_id <> %s AND merged_into IS NULL AND opened_at <= %s "
                    "AND (resolved_at IS NULL OR resolved_at > %s)"
                ).format(store.table(settings, "incidents")),
                [person, incident_id, opened_at, opened_at],
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
        commands = rerun_commands(context.nodes, root)
        self._fixes.append(Fix(incident_id, tables, response.fixed_at, commands))


# Snapshots record history at the night's run; a rerun after a fix must not add
# a version of its own in the middle of the day.
RERUN_EXCLUDE = ("--exclude", "resource_type:snapshot")


def rerun_commands(nodes: Mapping[str, Node], root: str) -> tuple[tuple[str, ...], ...]:
    """The dbt invocations that check ``root`` again after a fix, and rebuild what it feeds.

    A source: its freshness, then a build of the source and everything below
    it (its tests included). A model: a build of it and everything below. A
    test: a build of the model it is attached to and everything below, which
    runs the test again.
    """
    node = nodes.get(root)
    if node is None:
        return ()
    if node.resource_type == "source":
        _, _, source, table = root.split(".", 3)
        selector = f"source:{source}.{table}"
        return (
            ("source", "freshness", "--select", selector),
            ("build", "--select", f"{selector}+", *RERUN_EXCLUDE),
        )
    if node.resource_type == "test":
        attached = nodes.get(node.attached_node or "")
        if attached is None:
            return (("build", "--select", node.name, *RERUN_EXCLUDE),)
        node = attached
    return (("build", "--select", f"{node.name}+", *RERUN_EXCLUDE),)
