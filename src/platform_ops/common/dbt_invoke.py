"""Running dbt in-process.

dbt is invoked through ``dbtRunner`` rather than a subprocess, so the caller
gets structured results back. Every invocation goes through here so that the
warehouse path and the vars always come from ``settings.yaml``: the dbt profile
reads the path from ``PLATFORM_OPS_DUCKDB_PATH`` and has no default of its own,
which keeps a second source of truth from creeping in.
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

DUCKDB_PATH_ENV_VAR = "PLATFORM_OPS_DUCKDB_PATH"


class DbtError(RuntimeError):
    """dbt finished unsuccessfully."""


@dataclass(frozen=True)
class DbtInvocation:
    """Where dbt runs, where it writes, and what it is told."""

    project_dir: Path
    target_path: Path
    log_path: Path
    duckdb_path: Path
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
    settings: Settings, *, simulated_now: datetime | None = None
) -> DbtInvocation:
    """Everything dbt needs, taken from settings.

    ``simulated_now`` is passed to dbt only when given, because only source
    freshness uses it, and dbt re-parses the whole project whenever its vars
    change. Leaving it out of builds keeps their vars constant, so the weekly
    builds of a simulation can reuse dbt's saved parse.
    """
    target = settings.resolve(settings.paths.dbt_target)
    variables: dict[str, Any] = {"scale_factor": settings.scale_factor}
    if simulated_now is not None:
        variables["simulated_now"] = simulated_now.isoformat(sep=" ")
    return DbtInvocation(
        project_dir=settings.resolve(settings.paths.dbt_project),
        target_path=target,
        log_path=target.parent / "logs",
        duckdb_path=settings.resolve(settings.paths.duckdb),
        vars=variables,
    )


@contextmanager
def _warehouse_env(path: Path) -> Iterator[None]:
    previous = os.environ.get(DUCKDB_PATH_ENV_VAR)
    os.environ[DUCKDB_PATH_ENV_VAR] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DUCKDB_PATH_ENV_VAR, None)
        else:
            os.environ[DUCKDB_PATH_ENV_VAR] = previous


def _release_duckdb() -> None:
    """Close the DuckDB database dbt-duckdb keeps open after an invocation.

    dbt-duckdb caches one database handle per process in a class attribute
    (``DuckDBConnectionManager._ENV``, checked against dbt-duckdb 1.11.0) and
    keeps it open after ``dbtRunner.invoke`` returns. While it is open, a
    read-only connection to the same file is refused and no other process can
    open the file. Phases 2 and 3 run dbt and query the warehouse many times in
    one process, so every invocation releases the handle when it finishes. The
    next invocation simply opens a fresh one.
    """
    try:
        from dbt.adapters.duckdb.connections import DuckDBConnectionManager
    except ImportError:  # pragma: no cover - dbt-duckdb is a hard dependency
        return
    environment = getattr(DuckDBConnectionManager, "_ENV", None)
    if environment is not None:
        environment.close()
    DuckDBConnectionManager.close_all_connections()  # type: ignore[no-untyped-call]


def run_dbt(
    invocation: DbtInvocation, command: list[str], *, check: bool = True, manifest: Any = None
) -> Any:
    """Run one dbt command. Returns the dbt result object; raises on failure if ``check``.

    ``manifest`` is a parsed manifest (``run_dbt(..., ["parse"]).result``) to reuse
    instead of parsing again. It saved about 0.8 s of 1.9 s per targeted run on
    the development laptop. Only pass one parsed with the same vars.
    """
    from dbt.cli.main import dbtRunner

    invocation.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    with _warehouse_env(invocation.duckdb_path):
        try:
            outcome = dbtRunner(manifest=manifest).invoke(invocation.args(command))
        finally:
            _release_duckdb()
    if check and not outcome.success:
        detail = f": {outcome.exception}" if outcome.exception else ""
        raise DbtError(f"dbt {' '.join(command)} failed{detail}")
    return outcome
