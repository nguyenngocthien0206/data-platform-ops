"""The starter registry: few rules, every dataset covered once, never a tie."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from platform_ops.metadata.bootstrap import TODO, starter_rules, write_registry
from platform_ops.metadata.check import run_check
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import OwnershipFile, Registry, TeamsFile


def model(name: str, folder: str, package: str = "acme") -> Node:
    return Node(
        unique_id=f"model.{package}.{name}",
        resource_type="model",
        name=name,
        path=f"models/{folder}/{name}.sql",
        package=package,
    )


def nodes_of(*nodes: Node) -> dict[str, Node]:
    return {node.unique_id: node for node in nodes}


NODES = nodes_of(
    Node("source.acme.shop.orders", "source", "orders", package="acme"),
    Node("source.acme.shop.customers", "source", "customers", package="acme"),
    Node("source.acme.billing.invoices", "source", "invoices", package="acme"),
    Node("seed.acme.country_codes", "seed", "country_codes", package="acme"),
    Node("snapshot.acme.snap_customers", "snapshot", "snap_customers", package="acme"),
    model("stg_shop__orders", "staging/shop"),
    model("stg_shop__customers", "staging/shop"),
    model("fct_orders", "core"),
    model("fct_invoices", "core"),
    model("dim_customers", "core"),
    model("rpt_revenue_daily", "reporting/finance"),
    model("rpt_revenue_monthly", "reporting/finance"),
    model("rpt_unpaid_invoices", "reporting/finance"),
    model("rpt_new_customers", "reporting/growth"),
    model("calendar", "shared", package="shared_pkg"),
    Node("exposure.acme.revenue_board", "exposure", "revenue_board", owner_name="linh"),
    Node("test.acme.unique_x", "test", "unique_x"),
)


def test_groups_become_patterns_and_folders_split_by_name_prefix() -> None:
    matches = [rule.match for rule in starter_rules(NODES, "acme")]
    assert matches == [
        "source.acme.billing.*",
        "source.acme.shop.*",
        "seed.acme.*",
        "snapshot.acme.*",
        # models/core: no prefix shared by all three, so one more name token
        "model.acme.dim_customers",
        "model.acme.fct_*",
        # models/reporting/finance: 'rpt_' would also catch growth's model
        "model.acme.rpt_revenue_*",
        "model.acme.rpt_unpaid_invoices",
        "model.acme.rpt_new_customers",
        "model.acme.stg_shop__*",
        "model.shared_pkg.*",
        "exposure.acme.revenue_board",
    ]


def test_every_dataset_resolves_to_exactly_one_rule_and_tests_are_left_alone() -> None:
    rules = starter_rules(NODES, "acme")
    data = {
        "datasets": [
            {"match": r.match, "owner": "an", "team": "platform", "tier": "important"}
            for r in rules
        ]
    }
    teams = TeamsFile.model_validate(
        {"teams": [{"id": "platform", "name": "P", "channel": "#p", "members": ["an", "linh"]}]}
    )
    registry = Registry(
        teams={t.id: t for t in teams.teams},
        rules=tuple(OwnershipFile.model_validate(data).datasets),
    )
    for node in NODES.values():
        if node.resource_type != "test":
            resolution = registry.resolve(node.unique_id)
            assert resolution.owned, node.unique_id
            assert len(resolution.candidates) == 1, node.unique_id


def test_the_written_registry_lists_one_todo_per_rule_and_counts_nothing_as_owned(
    tmp_path: Path,
) -> None:
    write_registry(NODES, "acme", tmp_path, force=False)
    registry = Registry.from_config_dir(tmp_path)
    rules = yaml.safe_load((tmp_path / "ownership.yaml").read_text(encoding="utf-8"))["datasets"]
    assert {(r["team"], r["tier"]) for r in rules} == {(TODO, "important")}
    assert next(r for r in rules if r["match"] == "exposure.acme.revenue_board")["owner"] == "linh"
    report = run_check(registry, NODES)
    assert len(report.errors) == len(rules)
    assert all("names unknown team 'TODO'" in error for error in report.errors)
    assert all(owned == 0 for owned, _ in report.coverage.values())
    assert report.warnings == []


def test_an_existing_registry_is_never_overwritten_silently(tmp_path: Path) -> None:
    write_registry(NODES, "acme", tmp_path, force=False)
    (tmp_path / "ownership.yaml").write_text("datasets: []\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="--force"):
        write_registry(NODES, "acme", tmp_path, force=False)
    assert (tmp_path / "ownership.yaml").read_text(encoding="utf-8") == "datasets: []\n"
    write_registry(NODES, "acme", tmp_path, force=True)
    assert "TODO" in (tmp_path / "ownership.yaml").read_text(encoding="utf-8")


def test_the_output_is_the_same_on_every_run(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    write_registry(NODES, "acme", first, force=False)
    write_registry(dict(reversed(list(NODES.items()))), "acme", second, force=False)
    for name in ("teams.yaml", "ownership.yaml"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
