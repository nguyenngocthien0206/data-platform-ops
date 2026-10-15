"""The hooks orchestrators call, and the chat double their tests page into (Phase 14b).

The helpers are tested against a fake CLI that records what it was given, then
once against the real CLI and the real store, with the shop project's artifacts
(``tests/fixtures/dbt_artifacts``): that is the contract Airflow and Dagster
rely on. The chat double is tested with the toolkit's own Slack and Telegram
channels, so what the orchestrator tests will read is what a real page sends.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from psycopg import sql

from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.common.servers import connect_postgres
from platform_ops.incidents import store
from platform_ops.incidents.notify import Notification, SlackChannel, TelegramChannel, deliver
from platform_ops.integrations import REGISTRY_ENV_VAR, ingest
from platform_ops.integrations.cosmos import ingest_after_task, run_id_from
from platform_ops.integrations.dagster import ingest_dbt_invocation
from platform_ops.metadata.registry import Team

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "dbt_artifacts"

# A stand-in for `platform-ops`: records its arguments, exits as told.
FAKE_CLI = """
import json, os, sys, time
with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep(float(os.environ.get("FAKE_CLI_SLEEP", "0")))
print("ingested")
sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))
"""


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[list[str]]]:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PLATFORM_OPS_CLI", f"{sys.executable} {script}".replace("\\", "/"))
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.delenv(REGISTRY_ENV_VAR, raising=False)

    def calls() -> list[list[str]]:
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls


def test_the_helper_runs_ingest_with_the_target_and_run_id(
    fake_cli: Callable[[], list[list[str]]],
) -> None:
    outcome = ingest("/runs/target", "airflow:company:manual_1")
    assert outcome.ok and outcome.output == "ingested"
    assert fake_cli() == [
        ["incidents", "ingest", "--target", "/runs/target", "--run-id", "airflow:company:manual_1"]
    ]


def test_detection_time_is_passed_as_naive_utc(fake_cli: Callable[[], list[list[str]]]) -> None:
    hanoi = timezone(timedelta(hours=7))
    ingest("t", "r", at=datetime(2026, 4, 9, 9, 0, tzinfo=hanoi))
    ingest("t", "r", at=datetime(2026, 4, 9, 2, 0))
    assert [call[-1] for call in fake_cli()] == ["2026-04-09T02:00:00", "2026-04-09T02:00:00"]


def test_the_consuming_repositorys_registry_is_passed_on(
    fake_cli: Callable[[], list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_ENV_VAR, "/repo/registry")
    ingest("t", "r")
    assert fake_cli()[0][-2:] == ["--registry-dir", "/repo/registry"]


def test_a_failed_ingest_is_logged_never_raised(
    fake_cli: Callable[[], list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_CLI_EXIT", "2")
    with caplog.at_level(logging.WARNING, logger="platform_ops.integrations"):
        outcome = ingest("t", "night-1")
    assert not outcome.ok
    assert "incident ingest for night-1 failed (exit 2)" in caplog.text


def test_a_missing_cli_or_a_hung_one_is_reported(
    fake_cli: Callable[[], list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not ingest("t", "r", cli=["no-such-platform-ops-binary"]).ok
    monkeypatch.setenv("FAKE_CLI_SLEEP", "5")
    hung = ingest("t", "r", timeout=0.5)
    assert not hung.ok and "timed out" in hung.output


def test_cosmos_takes_the_run_from_the_task_context_or_airflows_environment(
    fake_cli: Callable[[], list[list[str]]], tmp_path: Path
) -> None:
    context = {"dag": SimpleNamespace(dag_id="company_dbt"), "run_id": "scheduled__2026-04-09"}
    assert run_id_from(context) == "airflow:company_dbt:scheduled__2026-04-09"
    from_env = run_id_from(
        {}, {"AIRFLOW_CTX_DAG_ID": "company_dbt", "AIRFLOW_CTX_DAG_RUN_ID": "manual__1"}
    )
    assert from_env == "airflow:company_dbt:manual__1"
    with pytest.raises(ValueError, match="no Airflow run id"):
        run_id_from({}, {})
    outcome = ingest_after_task(tmp_path / "proj", **context)
    assert outcome.ok
    target, run = fake_cli()[0][3], fake_cli()[0][5]
    assert Path(target) == tmp_path / "proj" / "target"
    assert run == "airflow:company_dbt:scheduled__2026-04-09"


def test_cosmos_without_a_run_id_does_not_call_the_cli(
    fake_cli: Callable[[], list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AIRFLOW_CTX_DAG_ID", "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    assert not ingest_after_task("/proj").ok
    assert fake_cli() == []


def test_dagster_takes_the_invocations_target_and_the_runs_id(
    fake_cli: Callable[[], list[list[str]]],
) -> None:
    invocation = SimpleNamespace(target_path=Path("/dagster/target"))
    assert ingest_dbt_invocation(invocation, SimpleNamespace(run_id="8b1f")).ok
    call = fake_cli()[0]
    assert Path(call[3]) == Path("/dagster/target") and call[5] == "dagster:8b1f"
    assert not ingest_dbt_invocation(SimpleNamespace(), SimpleNamespace(run_id="x")).ok


# -- the chat double -------------------------------------------------------------------


def _chat_double() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "chat_double", REPO_ROOT / "orchestrators" / "chat_double.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chat() -> Iterator[tuple[str, Any]]:
    server, messages = _chat_double().serve("127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.server_port}", messages
    finally:
        server.shutdown()


NOTE = Notification(
    "INC-001", "page", "night-1", "dana", "data", "SEV1", "2 checks failing", datetime(2026, 4, 9)
)


def test_the_double_takes_what_the_toolkit_sends_to_slack_and_telegram(
    chat: tuple[str, Any],
) -> None:
    url, messages = chat
    team = Team(id="data", name="D", channel="#data", members=["dana"], telegram_chat="-1001")
    channels = [SlackChannel("xoxb-test", url), TelegramChannel("123:abc", url)]
    assert deliver(NOTE, team, channels) == "slack, telegram"
    text = "[SEV1] INC-001 (page) for dana: 2 checks failing"
    assert messages.all() == [
        {"service": "slack", "to": "#data", "text": text},
        {"service": "telegram", "to": "-1001", "text": text},
    ]


def test_the_double_refuses_what_the_real_apis_refuse(chat: tuple[str, Any]) -> None:
    url, messages = chat
    team = Team(id="data", name="D", channel="#data", members=["dana"], telegram_chat="-1001")
    assert deliver(NOTE, team, [SlackChannel("", url)]) == "slack failed"
    assert messages.all() == []


# -- the contract, end to end ----------------------------------------------------------


@pytest.fixture
def isolated_store(
    tmp_path: Path,
    isolated_config: Callable[..., Path],
    servers_or_skip: Callable[[Settings], None],
) -> Iterator[tuple[Settings, Path]]:
    config = isolated_config(tmp_path / "cfg", 0.01, namespace="t_integ_")
    settings = load_settings(config)
    servers_or_skip(settings)
    store.reset(settings)
    yield settings, config
    with connect_postgres(settings, settings.company.ops_database) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(store.schema(settings))
        )
        connection.commit()


@pytest.mark.integration
def test_the_helper_feeds_the_real_store_through_the_real_cli(
    isolated_store: tuple[Settings, Path], tmp_path: Path
) -> None:
    settings, config = isolated_store
    target = tmp_path / "target"
    target.mkdir()
    for name in ("manifest.json", "snowflake/run_results.json"):
        shutil.copyfile(FIXTURES / name, target / Path(name).name)
    environ = {
        **os.environ,
        CONFIG_PATH_ENV_VAR: str(config),
        REGISTRY_ENV_VAR: str(FIXTURES / "registry"),
    }
    cli = [sys.executable, "-c", "from platform_ops.cli import main; main()"]
    first = ingest(target, "airflow:shop:manual_1", cli=cli, environ=environ)
    assert first.ok, first.output
    assert "opened 1" in first.output
    again = ingest(target, "airflow:shop:manual_1", cli=cli, environ=environ)
    assert again.ok and "nothing new" in again.output
    with store.transaction(settings) as connection:
        rows = connection.execute(
            sql.SQL("SELECT root, owner, opened_by_run FROM {}").format(
                store.table(settings, "incidents")
            )
        ).fetchall()
    assert rows == [("model.shop.stg_orders", "dana", "airflow:shop:manual_1")]
