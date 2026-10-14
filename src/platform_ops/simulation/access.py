"""The company's ClickHouse users, and what each one may do (version 2.0).

A real cluster does not run everything as one admin. Each team queries as its
own user, the BI tool and the pipelines run as service accounts, and grants
say who may read or write what. That is also what makes ClickHouse's query log
worth reading: every query carries the user that ran it, so Phase 13 can show
each team its own load and suggest quotas for the heaviest.

The users, all namespaced like every database:

- one per team in ``config/teams.yaml``. The business teams read the marts and
  the reference data; the platform team reads every layer, because it owns them
  and is who debugs them;
- ``metabase``: the company's BI, reading the marts and the reference data,
  and only its own entries in ``system.processes``;
- ``dbt``: reads raw, and owns the databases it builds;
- ``loader``: the ingestion, writing raw from Postgres through ``postgresql()``.

People and BI read through a ``reader`` settings profile (``readonly = 2``: no
writes, but a session may still change settings); the pipelines use
``writer``. Both carry the same session settings as the dbt profile, so a
query means the same thing whoever runs it, and ``writer`` also writes the way
dbt does: one part per partition per insert. No resource limits or quotas are
applied; the cost report suggests them from measured load (Phase 13).

Passwords come from ``CLICKHOUSE_PASSWORD_<USER>``, one variable per user, with
no default (``common.servers``). The admin that creates the users is the
toolkit's own account and runs nothing for the company.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from platform_ops.common.config import Settings
from platform_ops.common.servers import clickhouse_client, require_passwords
from platform_ops.metadata.registry import Registry

# The ClickHouse databases dbt builds into, one per layer, after the raw one.
DBT_LAYERS = ("reference", "staging", "intermediate", "marts", "snapshots")

# What every session means by SQL, the same as the dbt profile's custom_settings.
_SESSION = "join_use_nulls = 1, union_default_mode = 'DISTINCT'"
# How every insert writes, the same as the dbt profile's: on one thread and
# squashed into one block, so a table gets one part per partition and what a
# later query reads does not depend on when background merges ran.
_WRITES = (
    "max_insert_threads = 1, min_insert_block_size_rows = 1000000000, "
    "min_insert_block_size_bytes = 100000000000"
)
PROFILES = {
    "reader": f"readonly = 2, {_SESSION}",
    "writer": f"{_SESSION}, {_WRITES}",
}

Role = Literal["team", "platform", "bi", "transform", "load"]


@dataclass(frozen=True)
class CompanyUser:
    name: str
    role: Role

    @property
    def profile(self) -> str:
        return "writer" if self.role in ("transform", "load") else "reader"


def company_users(settings: Settings) -> list[CompanyUser]:
    """The teams in ``teams.yaml`` order, then the service users."""
    teams = Registry.from_config_dir(settings.root / "config").teams
    users = [CompanyUser(team, "platform" if team == "platform" else "team") for team in teams]
    return [
        *users,
        CompanyUser("metabase", "bi"),
        CompanyUser("dbt", "transform"),
        CompanyUser("loader", "load"),
    ]


def _db(settings: Settings, base: str) -> str:
    return f"`{settings.company.name(base)}`.*"


def grants(settings: Settings, user: CompanyUser) -> list[str]:
    """The privileges of one user, as ``GRANT`` statements."""
    company = settings.company
    raw = company.raw_database
    who = f"`{company.name(user.name)}`"
    if user.role in ("team", "bi"):
        readable = [f"GRANT SELECT ON {_db(settings, b)} TO {who}" for b in ("marts", "reference")]
        if user.role == "team":
            return readable
        # Metabase's ClickHouse driver looks each query up in system.processes
        # (to cancel it). The row policy keeps it to its own queries: without
        # it, the BI tool could read the text of every user's running query.
        policy = f"`{company.name('own_queries')}`"
        return [
            *readable,
            f"GRANT SELECT ON system.processes TO {who}",
            f"CREATE ROW POLICY OR REPLACE {policy} ON system.processes "
            f"USING user = currentUser() TO {who}",
        ]
    if user.role == "platform":
        return [f"GRANT SELECT ON {_db(settings, b)} TO {who}" for b in (raw, *DBT_LAYERS)]
    if user.role == "transform":
        return [
            f"GRANT SELECT ON {_db(settings, raw)} TO {who}",
            *(f"GRANT ALL ON {_db(settings, layer)} TO {who}" for layer in DBT_LAYERS),
            # dbt-clickhouse's insert_overwrite finds the partitions to replace
            # here; ClickHouse shows each user only the parts it may read.
            f"GRANT SELECT ON system.parts TO {who}",
        ]
    return [
        f"GRANT INSERT ON {_db(settings, raw)} TO {who}",
        # postgresql() needs both: the source itself, and a temporary table.
        f"GRANT READ ON POSTGRES TO {who}",
        f"GRANT CREATE TEMPORARY TABLE ON *.* TO {who}",
    ]


def ensure_users(settings: Settings) -> list[str]:
    """Create or replace every company user, its profile and its grants. Safe to repeat.

    Fails before touching anything when a password is missing, naming every
    missing variable at once. Returns the namespaced user names.
    """
    users = company_users(settings)
    passwords = require_passwords(settings, [u.name for u in users])
    company = settings.company
    client = clickhouse_client(settings)
    try:
        for profile, values in PROFILES.items():
            client.command(
                f"CREATE SETTINGS PROFILE OR REPLACE `{company.name(profile)}` SETTINGS {values}"
            )
        for user in users:
            # OR REPLACE drops the user's old grants, so the grants below are all it has.
            client.command(
                f"CREATE USER OR REPLACE `{company.name(user.name)}` "
                "IDENTIFIED WITH sha256_password BY %(password)s "
                f"SETTINGS PROFILE `{company.name(user.profile)}`",
                parameters={"password": passwords[user.name]},
            )
            for statement in grants(settings, user):
                client.command(statement)
    finally:
        client.close()
    return [company.name(u.name) for u in users]


def drop_users(settings: Settings) -> None:
    """Remove the company's users and profiles (the tests clean their namespace with it)."""
    company = settings.company
    client = clickhouse_client(settings)
    try:
        for user in company_users(settings):
            client.command(f"DROP USER IF EXISTS `{company.name(user.name)}`")
        for profile in PROFILES:
            client.command(f"DROP SETTINGS PROFILE IF EXISTS `{company.name(profile)}`")
        client.command(
            f"DROP ROW POLICY IF EXISTS `{company.name('own_queries')}` ON system.processes"
        )
    finally:
        client.close()
