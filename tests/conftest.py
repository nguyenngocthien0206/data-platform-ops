"""Shared fixtures.

Most of them touch nothing outside the test's temp directory. The company
fixtures (version 2.0) need the Postgres and ClickHouse servers (``make up``);
they skip with the reason when a server does not answer, and keep to their own
namespace, so they never touch the demo's data.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.sandbox import write_isolated_config

REPO_ROOT = Path(__file__).resolve().parent.parent

SETTINGS_YAML = textwrap.dedent(
    """
    seed: 42
    scale_factor: 0.1
    paths:
      duckdb: data/test.duckdb
      reports: reports
      dbt_project: dbt
      dbt_target: dbt/target
    simulation:
      history_start: 2025-12-01T00:00:00
      start: 2026-01-05T00:00:00
      weeks: 2
      daily_run_hour: 2
    pricing:
      scan:
        usd_per_tib: 6.25
        minimum_billed_bytes_per_table: 10485760
      compute:
        usd_per_credit: 2.0
        credits_per_hour: 1.0
        minimum_billed_seconds: 60
        idle_timeout_seconds: 300
        warehouses: {dbt: transform_wh, ingestion: transform_wh, maintenance: transform_wh,
          reconcile: transform_wh, dashboard: bi_wh, adhoc: adhoc_wh}
    cost:
      throughput_bytes_per_second: 200000000
      per_query_overhead_ms: 150
      clickhouse: {nodes: 1, instance_usd_per_month: 280.32, storage_gb: 500,
        storage_usd_per_gb_month: 0.08}
      legacy_reporting: {nodes: 1, instance_usd_per_month: 520.0, storage_gb: 500,
        storage_usd_per_gb_month: 0.115}
    recommendations:
      unused_lookback_days: [30, 90]
      hotspot_min_table_bytes: 20000000
      hotspot_min_filtered_reads: 10
      incremental_top_n: 15
    workload:
      real_build_every_days: 7
      dashboard_refresh_hours: {high: 6, medium: 12, low: 24}
      adhoc_users: [sam, maya]
      adhoc_max_queries_per_day: 3
      adhoc_result_page_rows: 500
      product_price_changes_per_day: 6
      customer_profile_changes_per_day: 40
    incidents:
      fault_days: [2, 5, 9, 14]
      fault_hour: 10
      severity: {root_tier_multiplier: 10, sev1_min_score: 150, sev2_min_score: 45}
      lifecycle:
        ack_minutes: {SEV1: 20, SEV2: 90, SEV3: 480}
        resolve_hours: {SEV1: 6, SEV2: 18, SEV3: 40}
        load_factor: 0.5
    reconcile:
      workers: 4
      leaf_width: 256
      rounding_tolerance: 0.01
      clean_nights_for_cutover: 3
      examples_per_class: 5
      canonical:
        engines:
          postgres: {rtrim: false, casefold: false}
        columns: {}
      thresholds:
        min_row_match_rate: 0.999
        min_column_match_rate: 0.999
        max_discrepancies: {missing_in_target: 0, extra_in_target: 0, duplicate_key: 0,
          value_mismatch: 0, timezone_shift: 0, whitespace: 0, case_only: 0, rounding: 100}
    metadata:
      tier_weights:
        critical: 3
        important: 2
        best_effort: 1
    """
).strip()


@pytest.fixture
def settings_dict() -> dict[str, object]:
    """The valid settings template, parsed, for tests that mutate a single key."""
    parsed: dict[str, object] = yaml.safe_load(SETTINGS_YAML)
    return parsed


@pytest.fixture
def write_settings(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """Write a settings mapping into a repo-shaped temp dir and return its path."""

    def _write(raw: dict[str, object]) -> Path:
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        path = config_dir / "settings.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def settings_file(write_settings: Callable[[dict[str, object]], Path]) -> Path:
    """A valid settings file on disk."""
    return write_settings(yaml.safe_load(SETTINGS_YAML))


@pytest.fixture
def settings(settings_file: Path) -> Settings:
    return load_settings(settings_file)


@pytest.fixture
def repo_settings() -> Settings:
    """The real config/settings.yaml that ships with the repo."""
    return load_settings(REPO_ROOT / "config" / "settings.yaml")


def make_isolated_config(
    root: Path,
    scale_factor: float,
    weeks: int | None = None,
    namespace: str = "",
    overrides: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """A throwaway copy of the repo config whose outputs all land under ``root``.

    See :func:`platform_ops.common.sandbox.write_isolated_config`. Integration
    tests drive the actual CLI against it, so they exercise exactly what a user
    runs without touching the real warehouse or ``dbt/target``.
    """
    return write_isolated_config(
        root, scale_factor, weeks, namespace=namespace, overrides=overrides
    )


@pytest.fixture(scope="session")
def isolated_config() -> Callable[..., Path]:
    """Expose :func:`make_isolated_config` to session-scoped fixtures."""
    return make_isolated_config


def require_servers(settings: Settings) -> None:
    """Skip, with the reason, unless the company's Postgres and ClickHouse both answer."""
    from platform_ops.common.servers import clickhouse_client, connect_postgres

    try:
        connect_postgres(settings).close()
    except Exception as error:
        pytest.skip(f"Postgres not reachable ({type(error).__name__}); run `make up`")
    try:
        clickhouse_client(settings).close()
    except Exception as error:
        pytest.skip(f"ClickHouse not reachable ({type(error).__name__}); run `make up`")


