"""Ingestion, severity, routing, lifecycle and notifier units, on hand-built inputs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from platform_ops.common.config import SEVERITIES, Settings, Severity, Tier
from platform_ops.incidents import ingest, lifecycle, notify
from platform_ops.incidents.routing import naive_route, route
from platform_ops.incidents.severity import score_incident, severity_for
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import DatasetRule, Registry, Team

AT = datetime(2026, 1, 8, 2)
SRC = "source.company.raw.orders"
STG = "model.company.stg_orders"
MART = "model.company.sales_fct_orders"
DASH = "exposure.company.sales_dashboard"
REL = "test.company.relationships_items_orders"


def _nodes() -> dict[str, Node]:
    return {
        SRC: Node(SRC, "source", "orders"),
        STG: Node(STG, "model", "stg_orders", depends_on=(SRC,)),
        MART: Node(MART, "model", "sales_fct_orders", depends_on=(STG,)),
        DASH: Node(DASH, "exposure", "sales_dashboard", depends_on=(MART,)),
        "test.company.not_null_stg_orders_id": Node(
            "test.company.not_null_stg_orders_id", "test", "not_null_stg_orders_id",
            depends_on=("macro.dbt.test_not_null", STG), attached_node=STG,
        ),
        REL: Node(REL, "test", "relationships_items_orders", depends_on=(MART, STG),
                  attached_node=MART),
    }  # fmt: skip


def _registry() -> Registry:
    return Registry(
        teams={
            "platform": Team(id="platform", name="Platform", channel="#platform",
                             members=["priya", "marco"]),
            "sales": Team(id="sales", name="Sales", channel="#sales", members=["sam"]),
        },
        rules=(
            DatasetRule(match="source.company.raw.*", owner="priya", team="platform",
                        tier="critical"),
            DatasetRule(match="model.company.stg_*", owner="marco", team="platform",
                        tier="important"),
            DatasetRule(match="model.company.sales_*", owner="sam", team="sales",
                        tier="critical"),
            DatasetRule(match="exposure.company.sales_*", owner="sam", team="sales",
                        tier="important"),
        ),
    )  # fmt: skip


# -- ingestion -------------------------------------------------------------------------


def test_run_results_keep_failures_and_skips_only() -> None:
    data = {
        "results": [
            {"unique_id": STG, "status": "error", "message": "Binder Error:\n  column"},
            {"unique_id": MART, "status": "skipped", "message": None},
            {"unique_id": "test.company.not_null_stg_orders_id", "status": "fail",
             "message": "Got 3 results"},
            {"unique_id": REL, "status": "pass", "message": None},
            {"unique_id": "model.company.not_in_manifest", "status": "error", "message": ""},
        ]
    }  # fmt: skip
    outcome = ingest.parse_run_results(data, _nodes(), run_id="r1", detected_at=AT)
    assert [(e.check_type, e.check_id, e.subject) for e in outcome.events] == [
        ("model", STG, STG),
        ("test", "test.company.not_null_stg_orders_id", STG),
    ]
    assert outcome.events[0].message == "Binder Error: column"
    assert outcome.skipped == (MART,)
    assert outcome.checks_run == 5


def test_a_test_across_two_models_is_its_own_subject() -> None:
    nodes = _nodes()
    assert ingest.test_subject(nodes["test.company.not_null_stg_orders_id"]) == STG
    assert ingest.test_subject(nodes[REL]) == REL


def test_freshness_warn_is_not_an_alert() -> None:
    data = {
        "results": [
            {"unique_id": SRC, "status": "error", "max_loaded_at": "x", "age": 180000},
            {"unique_id": "source.company.raw.payments", "status": "warn", "age": 90000},
            {"unique_id": "source.company.raw.items", "status": "pass", "age": 10},
        ]
    }
    outcome = ingest.parse_freshness(data, run_id="r1", detected_at=AT)
    assert [(e.check_id, e.subject) for e in outcome.events] == [(f"freshness:{SRC}", SRC)]
    assert "50.0 h" in outcome.events[0].message


def test_merge_combines_run_test_and_freshness() -> None:
    event = ingest.CheckEvent("r1", STG, "model", STG, "error", "", AT)
    merged = ingest.merge([
        ingest.RunOutcome((event,), (MART,), 3),
        ingest.RunOutcome((event,), (), 4),
    ])  # fmt: skip
    assert merged.events == (event,)
    assert merged.skipped == (MART,)
    assert merged.checks_run == 7


# -- severity --------------------------------------------------------------------------


def _tiers(registry: Registry, nodes: dict[str, Node]) -> dict[str, Tier]:
    tiers: dict[str, Tier] = {}
    for unique_id in nodes:
        rule = registry.resolve(unique_id).rule
        if rule is not None:
            tiers[unique_id] = rule.tier
    return tiers


def test_score_is_root_tier_plus_weighted_consumers(repo_settings: Settings) -> None:
    nodes, registry = _nodes(), _registry()
    graph = build_graph(nodes)
    scored = score_incident(graph, STG, nodes, _tiers(registry, nodes), repo_settings)
    weights = repo_settings.metadata.tier_weights
    multiplier = repo_settings.incidents.severity.root_tier_multiplier
    # stg_orders is important; below it a critical mart and an important dashboard.
    expected = multiplier * weights.important + weights.critical + weights.important
    assert scored.score == expected
    assert [c.unique_id for c in scored.consumers] == [DASH, MART]


def test_a_cross_model_test_root_is_scored_as_its_attached_model(
    repo_settings: Settings,
) -> None:
    nodes, registry = _nodes(), _registry()
    graph = build_graph(nodes)
    as_test = score_incident(graph, REL, nodes, _tiers(registry, nodes), repo_settings)
    as_model = score_incident(graph, MART, nodes, _tiers(registry, nodes), repo_settings)
    assert as_test == as_model


def test_severity_thresholds(repo_settings: Settings) -> None:
    levels = repo_settings.incidents.severity
    assert severity_for(levels.sev1_min_score, repo_settings) == "SEV1"
    assert severity_for(levels.sev1_min_score - 1, repo_settings) == "SEV2"
    assert severity_for(levels.sev2_min_score, repo_settings) == "SEV2"
    assert severity_for(levels.sev2_min_score - 1, repo_settings) == "SEV3"


# -- routing ---------------------------------------------------------------------------


def test_a_staging_root_pages_the_owner_of_its_source() -> None:
    nodes, registry = _nodes(), _registry()
    assert (route(STG, nodes, registry).owner, route(STG, nodes, registry).routed_via) == (
        "priya", SRC,
    )  # fmt: skip
    assert naive_route(STG, nodes, registry).owner == "marco"


def test_other_roots_page_their_own_owner() -> None:
    nodes, registry = _nodes(), _registry()
    assert route(MART, nodes, registry).owner == "sam"
    assert route(SRC, nodes, registry).owner == "priya"
    assert route(REL, nodes, registry).owner == "sam"


# -- lifecycle -------------------------------------------------------------------------


def test_response_is_deterministic_and_slower_when_busy(repo_settings: Settings) -> None:
    life = repo_settings.incidents.lifecycle
    first = lifecycle.respond("INC-001", "SEV2", AT, 0, life, seed=42)
    again = lifecycle.respond("INC-001", "SEV2", AT, 0, life, seed=42)
    busy = lifecycle.respond("INC-001", "SEV2", AT, 2, life, seed=42)
    assert first == again
    assert AT < first.acknowledged_at < first.resolved_at
    assert busy.acknowledged_at - AT > first.acknowledged_at - AT
    assert busy.resolved_at - AT > first.resolved_at - AT


@pytest.mark.parametrize("severity", SEVERITIES)
def test_response_stays_within_the_spread(repo_settings: Settings, severity: Severity) -> None:
    life = repo_settings.incidents.lifecycle
    for n in range(50):
        response = lifecycle.respond(f"INC-{n:03d}", severity, AT, 0, life, seed=7)
        ack = response.acknowledged_at - AT
        base = timedelta(minutes=life.ack_minutes[severity])
        assert base * 0.5 <= ack <= base * 1.5


# -- notifier --------------------------------------------------------------------------


def test_slack_url_comes_from_env_then_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(notify.SLACK_ENV_VAR, raising=False)
    env_file = tmp_path / ".env"
    assert notify.slack_webhook_url(env_file) is None
    env_file.write_text("# SLACK_WEBHOOK_URL=https://commented\nOTHER=1\n", encoding="utf-8")
    assert notify.slack_webhook_url(env_file) is None
    env_file.write_text("SLACK_WEBHOOK_URL='https://hooks.example/x'\n", encoding="utf-8")
    assert notify.slack_webhook_url(env_file) == "https://hooks.example/x"
    monkeypatch.setenv(notify.SLACK_ENV_VAR, "https://hooks.example/env")
    assert notify.slack_webhook_url(env_file) == "https://hooks.example/env"


def test_without_a_webhook_only_the_local_notifier_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(notify.SLACK_ENV_VAR, raising=False)
    local = notify.LocalNotifier()
    assert notify.build_notifier(local, tmp_path / ".env") is local
