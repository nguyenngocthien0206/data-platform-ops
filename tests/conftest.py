"""Shared fixtures. Nothing here touches Docker or the network."""

from __future__ import annotations

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
    simulation:
      start: 2026-01-05T00:00:00
      weeks: 2
      daily_run_hour: 2
    pricing:
      scan:
        usd_per_tib: 6.25
      compute:
        usd_per_credit: 2.0
        credits_per_hour: 1.0
        minimum_billed_seconds: 60
        idle_timeout_seconds: 300
    recommendations:
      unused_lookback_days: [30, 90]
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
