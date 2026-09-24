"""Root-cause grouping on hand-built lineage graphs."""

from __future__ import annotations

from datetime import datetime

import networkx as nx

from platform_ops.incidents.grouping import assign, group_failures, roots_by_node
from platform_ops.incidents.ingest import CheckEvent

AT = datetime(2026, 1, 6, 2)


def _graph(*edges: tuple[str, str]) -> nx.DiGraph[str]:
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_edges_from(edges)
    return graph


def _fail(subject: str, check: str | None = None, run: str = "r1") -> CheckEvent:
    check_id = check or f"test.{subject}"
    return CheckEvent(run, check_id, "test", subject, "fail", "", AT)


def test_a_chain_of_failures_is_one_incident_at_the_top() -> None:
    graph = _graph(("src", "stg"), ("stg", "int"), ("int", "mart"))
    groups = group_failures(graph, [_fail("stg"), _fail("int"), _fail("mart")])
    assert [g.root for g in groups] == ["stg"]
    assert groups[0].failed_nodes == ("int", "mart", "stg")
    assert len(groups[0].events) == 3


def test_a_diamond_collapses_to_its_single_root() -> None:
    #       a
    #      / \
    #     b   c
    #      \ /
    #       d
    graph = _graph(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
    groups = group_failures(graph, [_fail(n) for n in "abcd"])
    assert [g.root for g in groups] == ["a"]
    assert groups[0].failed_nodes == ("a", "b", "c", "d")


def test_a_diamond_without_its_top_takes_the_smallest_id() -> None:
    # b and c fail independently; d is below both. d must go to exactly one of
    # them, and always the same one.
    graph = _graph(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
    groups = group_failures(graph, [_fail("c"), _fail("b"), _fail("d")])
    assert [(g.root, g.failed_nodes) for g in groups] == [("b", ("b", "d")), ("c", ("c",))]


def test_two_checks_on_one_node_are_one_incident() -> None:
    graph = _graph(("src", "stg"))
    events = [_fail("stg", "test.not_null"), _fail("stg", "test.unique")]
    groups = group_failures(graph, events)
    assert len(groups) == 1
    assert [e.check_id for e in groups[0].events] == ["test.not_null", "test.unique"]


def test_siblings_with_no_failed_ancestor_are_separate_roots() -> None:
    graph = _graph(("src", "x"), ("src", "y"), ("x", "x2"), ("y", "y2"))
    groups = group_failures(graph, [_fail("x"), _fail("y2"), _fail("x2")])
    assert [(g.root, g.failed_nodes) for g in groups] == [("x", ("x", "x2")), ("y2", ("y2",))]


def test_a_failure_far_below_skips_healthy_nodes_to_find_its_root() -> None:
    graph = _graph(("a", "b"), ("b", "c"), ("c", "d"))
    assert roots_by_node(graph, ["a", "d"]) == {"a": "a", "d": "a"}


def test_a_cross_model_test_hangs_below_both_models() -> None:
    # A relationships test reads both models. If orders fail, it belongs to
    # orders even though it is attached to order items.
    graph = _graph(
        ("raw.orders", "stg_orders"),
        ("raw.items", "stg_items"),
        ("stg_orders", "rel_test"),
        ("stg_items", "rel_test"),
    )
    groups = group_failures(graph, [_fail("stg_orders"), _fail("rel_test")])
    assert [g.root for g in groups] == ["stg_orders"]


def test_skipped_nodes_are_impact_of_their_root() -> None:
    graph = _graph(("src", "stg"), ("stg", "int"), ("int", "mart"), ("src", "other"))
    events = [CheckEvent("r1", "stg", "model", "stg", "error", "", AT)]
    groups = group_failures(graph, events, skipped=["int", "mart", "other"])
    assert len(groups) == 1
    assert groups[0].skipped == ("int", "mart")
    assert groups[0].failed_nodes == ("stg",)


def test_a_failure_in_the_next_run_appends_to_the_open_incident() -> None:
    graph = _graph(("src", "stg"), ("stg", "mart"))
    first = group_failures(graph, [_fail("stg", run="r1"), _fail("mart", run="r1")])
    opened = assign(first, {})
    assert [g.root for g in opened.new] == ["stg"] and not opened.appended

    second = group_failures(graph, [_fail("stg", run="r2")])
    again = assign(second, {"stg": "INC-0001"})
    assert not again.new
    assert [(incident, g.root) for incident, g in again.appended] == [("INC-0001", "stg")]


def test_a_new_root_next_to_an_open_incident_opens_its_own() -> None:
    graph = _graph(("src", "a"), ("src", "b"))
    result = assign(group_failures(graph, [_fail("a"), _fail("b")]), {"a": "INC-0001"})
    assert [g.root for g in result.new] == ["b"]
    assert [i for i, _ in result.appended] == ["INC-0001"]


def test_grouping_ignores_event_order() -> None:
    graph = _graph(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
    events = [_fail(n) for n in "dcba"]
    assert group_failures(graph, events) == group_failures(graph, list(reversed(events)))
