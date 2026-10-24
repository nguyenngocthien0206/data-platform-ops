"""The incident desk on the Postgres store, fed artifacts from other warehouses (Phase 14).

A small shop project and the artifacts two of its runs left on Snowflake and
BigQuery (``tests/fixtures/dbt_artifacts``, written by
``scripts/make_artifact_fixtures.py``), plus invocations built from them the
way Cosmos splits a run, one per model. Each test starts from an empty store in
its own namespace; only Postgres is needed.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from psycopg import sql

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.servers import connect_postgres
from platform_ops.incidents import store
from platform_ops.incidents.desk import Context, ingest_target
from platform_ops.incidents.notify import SlackChannel
from platform_ops.incidents.severity import raised

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dbt_artifacts"
STG_ORDERS = "model.shop.stg_orders"
FCT = "model.shop.fct_revenue"
PAYMENTS = "source.shop.raw.payments"
NOT_NULL = "test.shop.not_null_stg_orders_customer_id.a1"
UNIQUE = "test.shop.unique_fct_revenue_order_id.c3"
AT = datetime(2026, 3, 2, 2, 0)


@pytest.fixture
def desk(
    tmp_path: Path,
    isolated_config: Callable[..., Path],
    servers_or_skip: Callable[[Settings], None],
) -> Iterator[tuple[Settings, Context]]:
    settings = load_settings(isolated_config(tmp_path / "cfg", 0.01, namespace="t_desk_"))
    servers_or_skip(settings)
    store.reset(settings)
    context = Context.load(settings, FIXTURES / "manifest.json", FIXTURES / "registry")
    yield settings, context
    with connect_postgres(settings, settings.company.ops_database) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(store.schema(settings))
        )
        connection.commit()


def _target(tmp_path: Path, name: str, *files: tuple[str, dict[str, Any]]) -> Path:
    """A target folder with the shop's manifest and the given result files."""
    target = tmp_path / name
    target.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "manifest.json", target / "manifest.json")
    for filename, data in files:
        (target / filename).write_text(json.dumps(data), encoding="utf-8")
    return target


def _fixture(adapter: str, filename: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / adapter / filename).read_text("utf-8"))
    return data


def _invocation(invocation_id: str, *results: tuple[str, str]) -> dict[str, Any]:
    """One Cosmos-style invocation: a run_results.json reporting on a few nodes."""
    return {
        "metadata": {"invocation_id": invocation_id, "generated_at": "2026-03-02T02:00:00Z"},
        "results": [{"unique_id": u, "status": s, "message": None} for u, s in results],
    }


def _rows(settings: Settings, table: str, query: str) -> list[tuple[Any, ...]]:
    with store.transaction(settings) as connection:
        return list(
            connection.execute(sql.SQL(query).format(store.table(settings, table))).fetchall()
        )


def _live(settings: Settings) -> list[tuple[Any, ...]]:
    return _rows(
        settings,
        "incidents",
        "SELECT root, owner, severity, resolved_by_run FROM {} WHERE merged_into IS NULL "
        "ORDER BY root",
    )


