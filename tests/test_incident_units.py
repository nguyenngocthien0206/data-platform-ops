"""Ingestion, severity, routing, lifecycle and notifier units, on hand-built inputs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from platform_ops.common.config import SEVERITIES, Settings, Severity, Tier
from platform_ops.incidents import ingest, lifecycle, notify
from platform_ops.incidents.desk import _healthy
from platform_ops.incidents.production import rerun_commands
from platform_ops.incidents.routing import naive_route, route
from platform_ops.incidents.severity import IncidentPolicy, score_incident, severity_for
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
            "test.company.not_null_stg_orders_id",
            "test",
            "not_null_stg_orders_id",
            depends_on=("macro.dbt.test_not_null", STG),
            attached_node=STG,
        ),
        REL: Node(
            REL, "test", "relationships_items_orders", depends_on=(MART, STG), attached_node=MART
        ),
    }


def _registry() -> Registry:
    return Registry(
        teams={
            "platform": Team(
                id="platform", name="Platform", channel="#platform", members=["priya", "marco"]
            ),
            "sales": Team(id="sales", name="Sales", channel="#sales", members=["sam"]),
        },
        rules=(
            DatasetRule(
                match="source.company.raw.*", owner="priya", team="platform", tier="critical"
            ),
            DatasetRule(
                match="model.company.stg_*", owner="marco", team="platform", tier="important"
            ),
            DatasetRule(match="model.company.sales_*", owner="sam", team="sales", tier="critical"),
            DatasetRule(
                match="exposure.company.sales_*", owner="sam", team="sales", tier="important"
            ),
        ),
    )


# -- ingestion -------------------------------------------------------------------------


def test_run_results_keep_failures_and_skips_only() -> None:
    data = {
        "results": [
            {"unique_id": STG, "status": "error", "message": "Binder Error:\n  column"},
            {"unique_id": MART, "status": "skipped", "message": None},
            {
                "unique_id": "test.company.not_null_stg_orders_id",
                "status": "fail",
                "message": "Got 3 results",
            },
            {"unique_id": REL, "status": "pass", "message": None},
            {"unique_id": "model.company.not_in_manifest", "status": "error", "message": ""},
        ]
    }
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


VOLUME = "test.company.source_recent_volume_raw_orders_"


def test_a_test_on_a_source_is_about_the_source() -> None:
    """dbt attaches no node to a source's tests: the source is still the subject."""
    volume = Node(VOLUME, "test", "source_recent_volume_raw_orders_", depends_on=(SRC,))
    assert ingest.test_subject(volume) == SRC


def _context(nodes: dict[str, Node]) -> Any:
    tests: dict[str, set[str]] = {}
    for unique_id, node in nodes.items():
        if node.resource_type == "test":
            subject = node.attached_node or ingest.test_subject(node)
            if subject != unique_id:
                tests.setdefault(subject, set()).add(unique_id)
    return SimpleNamespace(nodes=nodes, tests_of={k: frozenset(v) for k, v in tests.items()})


def test_a_source_is_healthy_when_fresh_and_its_tests_pass() -> None:
    nodes = {**_nodes(), VOLUME: Node(VOLUME, "test", "v", depends_on=(SRC,))}
    context = _context(nodes)
    assert not _healthy(SRC, context, {SRC: "pass"}), "its volume test has not run yet"
    assert not _healthy(SRC, context, {SRC: "pass", VOLUME: "fail"})
    assert not _healthy(SRC, context, {SRC: "error", VOLUME: "pass"})
    assert _healthy(SRC, context, {SRC: "warn", VOLUME: "pass"})


def test_a_rerun_rebuilds_the_root_and_everything_below_it() -> None:
    nodes = _nodes()
    keep = ("--exclude", "resource_type:snapshot")
    assert rerun_commands(nodes, SRC) == (
        ("source", "freshness", "--select", "source:raw.orders"),
        ("build", "--select", "source:raw.orders+", *keep),
    )
    assert rerun_commands(nodes, STG) == (("build", "--select", "stg_orders+", *keep),)
    # A test root: the model it is attached to, which reruns the test too.
    assert rerun_commands(nodes, REL) == (("build", "--select", "sales_fct_orders+", *keep),)
    assert rerun_commands(nodes, "model.company.gone") == ()


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
    merged = ingest.merge(
        [
            ingest.RunOutcome((event,), (MART,), 3),
            ingest.RunOutcome((event,), (), 4),
        ]
    )
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
    policy = IncidentPolicy.load(repo_settings)
    scored = score_incident(graph, STG, nodes, _tiers(registry, nodes), policy)
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
    policy = IncidentPolicy.load(repo_settings)
    as_test = score_incident(graph, REL, nodes, _tiers(registry, nodes), policy)
    as_model = score_incident(graph, MART, nodes, _tiers(registry, nodes), policy)
    assert as_test == as_model


