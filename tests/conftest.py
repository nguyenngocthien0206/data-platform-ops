"""Shared fixtures. Nothing here touches Docker or the network."""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from platform_ops.common.config import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

SETTINGS_YAML = textwrap.dedent(
    """
    seed: 42
    scale_factor: 0.1
    paths:
      duckdb: data/test.duckdb
      reports: reports
      iceberg_warehouse: warehouse
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
        warehouses: {dbt: transform_wh, dashboard: bi_wh, adhoc: adhoc_wh}
    cost:
      throughput_bytes_per_second: 200000000
      per_query_overhead_ms: 150
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
      scenario_days: 21
      fault_hour: 10
      severity: {root_tier_multiplier: 10, sev1_min_score: 150, sev2_min_score: 45}
      lifecycle:
        ack_minutes: {SEV1: 20, SEV2: 90, SEV3: 480}
        resolve_hours: {SEV1: 6, SEV2: 18, SEV3: 40}
        load_factor: 0.5
    reconcile:
      engines: [postgres]
      rows: {customers: 50000, orders: 300000, payments: 300000}
      legacy_year: 2025
      legacy_timezone: America/New_York
      legacy_timezone_windows: Eastern Standard Time
      fanout: 16
      leaf_width: 256
      rounding_tolerance: 0.01
      canonical:
        engines:
          postgres: {rtrim: false, casefold: false}
          sqlserver: {rtrim: false, casefold: true}
          duckdb: {rtrim: false, casefold: false}
        columns: {}
      thresholds:
        min_row_match_rate: 0.999
        min_column_match_rate: 0.999
        max_discrepancies: {missing_in_target: 0, extra_in_target: 0, value_mismatch: 0,
          timezone_shift: 0, whitespace: 0, case_only: 0, rounding: 100}
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


def make_isolated_config(root: Path, scale_factor: float, weeks: int | None = None) -> Path:
    """A throwaway copy of the repo config whose outputs all land under ``root``.

    The real settings, teams and ownership files are used, with the warehouse,
    dbt target and reports redirected into ``root`` and the dbt project pointing
    at the repo's real one. Integration tests drive the actual CLI against this,
    so they exercise exactly what a user runs without touching the real
    warehouse or ``dbt/target``.
    """
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw["scale_factor"] = scale_factor
    if weeks is not None:
        raw["simulation"]["weeks"] = weeks
    raw["paths"] = {
        "duckdb": str(root / "warehouse.duckdb"),
        "reports": str(root / "reports"),
        "iceberg_warehouse": str(root / "iceberg"),
        "dbt_project": str(REPO_ROOT / "dbt"),
        "dbt_target": str(root / "target"),
    }
    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    for name in ("teams.yaml", "ownership.yaml"):
        shutil.copyfile(REPO_ROOT / "config" / name, config_dir / name)
    # Local service credentials (Postgres, SQL Server) live in .env at the root.
    if (REPO_ROOT / ".env").is_file():
        shutil.copyfile(REPO_ROOT / ".env", root / ".env")
    return settings_path


@pytest.fixture(scope="session")
def isolated_config() -> Callable[..., Path]:
    """Expose :func:`make_isolated_config` to session-scoped fixtures."""
    return make_isolated_config