def test_a_snowflake_build_opens_one_incident_paged_to_the_source_owner(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    target = _target(
        tmp_path, "sf", ("run_results.json", _fixture("snowflake", "run_results.json"))
    )
    result = ingest_target(settings, target, "sf-1", context=context)
    assert (result.new_invocations, len(result.opened), result.failing_checks) == (1, 1, 1)
    # A staging root pages whoever owns the feed: dana owns raw.orders.
    assert [(root, owner) for root, owner, _, _ in _live(settings)] == [(STG_ORDERS, "dana")]
    # dbt wrote the file at 02:14 UTC; without --at, that is when it was detected.
    (opened,) = _rows(settings, "incidents", "SELECT opened_at FROM {}")[0]
    assert opened == datetime(2026, 3, 2, 2, 14, 9, 120934)


def test_a_bigquery_night_groups_a_stale_source_and_the_test_below_it(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    target = _target(
        tmp_path,
        "bq",
        ("sources.json", _fixture("bigquery", "sources.json")),
        ("run_results.json", _fixture("bigquery", "run_results.json")),
    )
    result = ingest_target(settings, target, "bq-1", at=AT, context=context)
    assert (result.new_invocations, result.failing_checks) == (2, 2)
    assert [root for root, *_ in _live(settings)] == [PAYMENTS]
    checks = _rows(settings, "incident_check_events", "SELECT check_type, incident_id FROM {}")
    assert sorted(checks) == [("freshness", "INC-001"), ("test", "INC-001")]


def test_ingesting_the_same_artifacts_twice_changes_nothing(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    target = _target(
        tmp_path, "sf", ("run_results.json", _fixture("snowflake", "run_results.json"))
    )
    ingest_target(settings, target, "sf-1", context=context)
    before = _rows(settings, "incidents", "SELECT * FROM {}")
    again = ingest_target(settings, target, "sf-1", context=context)
    assert again.new_invocations == 0
    assert _rows(settings, "incidents", "SELECT * FROM {}") == before
    assert len(_rows(settings, "incident_notifications", "SELECT * FROM {}")) == 1


def test_cosmos_style_invocations_group_like_one_build_in_any_order(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    """The downstream failure arrives first and opens an incident; the upstream
    one arrives later, and the first incident is merged into the real root's."""
    settings, context = desk
    downstream = _target(tmp_path, "fct", ("run_results.json", _invocation("c-1", (FCT, "error"))))
    upstream = _target(
        tmp_path,
        "stg",
        ("run_results.json", _invocation("c-2", (STG_ORDERS, "success"), (NOT_NULL, "fail"))),
    )
    first = ingest_target(settings, downstream, "cosmos-1", at=AT, context=context)
    assert [root for root, *_ in _live(settings)] == [FCT] and first.opened == ("INC-001",)
    ingest_target(settings, upstream, "cosmos-1", at=AT, context=context)
    assert [root for root, *_ in _live(settings)] == [STG_ORDERS]
    merged = _rows(settings, "incidents", "SELECT incident_id, merged_into FROM {} ORDER BY 1")
    assert merged == [("INC-001", "INC-002"), ("INC-002", None)]
    checks = _rows(settings, "incident_check_events", "SELECT DISTINCT incident_id FROM {}")
    assert checks == [("INC-002",)]


def test_a_later_run_resolves_only_a_root_it_covered_and_found_healthy(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    failing = _target(
        tmp_path,
        "r1",
        ("run_results.json", _invocation("r-1", (STG_ORDERS, "success"), (NOT_NULL, "fail"))),
    )
    ingest_target(settings, failing, "night-1", at=AT, context=context)
    # A run that never touched stg_orders cannot say it is healthy.
    elsewhere = _target(
        tmp_path, "r2", ("run_results.json", _invocation("r-2", (FCT, "success"), (UNIQUE, "pass")))
    )
    ingest_target(settings, elsewhere, "night-2", at=AT.replace(day=3), context=context)
    assert _live(settings)[0][3] is None
    # The model ran but its test has not run yet: not healthy yet either.
    model_only = _target(
        tmp_path, "r3", ("run_results.json", _invocation("r-3", (STG_ORDERS, "success")))
    )
    ingest_target(settings, model_only, "night-3", at=AT.replace(day=4), context=context)
    assert _live(settings)[0][3] is None
    tested = _target(tmp_path, "r3b", ("run_results.json", _invocation("r-3b", (NOT_NULL, "pass"))))
    result = ingest_target(settings, tested, "night-3", at=AT.replace(day=4), context=context)
    assert result.resolved == ("INC-001",)
    assert _live(settings)[0][3] == "night-3"


def test_a_rerun_failing_below_a_root_it_did_not_check_joins_that_incident(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    """stg_orders is still broken; a rerun after another fix rebuilds fct_revenue,
    which reads it, without running stg_orders. Same problem, not a new one."""
    settings, context = desk
    night = _target(
        tmp_path,
        "n1",
        ("run_results.json", _invocation("n-1", (STG_ORDERS, "success"), (NOT_NULL, "fail"))),
    )
    ingest_target(settings, night, "night-1", at=AT, context=context)
    rerun = _target(tmp_path, "rr", ("run_results.json", _invocation("rr-1", (FCT, "error"))))
    result = ingest_target(settings, rerun, "rerun-INC-009", at=AT.replace(hour=9), context=context)
    assert result.opened == () and result.appended == ("INC-001",)
    assert [root for root, *_ in _live(settings)] == [STG_ORDERS]
    # A run that checked stg_orders and found it healthy: a failure below is its own.
    checked = _target(
        tmp_path,
        "n2",
        (
            "run_results.json",
            _invocation("n-2", (STG_ORDERS, "success"), (NOT_NULL, "pass"), (FCT, "error")),
        ),
    )
    later = ingest_target(settings, checked, "night-2", at=AT.replace(day=3), context=context)
    assert later.opened == ("INC-002",) and later.resolved == ("INC-001",)


def test_an_incident_still_failing_after_its_time_is_raised_once(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    failing = ("run_results.json", _invocation("e-1", (STG_ORDERS, "success"), (NOT_NULL, "fail")))
    ingest_target(settings, _target(tmp_path, "e1", failing), "night-1", at=AT, context=context)
    ((before,),) = _rows(settings, "incidents", "SELECT severity FROM {}")
    hours = settings.incidents.escalation.after_hours[before]
    again = ("run_results.json", _invocation("e-2", (STG_ORDERS, "success"), (NOT_NULL, "fail")))
    later = AT + timedelta(hours=hours)
    ingest_target(settings, _target(tmp_path, "e2", again), "night-2", at=later, context=context)
    ((after,),) = _rows(settings, "incidents", "SELECT severity FROM {}")
    assert before != "SEV1" and after == raised(before)
    raises = _rows(settings, "incident_escalations", "SELECT from_severity, to_severity FROM {}")
    assert raises == [(before, after)]
    kinds = _rows(settings, "incident_notifications", "SELECT kind FROM {} ORDER BY sent_at, kind")
    assert ("escalation",) in kinds
    # The same run ingested again raises nothing more.
    ingest_target(settings, _target(tmp_path, "e3", again), "night-2", at=later, context=context)
    assert len(_rows(settings, "incident_escalations", "SELECT * FROM {}")) == 1


def test_concurrent_ingests_of_one_run_end_as_one_after_the_other(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    parts = [
        _target(tmp_path, f"p{n}", ("run_results.json", _invocation(f"p-{n}", *results)))
        for n, results in enumerate(
            [((FCT, "error"),), ((STG_ORDERS, "success"), (NOT_NULL, "fail"))]
        )
    ]
    threads = [
        threading.Thread(
            target=ingest_target,
            args=(settings, p, "night-1"),
            kwargs={"at": AT, "context": context},
        )
        for p in parts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [root for root, *_ in _live(settings)] == [STG_ORDERS]
    assert len(_rows(settings, "incident_invocations", "SELECT * FROM {}")) == 2


class _Slack(BaseHTTPRequestHandler):
    posts: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Slack.posts.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(length)),
            }
        )
        reply = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_: object) -> None:
        pass


def test_a_page_goes_to_the_teams_slack_channel_once_after_commit(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    settings, context = desk
    server = HTTPServer(("127.0.0.1", 0), _Slack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Slack.posts.clear()
    try:
        slack = SlackChannel("xoxb-test", f"http://127.0.0.1:{server.server_port}")
        target = _target(
            tmp_path, "sf", ("run_results.json", _fixture("snowflake", "run_results.json"))
        )
        ingest_target(settings, target, "sf-1", context=context, channels=[slack])
        ingest_target(settings, target, "sf-1", context=context, channels=[slack])
    finally:
        server.shutdown()
    (post,) = _Slack.posts
    assert post["path"] == "/chat.postMessage" and post["auth"] == "Bearer xoxb-test"
    assert post["body"]["channel"] == "#data-platform"
    assert "INC-001 (page) for dana" in post["body"]["text"]
    delivered = _rows(settings, "incident_notifications", "SELECT kind, delivered FROM {}")
    assert delivered == [("page", "slack")]


def test_a_stale_feed_above_an_open_incident_is_the_same_problem(
    desk: tuple[Settings, Context], tmp_path: Path
) -> None:
    """Freshness is ingested before the run: the feed's staleness arrives while
    its staging model's incident is open, and appends to it instead of paging."""
    settings, context = desk
    failing = _target(
        tmp_path, "n1", ("run_results.json", _invocation("n-1", (STG_ORDERS, "error")))
    )
    ingest_target(settings, failing, "night-1", at=AT, context=context)
    stale = {
        "metadata": {"invocation_id": "n-2", "generated_at": "2026-03-03T02:00:00Z"},
        "results": [{"unique_id": "source.shop.raw.orders", "status": "error", "age": 200000}],
    }
    result = ingest_target(
        settings,
        _target(tmp_path, "n2", ("sources.json", stale)),
        "night-2",
        at=AT.replace(day=3),
        context=context,
    )
    assert (result.opened, result.appended) == ((), ("INC-001",))
    assert [root for root, *_ in _live(settings)] == [STG_ORDERS]
    pages = _rows(settings, "incident_notifications", "SELECT kind FROM {} ORDER BY sent_at")
    assert pages == [("page",), ("update",)]