def test_severity_thresholds(repo_settings: Settings) -> None:
    policy = IncidentPolicy.load(repo_settings)
    levels = policy.severity
    assert severity_for(levels.sev1_min_score, policy) == "SEV1"
    assert severity_for(levels.sev1_min_score - 1, policy) == "SEV2"
    assert severity_for(levels.sev2_min_score, policy) == "SEV2"
    assert severity_for(levels.sev2_min_score - 1, policy) == "SEV3"


def test_a_team_sets_its_own_thresholds_in_incidents_yaml(
    repo_settings: Settings, tmp_path: Path
) -> None:
    """Next to the registry, overriding the settings' defaults only where it says so."""
    default = IncidentPolicy.load(repo_settings, tmp_path)
    assert default.severity == repo_settings.incidents.severity
    policy_file = tmp_path / "incidents.yaml"
    policy_file.write_text(
        "severity: {root_tier_multiplier: 1, sev1_min_score: 9, sev2_min_score: 3}",
        encoding="utf-8",
    )
    policy = IncidentPolicy.load(repo_settings, tmp_path)
    assert (policy.severity.sev1_min_score, policy.severity.sev2_min_score) == (9, 3)
    assert policy.tier_weights == repo_settings.metadata.tier_weights
    policy_file.write_text("pages: always", encoding="utf-8")
    with pytest.raises(ValueError, match="pages"):
        IncidentPolicy.load(repo_settings, tmp_path)


# -- routing ---------------------------------------------------------------------------


def test_a_staging_root_pages_the_owner_of_its_source() -> None:
    nodes, registry = _nodes(), _registry()
    assert (route(STG, nodes, registry).owner, route(STG, nodes, registry).routed_via) == (
        "priya",
        SRC,
    )
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
    assert AT < first.acknowledged_at < first.fixed_at
    assert busy.acknowledged_at - AT > first.acknowledged_at - AT
    assert busy.fixed_at - AT > first.fixed_at - AT


@pytest.mark.parametrize("severity", SEVERITIES)
def test_response_stays_within_the_spread(repo_settings: Settings, severity: Severity) -> None:
    life = repo_settings.incidents.lifecycle
    for n in range(50):
        response = lifecycle.respond(f"INC-{n:03d}", severity, AT, 0, life, seed=7)
        ack = response.acknowledged_at - AT
        base = timedelta(minutes=life.ack_minutes[severity])
        assert base * 0.5 <= ack <= base * 1.5


# -- notifier --------------------------------------------------------------------------


NOTE = notify.Notification(
    "INC-001", "page", "night-1", "priya", "platform", "SEV1", "2 checks failing", AT
)
TEAM = Team(id="platform", name="P", channel="#data-platform", members=["priya"])


def test_channels_come_from_their_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SLACK_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "SLACK_API_URL", "TELEGRAM_API_URL"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    assert notify.channels_from_env(env_file) == []
    env_file.write_text("TELEGRAM_BOT_TOKEN=t-1\n# SLACK_BOT_TOKEN=commented\n", encoding="utf-8")
    assert [c.name for c in notify.channels_from_env(env_file)] == ["telegram"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    assert [c.name for c in notify.channels_from_env(env_file)] == ["slack", "telegram"]


def test_without_a_channel_a_notification_stays_local() -> None:
    assert notify.deliver(NOTE, TEAM, []) == "local"


def test_a_team_without_a_telegram_chat_is_skipped_there() -> None:
    telegram = notify.TelegramChannel("t-1", "http://127.0.0.1:9")
    assert notify.deliver(NOTE, TEAM, [telegram]) == "telegram: no address"


def test_a_failed_send_is_recorded_not_raised() -> None:
    slack = notify.SlackChannel("xoxb-1", "http://127.0.0.1:9")  # nothing listens there
    assert notify.deliver(NOTE, TEAM, [slack]) == "slack failed"
