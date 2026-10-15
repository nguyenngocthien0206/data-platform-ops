"""Start Dagster for the company: its storage database, then ``dagster dev``.

Runs in Dagster's own environment (psycopg2 comes with dagster-postgres),
before ``dagster dev`` (the webserver and the daemon in one process tree, the
way ``airflow standalone`` runs Airflow):

1. creates the ``dagster`` database on the company's Postgres server, beside
   ``legacy``, the toolkit's store, Metabase's and Airflow's, if it is not there
   yet; ``dagster.yaml`` reads the same ``POSTGRES_*`` variables;
2. checks the company's dbt user has its password.

A missing password stops it here, named, before Dagster starts. Dagster's open
source UI has no login: publish its port on your own machine only.
"""

from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2 import sql

DATABASE = "dagster"


def _require(name: str, in_env_file: str | None = None) -> str:
    """The variable's value; stops naming the variable to set in .env when it is empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"error: {in_env_file or name} is not set; add it to .env (see .env.example)")
    return value


def main() -> None:
    password = _require("POSTGRES_PASSWORD")
    # The company's dbt user, for every dbt invocation.
    _require("CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD_DBT")
    connection = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=5432,
        user=os.environ.get("POSTGRES_USER", "platform_ops"),
        password=password,
        dbname="postgres",
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [DATABASE])
        if cursor.fetchone() is None:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    connection.close()
    os.execvp(
        "dagster",
        ["dagster", "dev", "--host", "0.0.0.0", "--port", "3000", "-w", "workspace.yaml"],
    )


if __name__ == "__main__":
    main()
