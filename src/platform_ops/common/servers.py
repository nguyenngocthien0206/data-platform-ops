"""Connections to the simulated company's servers (version 2.0).

The company runs on two servers in Docker Compose: the legacy Postgres that its
reporting has always used, and the ClickHouse it is migrating to. The toolkit's
own results live in a separate database on the same Postgres server (see
``common.store``). Hosts, ports, users and passwords come from the environment
or ``.env``, the same values Compose starts the containers with; a real
environment variable always wins, so the toolkit's container reaches the
servers by their Compose service names.

Inside ClickHouse the company has one user per team, plus ``metabase``, ``dbt``
and ``loader`` (``simulation.access``), so ClickHouse's query log records who
ran what. Each one's password comes from its own variable,
``CLICKHOUSE_PASSWORD_<USER>``, with no default: a missing one is an error that
names the variable, never a silent fallback to another account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from platform_ops.common.config import Settings
from platform_ops.common.env import env_value

if TYPE_CHECKING:
    import psycopg


@dataclass(frozen=True)
class PostgresParams:
    host: str
    port: int
    user: str
    password: str
    dbname: str


@dataclass(frozen=True)
class ClickHouseParams:
    host: str
    port: int
    user: str
    password: str


def postgres_params(settings: Settings, dbname: str | None = None) -> PostgresParams:
    env_file = settings.root / ".env"
    return PostgresParams(
        host=env_value("POSTGRES_HOST", env_file, "localhost") or "localhost",
        port=int(env_value("POSTGRES_PORT", env_file, "5432") or 5432),
        user=env_value("POSTGRES_USER", env_file, "platform_ops") or "platform_ops",
        password=env_value("POSTGRES_PASSWORD", env_file, "") or "",
        dbname=dbname or env_value("POSTGRES_DB", env_file, "legacy") or "legacy",
    )


def clickhouse_params(settings: Settings) -> ClickHouseParams:
    env_file = settings.root / ".env"
    return ClickHouseParams(
        host=env_value("CLICKHOUSE_HOST", env_file, "localhost") or "localhost",
        port=int(env_value("CLICKHOUSE_PORT", env_file, "8123") or 8123),
        user=env_value("CLICKHOUSE_USER", env_file, "platform_ops") or "platform_ops",
        password=env_value("CLICKHOUSE_PASSWORD", env_file, "change_me_locally")
        or "change_me_locally",
    )


def connect_postgres(settings: Settings, dbname: str | None = None) -> psycopg.Connection[Any]:
    """A connection to the legacy database, or to ``dbname`` on the same server.

    Sessions run in UTC: every timestamp in the company is a naive simulated
    time, and nothing may shift it by the server's zone.
    """
    import psycopg

    params = postgres_params(settings, dbname)
    connection = psycopg.connect(
        host=params.host,
        port=params.port,
        user=params.user,
        password=params.password,
        dbname=params.dbname,
    )
    connection.execute("SET TimeZone = 'UTC'")
    connection.commit()
    return connection


class MissingPasswordError(RuntimeError):
    """A company user's password is not set; the message names every missing variable."""

    def __init__(self, variables: list[str]) -> None:
        self.variables = variables
        super().__init__("set these in .env (see .env.example): " + ", ".join(variables))


def password_variable(user: str) -> str:
    """The variable holding a company user's password, e.g. ``CLICKHOUSE_PASSWORD_DBT``."""
    return f"CLICKHOUSE_PASSWORD_{user.upper()}"


def user_password(settings: Settings, user: str) -> str | None:
    """A company user's password from the environment or ``.env``; ``None`` when unset."""
    return env_value(password_variable(user), settings.root / ".env")


def require_passwords(settings: Settings, users: list[str]) -> dict[str, str]:
    """Every listed user's password, or one error naming all the missing variables."""
    found = {user: user_password(settings, user) for user in users}
    missing = [password_variable(user) for user, password in found.items() if not password]
    if missing:
        raise MissingPasswordError(missing)
    return {user: password for user, password in found.items() if password}


# ClickHouse settings every toolkit session uses, the same ones dbt's profile
# sets, so SQL means what it means in standard SQL: an outer join returns NULL
# for a missing row (not the type's default), and a bare UNION is UNION DISTINCT.
CLICKHOUSE_SESSION_SETTINGS: dict[str, Any] = {
    "join_use_nulls": 1,
    "union_default_mode": "DISTINCT",
}


def clickhouse_client(
    settings: Settings, user: str | None = None, session: dict[str, Any] | None = None
) -> Any:
    """A clickhouse-connect HTTP client with the toolkit's session settings.

    Without ``user`` it connects as the server's admin, which the toolkit uses
    to set the company up and to read what it measures. With ``user`` (a base
    name such as ``loader``) it connects as that company user, namespaced, with
    that user's own password. ``session`` adds settings to every query the
    client sends, such as the query log tag of ``common.tagging``.
    """
    import clickhouse_connect

    params = clickhouse_params(settings)
    username, password = params.user, params.password
    if user is not None:
        username = settings.company.name(user)
        password = require_passwords(settings, [user])[user]
    return clickhouse_connect.get_client(
        host=params.host,
        port=params.port,
        username=username,
        password=password,
        settings={**CLICKHOUSE_SESSION_SETTINGS, **(session or {})},
    )
