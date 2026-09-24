"""How bad an incident is: the root's own tier plus everything it reaches.

``score = root_tier_multiplier x tier_weight(root) + sum of tier weights of the
models and exposures downstream of the root``. The first term says how much the
broken dataset matters in itself; the second says how much of the business is
built on it. Thresholds in ``settings.yaml`` turn the score into SEV1 to SEV3.
ADR 0008 explains how they were calibrated against this project's lineage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import networkx as nx

from platform_ops.common.config import Settings, Severity, Tier
from platform_ops.metadata.lineage import Consumer, consumer_weight, downstream_consumers
from platform_ops.metadata.manifest import Node

DEFAULT_TIER: Tier = "best_effort"


@dataclass(frozen=True)
class Scored:
    score: int
    severity: Severity
    root_tier: Tier
    consumers: tuple[Consumer, ...]


def dataset_of(root: str, nodes: Mapping[str, Node]) -> str:
    """The dataset an incident root stands for.

    A root is normally a source or model. When it is a test that spans models
    (see ``ingest.test_subject``), it stands for the model it is attached to.
    """
    node = nodes.get(root)
    if node is not None and node.resource_type == "test" and node.attached_node:
        return node.attached_node
    return root


def severity_for(score: int, settings: Settings) -> Severity:
    thresholds = settings.incidents.severity
    if score >= thresholds.sev1_min_score:
        return "SEV1"
    if score >= thresholds.sev2_min_score:
        return "SEV2"
    return "SEV3"


def score_incident(
    graph: nx.DiGraph[str],
    root: str,
    nodes: Mapping[str, Node],
    tier_of: Mapping[str, Tier],
    settings: Settings,
) -> Scored:
    dataset = dataset_of(root, nodes)
    weights = settings.metadata.tier_weights
    root_tier = tier_of.get(dataset, DEFAULT_TIER)
    consumers = (
        tuple(downstream_consumers(graph, dataset, tier_of, weights, default_tier=DEFAULT_TIER))
        if dataset in graph
        else ()
    )
    score = settings.incidents.severity.root_tier_multiplier * weights.weight(root_tier)
    score += consumer_weight(consumers)
    return Scored(score, severity_for(score, settings), root_tier, consumers)
