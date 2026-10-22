"""End to end: the migration's parallel run, as ``make demo`` replays it (Phase 12).

Read from the shared ``company_warehouse``, whose second week is a short
parallel run: two nights of the job as delivered, the fixed job and its
backfill, one one-off fault and its repair the next day, and a cutover rule of
two clean nights. Every night was reconciled through the real CLI, at both
levels, and recorded in the ops store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from psycopg import sql
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.common.store import open_store, ops_table
from platform_ops.simulation.company import remove_company

pytestmark = pytest.mark.integration


def _rows(settings: Settings, table: str, query: str) -> list[tuple[Any, ...]]:
    with open_store(settings) as store:
        return list(store.execute(sql.SQL(query).format(ops_table(settings, table))).fetchall())


def _report(settings: Settings) -> Path:
    return settings.resolve(settings.paths.reports) / "reconciliation.md"


def test_every_night_of_the_parallel_run_was_reconciled_at_both_levels(
    company_warehouse: Settings,
) -> None:
    rows = _rows(
        company_warehouse,
        "reconcile_nights",
        "SELECT night, job, backfilled, planted, repaired, level, count(*) FROM {} "
        "GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY 1, 6",
    )
    nights = sorted({r[0] for r in rows})
    assert nights == list(range(7, 14))
    per_night = {(r[0], r[5]): r[6] for r in rows}
    assert all(per_night[(n, "table")] == 9 and per_night[(n, "report")] == 12 for n in nights)
    events = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows if r[5] == "table"}
    assert events[7] == events[8] == ("delivered", False, "", "")
    assert events[9] == ("fixed", True, "", "")
    assert events[11] == ("fixed", False, "M1", "")
    assert events[12] == ("fixed", False, "", "M1")


def test_the_delivered_job_fails_and_every_planted_discrepancy_is_found(
    company_warehouse: Settings,
) -> None:
    failing = _rows(
        company_warehouse,
        "reconcile_nights",
        "SELECT night, name FROM {} WHERE level = 'table' AND NOT passed ORDER BY 1, 2",
    )
    assert {name for night, name in failing if night in (7, 8)} >= {"customers", "payments"}
    assert {night for night, _ in failing} == {7, 8, 11}
    grades = _rows(
        company_warehouse,
        "reconcile_grades",
        "SELECT night, expected, detected, true_positives, correctly_classified FROM {} "
        "ORDER BY night",
    )
    for night, expected, detected, hits, correct in grades:
        assert hits == expected == detected, f"night {night}: recall and precision"
        assert correct == hits, f"night {night}: classification"
    by_night = {g[0]: g[1] for g in grades}
    assert by_night[7] > 0 and by_night[11] == 5 and by_night[10] == by_night[12] == 0


def test_every_report_difference_is_explained_by_an_upstream_table(
    company_warehouse: Settings,
) -> None:
    rows = _rows(
        company_warehouse,
        "reconcile_nights",
        "SELECT night, name, explained, differing_upstream FROM {} WHERE level = 'report' "
        "AND (missing + extra + duplicate_key + rows_with_diffs) > 0 ORDER BY 1, 2",
    )
    assert rows, "the delivered job reaches at least one report"
    assert all(explained for _, _, explained, _ in rows)
    assert {night for night, *_ in rows} <= {7, 8}
    assert any("payments" in upstream for *_, upstream in rows)


def test_the_cutover_is_recommended_after_the_clean_streak(company_warehouse: Settings) -> None:
    ((recommended, night, clean, required),) = _rows(
        company_warehouse,
        "reconcile_cutover",
        "SELECT recommended, night, clean_nights, required FROM {}",
    )
    assert (recommended, night, clean, required) == (True, 10, 2, 2)
    text = _report(company_warehouse).read_text(encoding="utf-8")
    assert "**Cutover recommended on 2026-01-15** (night 10)" in text
    assert text.index("## Acceptance criteria") < text.index("## Verdict")


def test_make_reconcile_rewrites_the_same_report_from_the_store(
    company_warehouse: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _report(company_warehouse).read_bytes()
    monkeypatch.setenv(
        CONFIG_PATH_ENV_VAR, str(company_warehouse.root / "config" / "settings.yaml")
    )
    result = CliRunner().invoke(app, ["reconcile", "run", "--strict"])
    assert result.exit_code == 0, result.output
    assert "cutover recommended" in result.output
    assert _report(company_warehouse).read_bytes() == before


def test_reconcile_without_a_parallel_run_says_what_to_run(
    tmp_path: Path, isolated_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = isolated_config(tmp_path, 0.01, namespace="t_norun_")
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    try:
        result = CliRunner().invoke(app, ["reconcile", "run"])
        assert result.exit_code == 2
        assert "make demo" in result.output
    finally:
        remove_company(load_settings(settings_path))
