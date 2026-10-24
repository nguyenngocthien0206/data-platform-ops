"""The incident store: live state in the Postgres ops store (version 2.0, Phase 14).

Unlike the rest of the ops store, which each module replaces whole, these
tables are written by every ingest and read while ingests are still arriving,
so their schema is versioned: ``incident_schema`` records the version applied,
and :data:`MIGRATIONS` brings any older store up to date, in order, under a
lock. Every ingest runs in one transaction holding :data:`LOCK`, so two ingests
of the same night (or of different nights) never interleave: the second sees
everything the first committed.

The tables:

- ``incident_invocations``: one row per dbt invocation ingested, by dbt's own
  invocation id, which is what makes ingesting the same artifacts twice change
  nothing;
- ``incident_results``: every node each invocation reported on, and its
  status, which is how a later run is known to have covered a root;
- ``incident_check_events``: every failing check, and the incident it belongs to;
- ``incidents``: one row per incident;
- ``incident_run_links``: which runs failed into which incident;
- ``incident_notifications``: every page, update and resolution, and how each
  was delivered;
- ``incident_responses``: when an owner acknowledged and fixed an incident
  (simulated in the company, entered by people elsewhere);
- ``incident_shared_nodes``: failed nodes below more than one root, linked to
  each incident whose root is above them (version 2);
- ``incident_escalations``: every time an incident was raised a level, and why
  (version 2).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.store import open_store

# One lock for every incident write, as a Postgres advisory lock key.
LOCK = 1_402_000_014

TABLES = (
    "incident_schema",
    "incident_invocations",
    "incident_results",
    "incident_check_events",
    "incidents",
    "incident_run_links",
    "incident_notifications",
    "incident_responses",
    "incident_shared_nodes",
    "incident_escalations",
)

# Version -> the statements that bring the store from the version before to it.
# ``{s}`` is the (namespaced) ops schema. Never edit a released migration; add one.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE {s}.incident_invocations (
                invocation_id text PRIMARY KEY, run_id text NOT NULL, kind text NOT NULL,
                detected_at timestamp NOT NULL, checks_run integer NOT NULL,
                ingested_seq bigserial NOT NULL)""",
            """CREATE TABLE {s}.incident_results (
                invocation_id text NOT NULL, run_id text NOT NULL, unique_id text NOT NULL,
                resource_type text NOT NULL, status text NOT NULL)""",
            "CREATE INDEX ON {s}.incident_results (run_id)",
            """CREATE TABLE {s}.incident_check_events (
                invocation_id text NOT NULL, run_id text NOT NULL, check_id text NOT NULL,
                check_type text NOT NULL, subject text NOT NULL, status text NOT NULL,
                message text NOT NULL, detected_at timestamp NOT NULL, incident_id text,
                PRIMARY KEY (run_id, check_id))""",
            """CREATE TABLE {s}.incidents (
                incident_id text PRIMARY KEY, seq integer NOT NULL UNIQUE,
                root text NOT NULL, dataset text NOT NULL,
                severity text NOT NULL, score integer NOT NULL, root_tier text NOT NULL,
                owner text NOT NULL, team text NOT NULL,
                naive_owner text NOT NULL, naive_team text NOT NULL, routed_via text NOT NULL,
                opened_at timestamp NOT NULL, opened_by_run text NOT NULL,
                resolved_at timestamp, resolved_by_run text,
                merged_into text, previous_incident_id text)""",
            # At most one open incident per root.
            """CREATE UNIQUE INDEX incidents_one_open_per_root ON {s}.incidents (root)
                WHERE resolved_at IS NULL AND merged_into IS NULL""",
            """CREATE TABLE {s}.incident_run_links (
                incident_id text NOT NULL, run_id text NOT NULL, at timestamp NOT NULL,
                checks integer NOT NULL, PRIMARY KEY (incident_id, run_id))""",
            """CREATE TABLE {s}.incident_notifications (
                incident_id text NOT NULL, kind text NOT NULL, run_id text NOT NULL,
                recipient text NOT NULL, team text NOT NULL, severity text NOT NULL,
                text text NOT NULL, sent_at timestamp NOT NULL, delivered text,
                PRIMARY KEY (incident_id, kind, run_id))""",
            """CREATE TABLE {s}.incident_responses (
                incident_id text PRIMARY KEY, acknowledged_at timestamp,
                fixed_at timestamp)""",
        ),
    ),
    (
        2,
        (
            # A failed node below several roots, shared by each of their incidents.
            """CREATE TABLE {s}.incident_shared_nodes (
                incident_id text NOT NULL, run_id text NOT NULL, node text NOT NULL,
                PRIMARY KEY (incident_id, run_id, node))""",
            # Who was paged: the owner, or the rule's backup when the owner was busy.
            "ALTER TABLE {s}.incidents ADD COLUMN paged text",
            # Every time an incident was raised a level, and why.
            """CREATE TABLE {s}.incident_escalations (
                incident_id text NOT NULL, run_id text NOT NULL, at timestamp NOT NULL,
                from_severity text NOT NULL, to_severity text NOT NULL, reason text NOT NULL,
                PRIMARY KEY (incident_id, reason))""",
        ),
    ),
)
VERSION = MIGRATIONS[-1][0]


def schema(settings: Settings) -> sql.Identifier:
    return sql.Identifier(settings.company.name(settings.company.ops_schema))


def _apply(connection: psycopg.Connection[Any], settings: Settings) -> int:
    """Bring the store to :data:`VERSION`; returns the version it was at."""
    s = schema(settings)
    connection.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {}.incident_schema (version integer NOT NULL)").format(
            s
        )
    )
    row = connection.execute(
        sql.SQL("SELECT max(version) FROM {}.incident_schema").format(s)
    ).fetchone()
    current = int(row[0]) if row and row[0] is not None else 0
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for statement in statements:
            connection.execute(sql.SQL(statement).format(s=s))
        connection.execute(
            sql.SQL("INSERT INTO {}.incident_schema VALUES (%s)").format(s), [version]
        )
    return current


@contextmanager
def transaction(settings: Settings) -> Iterator[psycopg.Connection[Any]]:
    """The store, migrated, inside one transaction holding the incident lock."""
    with open_store(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK])
        _apply(connection, settings)
        yield connection


def reset(settings: Settings) -> None:
    """Empty the store for a new history (the company's run starts one)."""
    with open_store(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK])
        for table in TABLES:
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    schema(settings), sql.Identifier(table)
                )
            )
        _apply(connection, settings)


def table(settings: Settings, name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(schema(settings), sql.Identifier(name))