@pytest.fixture(scope="session")
def servers_or_skip() -> Callable[[Settings], None]:
    """Expose :func:`require_servers` to module fixtures that build their own company."""
    return require_servers


# The demo's parallel run is six weeks and its production three; the suite's
# are one week each, so its company still goes through every stage: the
# delivered job, the fix and backfill, a one-off fault and its repair, a
# cutover decision, then two nights of faults in production, their incidents
# and their resolution.
SHORT_PARALLEL_RUN: dict[str, dict[str, Any]] = {
    "company": {"parallel_run_weeks": 1, "fix_after_nights": 2, "production_weeks": 1},
    "reconcile": {"clean_nights_for_cutover": 2, "workers": 4},
    "incidents": {"fault_days": [0, 1]},
}


@pytest.fixture(scope="session")
def short_parallel_run() -> dict[str, dict[str, Any]]:
    """The overrides the shared company runs with, for tests that build another."""
    return SHORT_PARALLEL_RUN


@pytest.fixture(scope="session")
def company_warehouse(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """The company run the way ``make demo`` runs it, at scale 0.01 over three weeks, shared.

    Through the real CLI: ``company run`` (seed Postgres, backfill ClickHouse,
    then each day's data, ingestion and a dbt build on the first day; the
    second week is the parallel run, built and reconciled every night; the third
    is production, with four faults in operation and the incident desk ingesting
    every night), then ``metadata check``, ``incidents run`` and ``cost report``.
    Every test that only reads a built company uses this one, because it is the
    most expensive thing the suite builds.
    """
    from typer.testing import CliRunner

    from platform_ops.cli import app
    from platform_ops.common.config import CONFIG_PATH_ENV_VAR
    from platform_ops.simulation.company import remove_company

    root = tmp_path_factory.mktemp("company")
    settings_path = make_isolated_config(
        root, 0.01, weeks=3, namespace="t_suite_", overrides=SHORT_PARALLEL_RUN
    )
    settings = load_settings(settings_path)
    require_servers(settings)
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    runner = CliRunner()
    try:
        for command in (
            ["company", "run"],
            ["metadata", "check"],
            ["incidents", "run"],
            ["cost", "report"],
        ):
            result = runner.invoke(app, command)
            assert result.exit_code == 0, f"{' '.join(command)} failed:\n{result.output}"
    finally:
        # Restored before yielding: a session fixture must not leave its config
        # in the environment for every test that runs after it.
        patch.undo()
    yield settings
    remove_company(settings)
