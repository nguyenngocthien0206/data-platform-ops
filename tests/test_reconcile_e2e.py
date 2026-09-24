"""End to end: the migration scenario and its sign-off (SPEC Phase 4 acceptance).

Three runs at scale 0.01:

- DuckDB as the legacy engine, so the whole scenario is exercised without
  Docker (and in CI): every planted discrepancy is found and correctly
  classified, results land in ``ops``, and two runs write the same report;
- Postgres through the real CLI, as ``make reconcile`` runs it (skipped when
  Postgres is not reachable);
- SQL Server, whose case-insensitive policy must treat the planted case change
  as equal (skipped without the profile and driver).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.reconcile.run import ReconcileError, ReconcileResult, run_and_report

pytestmark = pytest.mark.integration

runner = CliRunner()


def _settings(root: Path, isolated_config: Callable[..., Path]) -> Settings:
    return load_settings(isolated_config(root, 0.01))


def _query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(str(settings.resolve(settings.paths.duckdb)), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _report(settings: Settings) -> bytes:
    return (settings.resolve(settings.paths.reports) / "reconciliation.md").read_bytes()


@pytest.fixture(scope="module")
def duckdb_runs(
    tmp_path_factory: pytest.TempPathFactory, isolated_config: Callable[..., Path]
) -> tuple[Settings, Settings, ReconcileResult]:
    first = _settings(tmp_path_factory.mktemp("rec_a"), isolated_config)
    _, result = run_and_report(first, ["duckdb"])
    second = _settings(tmp_path_factory.mktemp("rec_b"), isolated_config)
    run_and_report(second, ["duckdb"])
    return first, second, result


def test_every_planted_discrepancy_is_found_and_classified(
    duckdb_runs: tuple[Settings, Settings, ReconcileResult],
) -> None:
    result = duckdb_runs[2]
    assert [p.name for p in result.passes] == ["as_delivered", "fixed"]
    for p in result.passes:
        assert p.grade.expected > 0
        assert p.grade.recall == 1.0, f"{p.name}: missed {p.grade.by_label}"
        assert p.grade.precision == 1.0
        assert p.grade.classification_accuracy == 1.0
    delivered = result.passes[0].grade.by_label
    assert {"D1", "D2", "D3", "D4"} <= set(delivered), "every job defect leaves a trace"
    assert all(found == planted for planted, found in delivered.values())


def test_the_delivered_migration_is_not_signed_off(
    duckdb_runs: tuple[Settings, Settings, ReconcileResult],
) -> None:
    result = duckdb_runs[2]
    assert not result.signed_off()
    assert b"**Not signed off.**" in _report(duckdb_runs[0])


def test_fixing_the_job_leaves_only_the_planted_faults(
    duckdb_runs: tuple[Settings, Settings, ReconcileResult],
) -> None:
    fixed = duckdb_runs[2].passes[1]
    assert all(label.startswith("M") for label in fixed.grade.by_label)
    delivered = duckdb_runs[2].passes[0]
    for table in ("orders", "payments"):
        assert fixed.diffs[table].transferred < delivered.diffs[table].transferred
        assert fixed.diffs[table].transferred < fixed.diffs[table].naive_transferred


def test_results_are_stored_in_ops(duckdb_runs: tuple[Settings, Settings, ReconcileResult]) -> None:
    settings, _, result = duckdb_runs
    stored = _query(
        settings, "SELECT pass, count(*) FROM ops.reconcile_discrepancies GROUP BY 1 ORDER BY 1"
    )
    expected = sorted((p.name, len(p.discrepancies)) for p in result.passes)
    assert stored == expected
    recall = _query(
        settings, "SELECT pass, value FROM ops.reconcile_metrics WHERE metric = 'recall' ORDER BY 1"
    )
    assert recall == [("as_delivered", 1.0), ("fixed", 1.0)]
    tables = _query(settings, "SELECT count(*) FROM ops.reconcile_tables")
    assert tables == [(6,)]


def test_two_runs_write_the_same_report(
    duckdb_runs: tuple[Settings, Settings, ReconcileResult],
) -> None:
    assert _report(duckdb_runs[0]) == _report(duckdb_runs[1])


# -- real legacy engines ---------------------------------------------------------------


def test_postgres_through_the_cli(
    tmp_path: Path, isolated_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = isolated_config(tmp_path, 0.01)
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    result = runner.invoke(app, ["reconcile", "run"])
    if result.exit_code != 0 and "not reachable" in result.output:
        pytest.skip("Postgres not reachable; run `make up`")
    assert result.exit_code == 0, result.output
    assert "NOT signed off" in result.output
    assert "detection recall 100.00%" in result.output
    strict = runner.invoke(app, ["reconcile", "run", "--strict"])
    assert strict.exit_code != 0, "--strict fails when the migration is not signed off"
    settings = load_settings(settings_path)
    engines = _query(settings, "SELECT DISTINCT engine FROM ops.reconcile_tables")
    assert engines == [("postgres",)]


def test_sqlserver_folds_case_like_the_legacy_system(
    tmp_path: Path, isolated_config: Callable[..., Path]
) -> None:
    pytest.importorskip("pymssql", reason="install with `uv sync --extra sqlserver`")
    settings = _settings(tmp_path, isolated_config)
    try:
        _, result = run_and_report(settings, ["sqlserver"])
    except ReconcileError as error:
        pytest.skip(str(error))
    for p in result.passes:
        assert p.grade.recall == 1.0 and p.grade.classification_accuracy == 1.0
        # The planted upper-casing of emails is equal under a case-insensitive policy.
        assert p.grade.equivalent_under_policy == 8
        assert "M4" not in p.grade.by_label
