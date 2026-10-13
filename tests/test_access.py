"""The company's ClickHouse users: who may do what, and what the query log records.

Read on the shared ``company_warehouse`` (its users are namespaced ``t_suite_``),
except the first test, which needs no server: a missing password stops the run
before anything is touched, naming every missing variable.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.servers import clickhouse_client, password_variable
from platform_ops.metadata.registry import Registry
from platform_ops.simulation.access import company_users, ensure_users
from platform_ops.simulation.bi import dashboards, exposed_models, model_shapes

USERS = ["platform", "sales", "marketing", "finance", "product", "metabase", "dbt", "loader"]


def test_a_missing_password_stops_the_run_and_names_every_variable(
    tmp_path: Path, isolated_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = isolated_config(tmp_path, 0.01, namespace="t_nopw_")
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    for user in USERS:
        monkeypatch.delenv(password_variable(user), raising=False)
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    for command in (["seed"], ["company", "run"], ["build"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 2, result.output
        for user in USERS:
            assert password_variable(user) in result.output


def test_the_users_are_the_teams_then_the_services(repo_settings: Settings) -> None:
    assert [u.name for u in company_users(repo_settings)] == USERS


# -- on the shared company -------------------------------------------------------------------


def _as(settings: Settings, user: str, query: str) -> Any:
    client = clickhouse_client(settings, user)
    try:
        return client.query(query).result_rows
    finally:
        client.close()


def _denied(settings: Settings, user: str, query: str) -> bool:
    client = clickhouse_client(settings, user)
    try:
        client.command(query)
    except Exception as error:
        return "ACCESS_DENIED" in str(error) or "READONLY" in str(error)
    finally:
        client.close()
    return False


@pytest.mark.integration
def test_business_teams_and_metabase_read_the_marts_and_nothing_else(
    company_warehouse: Settings,
) -> None:
    marts = company_warehouse.company.name("marts")
    raw = company_warehouse.company.name("raw")
    for user in ("sales", "metabase"):
        assert _as(company_warehouse, user, f"SELECT count() FROM `{marts}`.sales_fct_orders")
        assert _denied(company_warehouse, user, f"SELECT count() FROM `{raw}`.orders")
        assert _denied(company_warehouse, user, f"DROP TABLE `{marts}`.sales_fct_orders")
        assert _denied(
            company_warehouse,
            user,
            f"INSERT INTO `{marts}`.sales_fct_orders "
            f"SELECT * FROM `{marts}`.sales_fct_orders LIMIT 0",
        )


@pytest.mark.integration
def test_the_platform_team_reads_every_layer_but_writes_nothing(
    company_warehouse: Settings,
) -> None:
    raw = company_warehouse.company.name("raw")
    assert _as(company_warehouse, "platform", f"SELECT count() FROM `{raw}`.orders FINAL")
    assert _denied(company_warehouse, "platform", f"TRUNCATE TABLE `{raw}`.orders")


@pytest.mark.integration
def test_metabase_sees_only_its_own_running_queries(company_warehouse: Settings) -> None:
    users = {u for (u,) in _as(company_warehouse, "metabase", "SELECT user FROM system.processes")}
    assert users == {company_warehouse.company.name("metabase")}


@pytest.mark.integration
def test_the_query_log_records_dbt_and_the_ingestion_under_their_own_users(
    company_warehouse: Settings,
) -> None:
    company = company_warehouse.company
    client = clickhouse_client(company_warehouse)
    try:
        client.command("SYSTEM FLUSH LOGS")
        rows = client.query(
            "SELECT user, countIf(query_kind = 'Insert'), "
            'countIf(startsWith(query, \'/* {"app": "platform-ops"\')) '
            "FROM system.query_log WHERE type = 'QueryFinish' AND user IN %(users)s GROUP BY user",
            parameters={"users": [company.name("dbt"), company.name("loader")]},
        ).result_rows
    finally:
        client.close()
    found = {user: (inserts, commented) for user, inserts, commented in rows}
    assert found[company.name("dbt")][1] > 0, "dbt's queries carry the node comment"
    assert found[company.name("loader")][0] > 0, "the ingestion inserts as loader"
    assert found[company.name("loader")][1] == 0


@pytest.mark.integration
def test_creating_the_users_again_changes_nothing(company_warehouse: Settings) -> None:
    def grants() -> list[str]:
        client = clickhouse_client(company_warehouse)
        try:
            names = [company_warehouse.company.name(u) for u in USERS]
            return sorted(
                g
                for name in names
                for (g,) in client.query(f"SHOW GRANTS FOR `{name}`").result_rows
            )
        finally:
            client.close()

    before = grants()
    ensure_users(company_warehouse)
    assert grants() == before
    assert _as(company_warehouse, "metabase", "SELECT 1") == [(1,)]


@pytest.mark.integration
def test_every_dashboard_card_runs_as_metabase(company_warehouse: Settings) -> None:
    """The SQL Metabase runs; Metabase itself is checked by make docker-metabase-check."""
    manifest = invocation_from_settings(company_warehouse).manifest_path
    client = clickhouse_client(company_warehouse, "metabase")
    try:
        shapes = model_shapes(
            client, company_warehouse.company.name("marts"), exposed_models(manifest)
        )
        boards = dashboards(
            manifest, Registry.from_config_dir(company_warehouse.root / "config"), shapes
        )
        assert len(boards) == 12
        for board in boards:
            assert board.cards, board.name
            for card in board.cards:
                assert client.query(card.sql).result_rows, f"{board.name}: {card.model}"
    finally:
        client.close()
