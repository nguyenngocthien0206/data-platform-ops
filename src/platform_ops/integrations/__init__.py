"""Hooks for the orchestrators that run dbt: hand each invocation to the incident desk.

Standard library only, and no import of Airflow or Dagster (Phase 14b, ADR 0020).
An orchestrator calls these with what it already has: where dbt wrote its
artifacts, and the id of the scheduled run they belong to. They run
``platform-ops incidents ingest`` from the toolkit's own environment, so the
toolkit's dependencies never meet the orchestrator's, and a failure to ingest
is logged and never raised: incident tracking must not fail a pipeline run.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The command that runs the toolkit's CLI, e.g. /opt/toolkit/bin/platform-ops.
CLI_ENV_VAR = "PLATFORM_OPS_CLI"
# The consuming repository's registry folder (teams, ownership, incidents.yaml).
REGISTRY_ENV_VAR = "PLATFORM_OPS_REGISTRY_DIR"
DEFAULT_CLI = "platform-ops"
TIMEOUT_SECONDS = 300
_TAIL = 500

logger = logging.getLogger("platform_ops.integrations")


@dataclass(frozen=True)
class IngestOutcome:
    ok: bool
    command: tuple[str, ...]
    output: str


def _cli(cli: Sequence[str] | None, environ: Mapping[str, str]) -> list[str]:
    if cli is not None:
        return list(cli)
    return shlex.split(environ.get(CLI_ENV_VAR, "").strip() or DEFAULT_CLI)


def _when(at: datetime) -> str:
    """The CLI's format, as a naive UTC time (the store's convention)."""
    if at.tzinfo is not None:
        at = at.astimezone(UTC).replace(tzinfo=None)
    return at.strftime("%Y-%m-%dT%H:%M:%S")


def ingest(
    target: str | Path,
    run_id: str,
    *,
    at: datetime | None = None,
    registry_dir: str | Path | None = None,
    cli: Sequence[str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> IngestOutcome:
    """Run ``platform-ops incidents ingest`` on one dbt target folder; never raises.

    ``at`` defaults to when dbt wrote each file. ``registry_dir`` (or
    ``PLATFORM_OPS_REGISTRY_DIR``) points at the consuming repository's
    registry. The settings, namespace and passwords come from the environment,
    as for any toolkit command.
    """
    env = dict(os.environ if environ is None else environ)
    command = [
        *_cli(cli, env),
        "incidents",
        "ingest",
        "--target",
        str(target),
        "--run-id",
        run_id,
    ]
    if at is not None:
        command += ["--at", _when(at)]
    registry = registry_dir or env.get(REGISTRY_ENV_VAR, "").strip()
    if registry:
        command += ["--registry-dir", str(registry)]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("incident ingest for %s could not run: %s", run_id, error)
        return IngestOutcome(False, tuple(command), str(error))
    output = (done.stdout + done.stderr).strip()
    if done.returncode != 0:
        logger.warning(
            "incident ingest for %s failed (exit %s): %s", run_id, done.returncode, output[-_TAIL:]
        )
        return IngestOutcome(False, tuple(command), output)
    logger.info("%s", done.stdout.strip()[-_TAIL:])
    return IngestOutcome(True, tuple(command), output)
