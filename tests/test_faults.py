"""Every fault breaks the raw data, and its repair puts back exactly what was there."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.db import connect
from platform_ops.simulation import faults, raw_data
from platform_ops.simulation.faults import CATALOGUE, Fault

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def small() -> Settings:
    base = load_settings(REPO_ROOT / "config" / "settings.yaml")
    return base.model_copy(update={"scale_factor": 0.01})


def _rows(connection: duckdb.DuckDBPyConnection, table: str) -> list[tuple[Any, ...]]:
    columns = ", ".join(raw_data.generated_columns(table))
    key = raw_data.PRIMARY_KEYS[table]
    return connection.execute(
        f"SELECT {columns} FROM raw.{table} ORDER BY {key}, _loaded_at"
    ).fetchall()


def _loads(settings: Settings, days: int) -> list[tuple[Any, Any]]:
    """Daily load windows ending at each day's 02:00 run."""
    start = settings.simulation.start
    runs = [start + timedelta(days=d, hours=2) for d in range(days + 1)]
    return list(zip([start, *runs[:-1]], runs, strict=True))


@pytest.mark.parametrize("fault", CATALOGUE, ids=[f.fault_id for f in CATALOGUE])
def test_inject_then_repair_restores_the_raw_data(small: Settings, fault: Fault) -> None:
    plan = raw_data.RawDataPlan.from_settings(small)
    connection = connect(path=":memory:")
    raw_data.seed(connection, small)
    faults.reset_ground_truth(connection)
    windows = _loads(small, 4)

    stalled_since = None
    for day, (after, until) in enumerate(windows):
        tables = [t for t in raw_data.TABLES if not (stalled_since and t == fault.table)]
        raw_data.load_window(connection, plan, after, until, tables=tables)
        if day == 1:
            injected_at = until + timedelta(hours=8)
            affected = faults.inject(connection, fault, injected_at, small.seed)
            if fault.fault_type == "stale_source":
                stalled_since = until
            else:
                assert affected > 0 or fault.fault_type.startswith("schema"), "fault did nothing"

    loaded_until = windows[-1][1]
    faults.repair(connection, plan, fault, loaded_until, loaded_until, stalled_since)

    clean = connect(path=":memory:")
    raw_data.create_raw_tables(clean)
    raw_data.load_window(clean, plan, None, loaded_until)
    for table in raw_data.TABLES:
        assert _rows(connection, table) == _rows(clean, table), f"{fault.fault_id}: {table}"

    (repaired,) = connection.execute(
        "SELECT repaired_at FROM ops.fault_ground_truth WHERE fault_id = ?", [fault.fault_id]
    ).fetchone() or (None,)
    assert repaired == loaded_until


def test_catalogue_covers_every_fault_type_in_the_spec() -> None:
    kinds = {f.fault_type for f in CATALOGUE}
    assert {"null_spike", "duplicate_keys", "stale_source", "volume_drop",
            "invalid_category"} <= kinds  # fmt: skip
    assert kinds & {"schema_drop", "schema_rename"}, "schema change"


def test_fault_ids_are_unique_and_days_ordered() -> None:
    ids = [f.fault_id for f in CATALOGUE]
    assert len(ids) == len(set(ids))
    assert [f.day for f in CATALOGUE] == sorted(f.day for f in CATALOGUE)
