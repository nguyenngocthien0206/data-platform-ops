"""Loading and validation of config/settings.yaml.

Every module reads run parameters through :func:`load_settings` rather than
opening YAML itself. That keeps one validated view of scale, seed, paths and
pricing, so a change to a rate or the simulated window lands everywhere at once.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")
CONFIG_PATH_ENV_VAR = "PLATFORM_OPS_CONFIG"
# Overrides scale_factor for one run, for a check that needs the behaviour and
# not the numbers (the release workflow's demo). Results are always quoted at
# the scale in config/settings.yaml.
SCALE_ENV_VAR = "PLATFORM_OPS_SCALE_FACTOR"
# Runs another company from the same config, in its own namespace (Phase 14b):
# each orchestrator stack runs its own, so `make demo` is never touched.
NAMESPACE_ENV_VAR = "PLATFORM_OPS_NAMESPACE"


class _Strict(BaseModel):
    """Reject unknown keys so a typo in YAML fails loudly instead of silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(_Strict):
    duckdb: Path
    # Version 2.0: the generator's scratch database. The raw data is still
    # generated in DuckDB, then copied into the company's legacy Postgres.
    generator: Path = Path("data/generator.duckdb")
    reports: Path
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


# Who ran a query, by the kind of work (version 2.0 adds the pipelines and the
# migration's own load): dbt builds and tests, the ingestion, the platform's
# maintenance (OPTIMIZE), the parallel run's reconciliation, BI refreshes, and
# people's ad hoc SQL.
ActorType = Literal["dbt", "ingestion", "maintenance", "reconcile", "dashboard", "adhoc"]
ACTOR_TYPES: tuple[ActorType, ...] = (
    "dbt",
    "ingestion",
    "maintenance",
    "reconcile",
    "dashboard",
    "adhoc",
)


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
        missing = set(ACTOR_TYPES) - set(self.warehouses)
        if missing:
            raise ValueError(f"pricing.compute.warehouses is missing {sorted(missing)}")
        return self


class PricingRates(_Strict):
    scan: ScanRates
    compute: ComputeRates


class StatedCost(_Strict):
    """What a piece of infrastructure costs a month, from public list prices."""

    nodes: Annotated[int, Field(ge=1)] = 1
    instance_usd_per_month: Annotated[float, Field(gt=0)]
    storage_gb: Annotated[int, Field(ge=0)]
    storage_usd_per_gb_month: Annotated[float, Field(ge=0)]

    @property
    def compute_usd(self) -> float:
        return self.nodes * self.instance_usd_per_month

    @property
    def storage_usd(self) -> float:
        return self.nodes * self.storage_gb * self.storage_usd_per_gb_month


class CostModel(_Strict):
    """Declared assumptions behind modelled compute time (ADR 0004), and the stated
    monthly cost of the company's ClickHouse and of the legacy reporting it replaces."""

    throughput_bytes_per_second: Annotated[int, Field(gt=0)]
    per_query_overhead_ms: Annotated[int, Field(ge=0)]
    clickhouse: StatedCost
    legacy_reporting: StatedCost


class Recommendations(_Strict):
    unused_lookback_days: list[Annotated[int, Field(gt=0)]]
    hotspot_min_table_bytes: Annotated[int, Field(ge=0)]
    hotspot_min_filtered_reads: Annotated[int, Field(gt=0)]
    incremental_top_n: Annotated[int, Field(gt=0)]


class DashboardViews(_Strict):
    """How often people open a dashboard on a working day, in percent.

    Each dashboard also has a popularity of its own (none, half, normal or one
    and a half), from the seed, so some are opened by nobody.
    """

    own_team_percent: Annotated[int, Field(ge=0, le=100)]
    other_team_percent: Annotated[int, Field(ge=0, le=100)]


class WorkloadSettings(_Strict):
    real_build_every_days: Annotated[int, Field(ge=1)]
    dashboard_refresh_hours: dict[str, Annotated[int, Field(ge=1, le=24)]]
    dashboard_views: DashboardViews
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


class EscalationSettings(_Strict):
    # An incident that stays at a severity this long is raised a level (SEV1 is
    # the top, so it has no entry).
    after_hours: dict[Severity, Annotated[int, Field(gt=0)]]


class PagingSettings(_Strict):
    # An owner who already has this many incidents open is busy: the next page
    # goes to the rule's backup, when it names one.
    max_open_per_person: Annotated[int, Field(ge=1)]


