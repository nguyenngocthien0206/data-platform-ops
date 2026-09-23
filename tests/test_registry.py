from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from platform_ops.metadata.check import run_check
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import (
    DatasetRule,
    OwnershipFile,
    Registry,
    Team,
    TeamsFile,
    specificity,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

TEAMS = {
    "platform": Team(id="platform", name="Platform", channel="#p", members=["priya"]),
    "finance": Team(id="finance", name="Finance", channel="#f", members=["alice", "bruno"]),
}


def rule(
    match: str, owner: str = "bruno", team: str = "finance", tier: str = "important"
) -> DatasetRule:
    return DatasetRule.model_validate({"match": match, "owner": owner, "team": team, "tier": tier})


def registry(*rules: DatasetRule) -> Registry:
    return Registry(teams=TEAMS, rules=tuple(rules))


def model(name: str) -> Node:
    return Node(unique_id=f"model.company.{name}", resource_type="model", name=name)


# --- specificity -------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("model.company.finance_fct_revenue", (33, 0)),
        ("model.company.finance_*", (22, 1)),
        ("model.company.*_v?", (16, 2)),
        ("model.company.stg_[ab]*", (18, 2)),
        ("model.company.[broken", (21, 0)),  # fnmatch reads an unclosed '[' literally
    ],
)
def test_specificity_counts_literals_and_wildcards(pattern: str, expected: tuple[int, int]) -> None:
    assert specificity(pattern) == expected


def test_more_literal_characters_win() -> None:
    reg = registry(
        rule("model.company.finance_*", owner="bruno"),
        rule("model.company.finance_fct_revenue*", owner="alice", tier="critical"),
    )
    resolution = reg.resolve("model.company.finance_fct_revenue_monthly")
    assert resolution.rule is not None
    assert resolution.rule.owner == "alice"
    assert not resolution.ambiguous


def test_exact_match_beats_a_glob_that_also_matches() -> None:
    """The finance_fct_revenue_v0 case: demoted by a rule more specific than the critical one."""
    reg = registry(
        rule("model.company.finance_fct_revenue*", owner="alice", tier="critical"),
        rule("model.company.finance_fct_revenue_v0", owner="bruno", tier="best_effort"),
    )
    resolution = reg.resolve("model.company.finance_fct_revenue_v0")
    assert resolution.rule is not None
    assert resolution.rule.tier == "best_effort"


def test_fewer_wildcards_break_a_tie_on_literals() -> None:
    fewer, more = "model.company.*finance_x", "model.company.*finance_x*"
    assert specificity(fewer)[0] == specificity(more)[0], "must tie on literals"
    reg = registry(rule(fewer, owner="alice"), rule(more, owner="bruno"))
    resolution = reg.resolve("model.company.finance_x")
    assert resolution.rule is not None
    assert resolution.rule.owner == "alice"


def test_equal_specificity_is_ambiguous_regardless_of_file_order() -> None:
    first = registry(
        rule("model.company.finance_*", owner="alice"),
        rule("model.company.*_revenue", owner="bruno"),
    )
    swapped = registry(*reversed(first.rules))
    for reg in (first, swapped):
        report = run_check(reg, {"m": model("finance_revenue")})
        assert not report.passed
        assert any("ambiguous" in error for error in report.errors)


def test_no_matching_rule_means_unowned() -> None:
    report = run_check(registry(rule("model.company.finance_*")), {"m": model("sales_orders")})
    assert "model.company.sales_orders has no owner" in report.errors


# --- validation --------------------------------------------------------------


def test_owner_must_be_a_member_of_the_named_team() -> None:
    reg = registry(rule("model.company.finance_*", owner="priya", team="finance"))
    assert any("not a member of team 'finance'" in problem for problem in reg.problems())


def test_unknown_team_is_reported() -> None:
    reg = registry(rule("model.company.x_*", owner="bruno", team="legal"))
    assert any("unknown team 'legal'" in problem for problem in reg.problems())


def test_bad_tier_is_rejected() -> None:
    with pytest.raises(ValidationError):
        rule("model.company.x", tier="urgent")


def test_duplicate_patterns_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate ownership patterns"):
        OwnershipFile.model_validate({"datasets": [rule("model.company.x").model_dump()] * 2})


def test_duplicate_team_ids_are_rejected() -> None:
    team = TEAMS["finance"].model_dump()
    with pytest.raises(ValidationError, match="duplicate team ids"):
        TeamsFile.model_validate({"teams": [team, team]})


def test_team_needs_at_least_one_member() -> None:
    with pytest.raises(ValidationError):
        Team(id="empty", name="Empty", channel="#e", members=[])


# --- the check -----------------------------------------------------------------


def test_exposure_owner_must_agree_with_the_registry() -> None:
    exposure = Node(
        unique_id="exposure.company.finance_board_pack",
        resource_type="exposure",
        name="finance_board_pack",
        owner_name="alice",
    )
    reg = registry(rule("exposure.company.finance_*", owner="bruno"))
    report = run_check(reg, {exposure.unique_id: exposure})
    assert any("declares owner 'alice'" in error for error in report.errors)


def test_stale_rule_warns_but_does_not_fail() -> None:
    reg = registry(rule("model.company.finance_*"), rule("model.company.finance_gone"))
    report = run_check(reg, {"m": model("finance_revenue")})
    assert report.passed
    assert any("finance_gone" in warning for warning in report.warnings)


def test_tests_are_not_required_to_have_owners() -> None:
    test_node = Node(unique_id="test.company.unique_x", resource_type="test", name="unique_x")
    report = run_check(registry(), {test_node.unique_id: test_node})
    assert report.passed


def test_coverage_counts_owned_nodes_per_type() -> None:
    nodes = {"a": model("finance_a"), "b": model("sales_b")}
    report = run_check(registry(rule("model.company.finance_*")), nodes)
    assert report.coverage["model"] == (1, 2)


# --- the real config -----------------------------------------------------------


def test_real_config_loads_without_problems() -> None:
    reg = Registry.from_config_dir(CONFIG_DIR)
    assert reg.problems() == []
    assert {"platform", "sales", "marketing", "finance", "product"} == set(reg.teams)


def test_every_person_is_on_exactly_one_team() -> None:
    teams = yaml.safe_load((CONFIG_DIR / "teams.yaml").read_text(encoding="utf-8"))["teams"]
    people = [person for team in teams for person in team["members"]]
    assert len(people) == len(set(people))
    reg = Registry.from_config_dir(CONFIG_DIR)
    assert reg.team_of("alice") == "finance"
    assert reg.team_of("nobody") is None
