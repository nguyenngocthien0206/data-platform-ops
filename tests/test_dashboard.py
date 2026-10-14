"""The dashboards: an ops-only data layer, a read-only role, and every page on real data.

The data layer tests need no server. The rest read the shared
``company_warehouse`` (two weeks at scale 0.01, its cost report included)
through the dashboards' own Postgres role, and render every page with
Streamlit's ``AppTest``.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors, sql
from streamlit.testing.v1 import AppTest

from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings
from platform_ops.common.store import connect_dashboard, open_store, ops_table, replace_ops_table
from platform_ops.cost.sql_parse import parse_query
from platform_ops.dashboard.queries import QUERIES, MissingData, Store, producer

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGES = REPO_ROOT / "dashboards" / "pages"


# -- the data layer, no servers needed -------------------------------------------------


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_every_query_reads_only_ops(name: str) -> None:
    query = QUERIES[name]
    parsed = parse_query(query.sql.format(ops="ops"), {}, dialect="postgres")
    assert parsed.ok, parsed.error
    assert parsed.writes is None
    read = set(parsed.reads)
    assert read, "a query must read something"
    assert all(table.startswith("ops.") for table in read), read
    assert {t.removeprefix("ops.") for t in read} == set(query.tables), "declared tables"


def test_every_table_names_the_command_that_produces_it() -> None:
    commands = {table: producer(table) for q in QUERIES.values() for table in q.tables}
    assert commands["cost_months"] == commands["query_log"] == "make cost"
    assert commands["reconcile_nights"] == commands["node_ownership"] == "make demo"
    assert commands["incident_metrics"] == commands["fault_incidents"] == "make incidents"
    assert commands["incidents"] == commands["incident_invocations"] == "make demo"


def test_no_password_says_what_to_set(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD_DASHBOARD", raising=False)
    with pytest.raises(MissingData, match="POSTGRES_PASSWORD_DASHBOARD"):
        Store(settings).frame("company_run")


# -- on the company's store ------------------------------------------------------------

# Hotspots need tables over `hotspot_min_table_bytes`; none are that big at 0.01.
MAY_BE_EMPTY_AT_SMALL_SCALE = {"hotspots"}


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(QUERIES))
def test_every_query_runs_as_the_dashboards_role(company_warehouse: Settings, name: str) -> None:
    frame = Store(company_warehouse).frame(name)
    if name not in MAY_BE_EMPTY_AT_SMALL_SCALE:
        assert not frame.empty, f"{name} returned nothing on a full run"


@pytest.mark.integration
def test_a_missing_table_names_its_command(company_warehouse: Settings) -> None:
    with open_store(company_warehouse) as store:
        store.execute(
            sql.SQL("ALTER TABLE {} RENAME TO cost_quotas_aside").format(
                ops_table(company_warehouse, "cost_quotas")
            )
        )
    try:
        with pytest.raises(
            MissingData, match=re.escape("ops.cost_quotas not found. Run `make cost`")
        ):
            Store(company_warehouse).frame("quotas")
    finally:
        with open_store(company_warehouse) as store:
            store.execute(
                sql.SQL("ALTER TABLE {} RENAME TO cost_quotas").format(
                    ops_table(company_warehouse, "cost_quotas_aside")
                )
            )


@pytest.mark.integration
def test_the_role_reads_every_table_but_cannot_write(company_warehouse: Settings) -> None:
    """Including a table recreated after the grant, as every run recreates them."""
    with open_store(company_warehouse) as store:
        replace_ops_table(store, company_warehouse, "zz_recreated", "a integer", ["a"], [(1,)])
    connection = connect_dashboard(company_warehouse)
    connection.autocommit = True
    try:
        assert connection.execute(
            sql.SQL("SELECT a FROM {}").format(ops_table(company_warehouse, "zz_recreated"))
        ).fetchone() == (1,)
        # Read-only by default, and without the privilege when that is switched off.
        with pytest.raises(errors.ReadOnlySqlTransaction):
            connection.execute(
                sql.SQL("DELETE FROM {}").format(ops_table(company_warehouse, "cost_showback"))
            )
        connection.execute("SET default_transaction_read_only = off")
        for statement in (
            sql.SQL("DELETE FROM {}").format(ops_table(company_warehouse, "cost_showback")),
            sql.SQL("CREATE TABLE {} (a integer)").format(ops_table(company_warehouse, "zz_mine")),
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(statement)
    finally:
        connection.close()
        with open_store(company_warehouse) as store:
            store.execute(
                sql.SQL("DROP TABLE {}").format(ops_table(company_warehouse, "zz_recreated"))
            )


def _elements(node: Any, found: Counter[str]) -> Counter[str]:
    children = getattr(node, "children", None) or {}
    for child in children.values() if isinstance(children, dict) else children:
        found[str(getattr(child, "type", type(child).__name__))] += 1
        _elements(child, found)
    return found


EXPECTED = {
    "overview": ("Data platform operations", 1),
    "cost": ("Cost and FinOps", 4),
    "incidents": ("Incident management", 4),
    "reconciliation": ("Migration reconciliation", 2),
}


def _page(settings: Settings, name: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings.root / "config" / "settings.yaml"))
    return AppTest.from_file(str(PAGES / f"{name}.py"), default_timeout=120).run()


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_page_renders(
    company_warehouse: Settings, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = _page(company_warehouse, name, monkeypatch)
    title, charts = EXPECTED[name]
    assert not test.exception, [e.value for e in test.exception]
    assert [t.value for t in test.title] == [title]
    assert not test.info, [i.value for i in test.info]  # no "run make X first" after a run
    elements = _elements(test._tree, Counter())
    assert elements["vega_lite_chart"] == charts
    assert elements["metric"] >= 1


@pytest.mark.integration
def test_the_cost_page_switches_pricing_model(
    company_warehouse: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = _page(company_warehouse, "cost", monkeypatch)
    cluster_total = test.metric[0].value
    test.button_group[0].set_value("compute").run()
    assert not test.exception
    assert test.metric[0].value != cluster_total


@pytest.mark.integration
def test_the_night_picker_shows_that_night(
    company_warehouse: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Night 1 is the delivered job's; a later night of the fixed job plants nothing."""
    test = _page(company_warehouse, "reconciliation", monkeypatch)
    first = [m.value for m in test.metric if m.label == "Planted"]
    assert first and first[0] != "0"
    slider = test.select_slider[0]
    slider.set_value(len(slider.options)).run()
    assert not test.exception
    assert [m.value for m in test.metric if m.label == "Planted"] != first
