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
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    dbt_target: Path


class SimulationWindow(_Strict):
    history_start: datetime
    start: datetime
    weeks: Annotated[int, Field(ge=1)]
    daily_run_hour: Annotated[int, Field(ge=0, le=23)]

    @model_validator(mode="after")
    def _history_precedes_window(self) -> SimulationWindow:
        if self.history_start >= self.start:
            raise ValueError("simulation.history_start must be before simulation.start")
        return self

    @property
    def duration(self) -> timedelta:
        return timedelta(weeks=self.weeks)

    @property
    def end(self) -> datetime:
        return self.start + self.duration


ActorType = Literal["dbt", "dashboard", "adhoc"]


class ScanRates(_Strict):
    usd_per_tib: Annotated[float, Field(gt=0)]
    minimum_billed_bytes_per_table: Annotated[int, Field(ge=0)]


class ComputeRates(_Strict):
    usd_per_credit: Annotated[float, Field(gt=0)]
    credits_per_hour: Annotated[float, Field(gt=0)]
    minimum_billed_seconds: Annotated[int, Field(ge=0)]
    idle_timeout_seconds: Annotated[int, Field(ge=0)]
    warehouses: dict[ActorType, str]

    @model_validator(mode="after")
    def _every_workload_has_a_warehouse(self) -> ComputeRates:
        missing = {"dbt", "dashboard", "adhoc"} - set(self.warehouses)
        if missing:
            raise ValueError(f"pricing.compute.warehouses is missing {sorted(missing)}")
        return self


class PricingRates(_Strict):
    scan: ScanRates
    compute: ComputeRates


class CostModel(_Strict):
    """Declared assumptions behind modelled compute time (ADR 0005)."""

    throughput_bytes_per_second: Annotated[int, Field(gt=0)]
    per_query_overhead_ms: Annotated[int, Field(ge=0)]


class Recommendations(_Strict):
    unused_lookback_days: list[Annotated[int, Field(gt=0)]]
    hotspot_min_table_bytes: Annotated[int, Field(ge=0)]
    hotspot_min_filtered_reads: Annotated[int, Field(gt=0)]
    incremental_top_n: Annotated[int, Field(gt=0)]


class WorkloadSettings(_Strict):
    real_build_every_days: Annotated[int, Field(ge=1)]
    dashboard_refresh_hours: dict[str, Annotated[int, Field(ge=1, le=24)]]
    adhoc_users: list[str]
    adhoc_max_queries_per_day: Annotated[int, Field(ge=0)]
    adhoc_result_page_rows: Annotated[int, Field(gt=0)]
    product_price_changes_per_day: Annotated[int, Field(ge=0)]
    customer_profile_changes_per_day: Annotated[int, Field(ge=0)]


Tier = Literal["critical", "important", "best_effort"]
TIERS: tuple[Tier, ...] = ("critical", "important", "best_effort")


class TierWeights(_Strict):
    critical: Annotated[int, Field(ge=0)]
    important: Annotated[int, Field(ge=0)]
    best_effort: Annotated[int, Field(ge=0)]

    def weight(self, tier: Tier) -> int:
        value: int = getattr(self, tier)
        return value


class MetadataSettings(_Strict):
    tier_weights: TierWeights


Severity = Literal["SEV1", "SEV2", "SEV3"]
SEVERITIES: tuple[Severity, ...] = ("SEV1", "SEV2", "SEV3")


class SeveritySettings(_Strict):
    root_tier_multiplier: Annotated[int, Field(ge=0)]
    sev1_min_score: Annotated[int, Field(ge=0)]
    sev2_min_score: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _ordered(self) -> SeveritySettings:
        if self.sev2_min_score > self.sev1_min_score:
            raise ValueError("incidents.severity: sev2_min_score must not exceed sev1_min_score")
        return self


class LifecycleSettings(_Strict):
    ack_minutes: dict[Severity, Annotated[int, Field(gt=0)]]
    resolve_hours: dict[Severity, Annotated[int, Field(gt=0)]]
    load_factor: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def _every_severity(self) -> LifecycleSettings:
        for name in ("ack_minutes", "resolve_hours"):
            missing = set(SEVERITIES) - set(getattr(self, name))
            if missing:
                raise ValueError(f"incidents.lifecycle.{name} is missing {sorted(missing)}")
        return self


class IncidentSettings(_Strict):
    scenario_days: Annotated[int, Field(ge=1)]
    fault_hour: Annotated[int, Field(ge=0, le=23)]
    severity: SeveritySettings
    lifecycle: LifecycleSettings


class Settings(_Strict):
    seed: int
    scale_factor: Annotated[float, Field(gt=0)]
    paths: Paths
    simulation: SimulationWindow
    pricing: PricingRates
    cost: CostModel
    recommendations: Recommendations
    workload: WorkloadSettings
    incidents: IncidentSettings
    metadata: MetadataSettings

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
