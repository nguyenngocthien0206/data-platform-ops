"""Running dbt in-process, against the company's ClickHouse (version 2.0).

dbt is invoked through ``dbtRunner`` rather than a subprocess, so the caller
gets structured results back. Every invocation goes through here so that the
connection and the vars always come from the toolkit's settings: the dbt
profile reads the ClickHouse connection from ``CLICKHOUSE_*`` variables, which
this module sets from ``common.servers`` for the length of the invocation, so
dbt and the toolkit can never point at different servers.

dbt connects as the company's ``dbt`` user (``simulation.access``), so its
queries are logged under it. When that user's password is not set, the
invocation keeps the admin connection: enough for ``dbt parse``, which never
connects, as the ownership check in CI does. Anything that builds creates the
users first, and fails there with the missing variable named.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from platform_ops.common.config import Settings


class DbtError(RuntimeError):
    """dbt finished unsuccessfully."""


@dataclass(frozen=True)
class DbtInvocation:
    """Where dbt runs, where it writes, what it connects to, and what it is told."""

    project_dir: Path
    target_path: Path
    log_path: Path
    environment: dict[str, str] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.target_path / "manifest.json"

    def args(self, command: list[str]) -> list[str]:
        return [
            *command,
            "--project-dir",
            str(self.project_dir),
            "--profiles-dir",
            str(self.project_dir),
            "--target-path",
            str(self.target_path),
            "--log-path",
            str(self.log_path),
            "--vars",
            json.dumps(self.vars, sort_keys=True),
        ]


def invocation_from_settings(
    settings: Settings, *, simulated_now: datetime | None = None, log_comment: str = ""
) -> DbtInvocation:
    """Everything dbt needs, taken from settings.

    ``simulated_now`` is passed to dbt only when given, because only source
    freshness uses it, and dbt re-parses the whole project whenever its vars
    change. Leaving it out of builds keeps their vars constant, so the builds of
    a simulation can reuse dbt's saved parse. ``namespace`` prefixes every
    database dbt reads and writes, so a test never touches the demo's data.
    """
    from platform_ops.common.servers import clickhouse_params, user_password

    target = settings.resolve(settings.paths.dbt_target)
    variables: dict[str, Any] = {
        "scale_factor": settings.scale_factor,
        "namespace": settings.company.namespace,
    }
    if simulated_now is not None:
        variables["simulated_now"] = simulated_now.isoformat(sep=" ")
    params = clickhouse_params(settings)
    user, password = params.user, params.password
    dbt_password = user_password(settings, "dbt")
    if dbt_password:
        user, password = settings.company.name("dbt"), dbt_password
    return DbtInvocation(
        project_dir=settings.resolve(settings.paths.dbt_project),
        target_path=target,
        log_path=target.parent / "logs",
        environment={
            "CLICKHOUSE_HOST": params.host,
            "CLICKHOUSE_PORT": str(params.port),
            "CLICKHOUSE_USER": user,
            "CLICKHOUSE_PASSWORD": password,
            # The query log tag of every statement dbt runs (common.tagging).
            "PLATFORM_OPS_LOG_COMMENT": log_comment,
        },
        vars=variables,
    )


@contextmanager
def _environment(values: dict[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_dbt(
    invocation: DbtInvocation, command: list[str], *, check: bool = True, manifest: Any = None
) -> Any:
    """Run one dbt command. Returns the dbt result object; raises on failure if ``check``.

    ``manifest`` is a parsed manifest (``run_dbt(..., ["parse"]).result``) to reuse
    instead of parsing again. Only pass one parsed with the same vars.
    """
    from dbt.cli.main import dbtRunner

    invocation.target_path.mkdir(parents=True, exist_ok=True)
    with _environment(invocation.environment):
        outcome = dbtRunner(manifest=manifest).invoke(invocation.args(command))
    if check and not outcome.success:
        detail = f": {outcome.exception}" if outcome.exception else ""
        raise DbtError(f"dbt {' '.join(command)} failed{detail}")
    return outcome
