"""Start Airflow for the company: its metadata database, the admin login, then standalone.

Runs in Airflow's own environment (psycopg2 ships with the image), before
``airflow standalone`` (API server, scheduler, DAG processor and triggerer in one
process tree):

1. creates the ``airflow`` database on the company's Postgres server, beside
   ``legacy``, the toolkit's store and Metabase's, if it is not there yet;
2. points Airflow at it, built from the same ``POSTGRES_*`` variables the
   toolkit reads, so the password is quoted once, here. Airflow asks for it
   with ``start.py --connection`` (``AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_CMD``
   in the Dockerfile), so ``docker exec dpo-airflow airflow ...`` finds the
   same database as the running stack;
3. writes the admin's password from ``AIRFLOW_ADMIN_PASSWORD`` into the simple
   auth manager's passwords file. There is no default: without it Airflow
   would make one up and print it in the log.

A missing password stops it here, named, before Airflow starts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg2
from psycopg2 import sql

DATABASE = "airflow"
ADMIN = "admin"


def _require(name: str, in_env_file: str | None = None) -> str:
    """The variable's value; stops naming the variable to set in .env when it is empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"error: {in_env_file or name} is not set; add it to .env (see .env.example)")
    return value


def _postgres() -> dict[str, str]:
    return {
        "host": os.environ.get("POSTGRES_HOST", "postgres"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "user": os.environ.get("POSTGRES_USER", "platform_ops"),
        "password": _require("POSTGRES_PASSWORD"),
    }


def connection_url() -> str:
    """Airflow's metadata database, as SQLAlchemy wants it."""
    pg = _postgres()
    return (
        f"postgresql+psycopg2://{quote(pg['user'], safe='')}:{quote(pg['password'], safe='')}"
        f"@{pg['host']}:{pg['port']}/{DATABASE}"
    )


def main() -> None:
    admin_password = _require("AIRFLOW_ADMIN_PASSWORD")
    # The company's dbt user, for every dbt task.
    _require("CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD_DBT")

    connection = psycopg2.connect(**_postgres(), dbname="postgres")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [DATABASE])
        if cursor.fetchone() is None:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    connection.close()

    home = Path(os.environ.get("AIRFLOW_HOME", "/opt/airflow"))
    passwords = Path(
        os.environ.get(
            "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE",
            home / "simple_auth_manager_passwords.json.generated",
        )
    )
    passwords.write_text(json.dumps({ADMIN: admin_password}), encoding="utf-8")
    os.execvp("airflow", ["airflow", "standalone"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--connection"]:
        # Read as is by Airflow: no newline, or it is part of the database name.
        sys.stdout.write(connection_url())
    else:
        main()