class IncidentSettings(_Strict):
    # Days into the production weeks on which faults happen, two at a time
    # (simulation.faults.CATALOGUE, in order); days past the window are dropped.
    fault_days: list[Annotated[int, Field(ge=0)]] = Field(default_factory=lambda: [2, 5, 9, 14])
    fault_hour: Annotated[int, Field(ge=0, le=23)]
    severity: SeveritySettings
    lifecycle: LifecycleSettings
    paging: PagingSettings
    escalation: EscalationSettings


# The legacy engine whose policy decides what counts as equal (ADR 0006).
Engine = Literal["postgres"]
DiscrepancyClass = Literal[
    "missing_in_target",
    "extra_in_target",
    "duplicate_key",
    "rounding",
    "timezone_shift",
    "whitespace",
    "case_only",
    "value_mismatch",
]
DISCREPANCY_CLASSES: tuple[DiscrepancyClass, ...] = (
    "missing_in_target",
    "extra_in_target",
    "duplicate_key",
    "rounding",
    "timezone_shift",
    "whitespace",
    "case_only",
    "value_mismatch",
)


class CanonicalRule(_Strict):
    """How one column, or every text column of one engine, is rendered before hashing."""

    rtrim: bool | None = None
    casefold: bool | None = None
    scale: Annotated[int, Field(ge=0, le=18)] | None = None


