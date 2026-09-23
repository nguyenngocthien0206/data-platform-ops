"""Loading and validation of config/settings.yaml.

Every module reads run parameters through :func:`load_settings` rather than
opening YAML itself. That keeps one validated view of scale, seed, paths and
pricing, so a change to a rate or the simulated window lands everywhere at once.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")
CONFIG_PATH_ENV_VAR = "PLATFORM_OPS_CONFIG"


class _Strict(BaseModel):
    """Reject unknown keys so a typo in YAML fails loudly instead of silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(_Strict):
    duckdb: Path
    reports: Path
    iceberg_warehouse: Path
    dbt_project: Path


class SimulationWindow(_Strict):
    start: datetime
    weeks: Annotated[int, Field(ge=1)]
    daily_run_hour: Annotated[int, Field(ge=0, le=23)]

    @property
    def duration(self) -> timedelta:
        return timedelta(weeks=self.weeks)

    @property
    def end(self) -> datetime:
        return self.start + self.duration


class ScanPricing(_Strict):
    usd_per_tib: Annotated[float, Field(gt=0)]


class ComputePricing(_Strict):
    usd_per_credit: Annotated[float, Field(gt=0)]
    credits_per_hour: Annotated[float, Field(gt=0)]
    minimum_billed_seconds: Annotated[int, Field(ge=0)]
    idle_timeout_seconds: Annotated[int, Field(ge=0)]


class PricingRates(_Strict):
    scan: ScanPricing
    compute: ComputePricing


class Recommendations(_Strict):
    unused_lookback_days: list[Annotated[int, Field(gt=0)]]


class Settings(_Strict):
    seed: int
    scale_factor: Annotated[float, Field(gt=0)]
    paths: Paths
    simulation: SimulationWindow
    pricing: PricingRates
    recommendations: Recommendations

    # Directory the config file was found in, used to resolve relative paths.
    root: Path = Field(default=Path("."), exclude=True)

    def resolve(self, path: Path) -> Path:
        """Turn a configured relative path into an absolute one under the repo root."""
        return path if path.is_absolute() else (self.root / path)


def _config_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    from_env = os.environ.get(CONFIG_PATH_ENV_VAR)
    return Path(from_env) if from_env else DEFAULT_CONFIG_PATH


def load_settings(path: Path | str | None = None) -> Settings:
    """Read, validate and return the settings.

    Resolution order: explicit argument, then ``PLATFORM_OPS_CONFIG``, then
    ``config/settings.yaml`` relative to the working directory.
    """
    config_file = _config_path(path)
    if not config_file.is_file():
        raise FileNotFoundError(
            f"No settings file at {config_file}. "
            f"Pass a path, or set {CONFIG_PATH_ENV_VAR}, or run from the repo root."
        )
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_file} must contain a YAML mapping at the top level")
    # The repo root is the parent of config/, which is where relative paths hang off.
    raw["root"] = config_file.resolve().parent.parent
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings for callers that do not want to thread them."""
    return load_settings()