class CanonicalSettings(_Strict):
    engines: dict[Engine, CanonicalRule]
    columns: dict[str, CanonicalRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _columns_are_table_dot_column(self) -> CanonicalSettings:
        bad = [key for key in self.columns if key.count(".") != 1]
        if bad:
            raise ValueError(f"reconcile.canonical.columns keys must be table.column: {bad}")
        return self


class Thresholds(_Strict):
    min_row_match_rate: Annotated[float, Field(ge=0, le=1)]
    min_column_match_rate: Annotated[float, Field(ge=0, le=1)]
    max_discrepancies: dict[DiscrepancyClass, Annotated[int, Field(ge=0)]]
    # How far behind the comparison the legacy side may be: the copy's
    # watermark for a table, the frozen snapshot for a report.
    max_watermark_lag_hours: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _every_class(self) -> Thresholds:
        missing = set(DISCREPANCY_CLASSES) - set(self.max_discrepancies)
        if missing:
            raise ValueError(f"reconcile.thresholds.max_discrepancies is missing {sorted(missing)}")
        return self


class ReconcileSettings(_Strict):
    """How the parallel run is reconciled and judged (version 2.0, Phase 12)."""

    # Pairs reconciled at the same time; each holds a Postgres and a ClickHouse
    # connection, and Postgres renders the legacy side.
    workers: Annotated[int, Field(ge=1, le=32)] = 6
    leaf_width: Annotated[int, Field(ge=1)]
    rounding_tolerance: Annotated[float, Field(ge=0)]
    # The time zones the legacy system's applications run in. A difference is
    # a time zone shift only when one side is the other read as local time in
    # one of these; any other whole-hour difference is a value mismatch.
    time_zones: list[str] = Field(default_factory=list)
    # Consecutive nights with every table and report within the thresholds
    # before the cutover is recommended.
    clean_nights_for_cutover: Annotated[int, Field(ge=1)]
    # Differences kept per night, pair and class as examples; the counts are
    # always complete.
    examples_per_class: Annotated[int, Field(ge=0)] = 20
    canonical: CanonicalSettings
    thresholds: Thresholds

    @field_validator("time_zones")
    @classmethod
    def _known_zones(cls, zones: list[str]) -> list[str]:
        for zone in zones:
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError(f"reconcile.time_zones: unknown time zone {zone!r}") from error
        return zones


class DeliveredJob(_Strict):
    """The ingestion job as the migration team first delivered it, with four defects.

    Each is the kind a real copy job ships with, and each is reconciled away by
    the fix (``company.fix_after_nights``).
    """

    # Reads a UTC timestamp's wall-clock text as local time in this zone.
    timezone_table: str = "payments"
    timezone_column: str = "created_at"
    timezone: str = "America/New_York"
    # Passes a decimal through a float, and the decimal cast back truncates.
    float_table: str = "payments"
    float_column: str = "amount"
    # Trims trailing spaces off a text column.
    trim_table: str = "customers"
    trim_column: str = "email"
    # Pages a table by key and loses the last key of every page. On web sessions
    # no dbt test notices (nothing references a session), which is why only the
    # reconciliation finds it. The key must be dense for pages to have a last key.
    batch_table: str = "web_sessions"
    batch_rows: Annotated[int, Field(ge=2)] = 25000


class CompanySettings(_Strict):
    """Version 2.0: the simulated company on legacy Postgres and ClickHouse.

    Connection details (hosts, ports, users, passwords) come from the
    environment or ``.env``, never from this file. Every schema and database
    name below is prefixed with ``namespace``, so tests can run side by side
    with the demo on the same servers without touching its data.
    """

    namespace: str = ""
    # Legacy Postgres: operational tables, and the hand-written reports.
    legacy_schema: str = "app"
    reporting_schema: str = "reporting"
    # The reports frozen every night of the parallel run, as of the copy's
    # watermark, for the reconciliation to compare (reconcile.asof).
    frozen_reporting_schema: str = "reporting_asof"
    # ClickHouse: the raw layer the ingestion fills; dbt's own databases are
    # named after its layers (staging, intermediate, marts).
    raw_database: str = "raw"
    # The toolkit's own results, in a Postgres database next to the legacy one.
    ops_database: str = "platform_ops"
    ops_schema: str = "ops"
    # How ClickHouse reaches Postgres. The postgresql() table function runs on
    # the ClickHouse server, so this is Postgres's address inside Docker.
    postgres_from_clickhouse: str = "postgres:5432"
    # Each load re-reads this much before the watermark, so a row committed
    # late with an older updated_at is not lost; ReplacingMergeTree keeps the
    # latest version of anything read twice.
    ingest_overlap_minutes: Annotated[int, Field(ge=0)] = 60
    # dbt builds on this cadence over the simulated window, and on its last day.
    build_every_days: Annotated[int, Field(ge=1)] = 28
    # Metabase (Phase 11b) keeps its own state in this Postgres database, and
    # reaches ClickHouse at this address inside Docker. Its URL and admin login
    # come from the environment (METABASE_URL, MB_ADMIN_EMAIL, MB_ADMIN_PASSWORD).
    metabase_database: str = "metabase"
    clickhouse_from_metabase: str = "clickhouse:8123"
    # The parallel run (Phase 12): the weeks before production, when legacy and
    # ClickHouse both run, dbt builds every night and the migration is
    # reconciled every night. Zero means no parallel run.
    parallel_run_weeks: Annotated[int, Field(ge=0)] = 0
    # Production (Phase 14): the last weeks of the window, after the parallel
    # run, when ClickHouse is what the business runs on. Every night runs source
    # freshness, the sources' tests, run, snapshot and the other tests, faults
    # happen in operation, and the incident desk ingests what dbt reports. Zero
    # means no production weeks.
    production_weeks: Annotated[int, Field(ge=0)] = 0
    # Nights into the parallel run when the migration team ships the fixed job
    # and backfills. Until then the job runs as delivered.
    fix_after_nights: Annotated[int, Field(ge=1)] = 7
    # During the parallel run the application keeps writing after the copy has
    # read it: the next day's first minutes of new rows reach Postgres before
    # the reconciliation runs. Zero writes nothing between them.
    writes_after_ingest_minutes: Annotated[int, Field(ge=0)] = 0
    delivered_job: DeliveredJob = Field(default_factory=lambda: DeliveredJob())

    def name(self, base: str) -> str:
        """A schema or database name with the namespace applied."""
        return f"{self.namespace}{base}"


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
    reconcile: ReconcileSettings
    metadata: MetadataSettings
    company: CompanySettings = Field(default_factory=CompanySettings)

    # Directory the config file was found in, used to resolve relative paths.
    root: Path = Field(default=Path(), exclude=True)

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
    ``PLATFORM_OPS_SCALE_FACTOR``, when set, replaces the file's scale factor,
    and ``PLATFORM_OPS_NAMESPACE`` the company's namespace.
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
    scale = os.environ.get(SCALE_ENV_VAR, "").strip()
    if scale:
        raw["scale_factor"] = float(scale)
    namespace = os.environ.get(NAMESPACE_ENV_VAR, "").strip()
    if namespace:
        raw["company"] = {**(raw.get("company") or {}), "namespace": namespace}
    return Settings.model_validate(raw)
