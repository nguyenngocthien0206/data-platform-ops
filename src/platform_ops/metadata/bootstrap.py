"""A starter ownership registry, written from a dbt manifest.

Writing hundreds of rules by hand is what stalls a rollout, so
``platform-ops metadata init-registry`` writes one rule per natural group of
datasets instead: a source system, a folder of models, an installed package,
the seeds, the snapshots, each exposure. Every rule names ``TODO`` as its owner
and team, so the first ``metadata check`` lists one problem per rule, a short
to-do list, rather than one per dataset.

Rules match unique ids, which carry no folder, so a folder becomes the longest
name prefix its models share that matches no model outside it. When there is
no such prefix the folder's models get one exact rule each. Every dataset ends
up with exactly one matching rule and no two rules tie, so the check can only
ever report the TODOs, never ambiguity (ADR 0001).
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from platform_ops.metadata.manifest import OWNED_TYPES, Node

TODO = "TODO"
DEFAULT_TIER = "important"


@dataclass(frozen=True)
class StarterRule:
    match: str
    comment: str
    owner: str = TODO


def _token_prefix(name: str, tokens: int) -> str:
    """The first ``tokens`` underscore-separated parts of a name, with their underscores."""
    parts = re.findall(r"[^_]+_*", name)
    return "".join(parts[:tokens])


def _cover(names: list[str], all_ids: list[str], root_project: str, depth: int) -> list[str]:
    """Patterns that match exactly ``names`` among ``all_ids``: as few as name prefixes allow.

    Try the whole group's shared prefix first; when it would also catch a model
    outside the group, split the group by one more name token (``rpt_``, then
    ``rpt_revenue_``) and try again, down to one exact rule per model.
    """
    if len(names) == 1:
        return [f"model.{root_project}.{names[0]}"]
    inside = {f"model.{root_project}.{name}" for name in names}
    prefix = os.path.commonprefix(names)
    pattern = f"model.{root_project}.{prefix}*"
    if prefix and not any(fnmatchcase(i, pattern) for i in all_ids if i not in inside):
        return [pattern]
    groups: dict[str, list[str]] = defaultdict(list)
    for name in names:
        groups[_token_prefix(name, depth + 1)].append(name)
    if len(groups) == 1 and all(_token_prefix(n, depth + 1) == n for n in names):
        return sorted(inside)
    return [
        p for key in sorted(groups) for p in _cover(groups[key], all_ids, root_project, depth + 1)
    ]


def _model_rules(models: list[Node], root_project: str) -> list[StarterRule]:
    """Root-project models, grouped by folder, then by name prefix within it."""
    by_folder: dict[str, list[Node]] = defaultdict(list)
    for node in models:
        by_folder[str(PurePosixPath(node.path).parent)].append(node)
    all_ids = sorted(node.unique_id for node in models)
    rules: list[StarterRule] = []
    for folder, members in sorted(by_folder.items()):
        names = sorted(node.name for node in members)
        label = f"{folder} ({len(names)} model{'s' if len(names) > 1 else ''})"
        patterns = _cover(names, all_ids, root_project, depth=0)
        rules.extend(StarterRule(p, label if i == 0 else "") for i, p in enumerate(patterns))
    return rules


def starter_rules(nodes: dict[str, Node], root_project: str) -> list[StarterRule]:
    """One rule per group of owned datasets, in a stable order."""
    owned = [n for n in nodes.values() if n.resource_type in OWNED_TYPES]
    rules: list[StarterRule] = []

    sources: dict[tuple[str, str], int] = defaultdict(int)
    for node in owned:
        if node.resource_type == "source":
            _, package, source_name, _table = node.unique_id.split(".", 3)
            sources[(package, source_name)] += 1
    for (package, source_name), count in sorted(sources.items()):
        rules.append(
            StarterRule(
                f"source.{package}.{source_name}.*",
                f"source {source_name} ({count} table{'s' if count > 1 else ''})",
            )
        )

    for resource_type in ("seed", "snapshot"):
        packages = sorted({n.package for n in owned if n.resource_type == resource_type})
        rules.extend(
            StarterRule(f"{resource_type}.{package}.*", f"{resource_type}s of {package}")
            for package in packages
        )

    models = [n for n in owned if n.resource_type == "model"]
    rules.extend(_model_rules([n for n in models if n.package == root_project], root_project))
    rules.extend(
        StarterRule(f"model.{package}.*", f"models from the installed package {package}")
        for package in sorted({n.package for n in models if n.package != root_project})
    )

    for node in sorted(
        (n for n in owned if n.resource_type == "exposure"), key=lambda n: n.unique_id
    ):
        owner = node.owner_name or TODO
        rules.append(StarterRule(node.unique_id, f"exposure {node.name}", owner))
    return rules


def render_ownership(rules: list[StarterRule], root_project: str) -> str:
    lines = [
        f"# Starter ownership registry for {root_project}, written by",
        "# `platform-ops metadata init-registry`. Replace every TODO with an owner and",
        "# the id of a team in teams.yaml, and split a rule when a group has more than",
        "# one owner: the most specific matching rule wins (ADR 0001). Run",
        "# `platform-ops metadata check --report-only` to see what is left.",
        "datasets:",
    ]
    for rule in rules:
        if rule.comment:
            lines.append(f"  # {rule.comment}")
        lines.append(
            f"  - {{match: {json.dumps(rule.match)}, owner: {json.dumps(rule.owner)}, "
            f"team: {json.dumps(TODO)}, tier: {DEFAULT_TIER}}}"
        )
    return "\n".join(lines) + "\n"


TEAMS_TEMPLATE = """\
# Teams and their members. Every owner named in ownership.yaml must be a member
# of the team named on the same rule. `channel` is the team's Slack channel;
# `telegram_chat` is optional, for teams that take their alerts on Telegram.
teams: []
#  - id: platform
#    name: Data Platform
#    channel: "#data-platform"
#    telegram_chat: "-1001234567890"
#    members: [alice, bob]
"""


def write_registry(nodes: dict[str, Node], root_project: str, out: Path, force: bool) -> list[Path]:
    """Write ``teams.yaml`` and ``ownership.yaml`` under ``out`` and return their paths."""
    teams_path, ownership_path = out / "teams.yaml", out / "ownership.yaml"
    existing = [p for p in (teams_path, ownership_path) if p.exists()]
    if existing and not force:
        names = ", ".join(str(p) for p in existing)
        raise FileExistsError(f"{names} already exists; pass --force to overwrite")
    out.mkdir(parents=True, exist_ok=True)
    rules = starter_rules(nodes, root_project)
    teams_path.write_text(TEAMS_TEMPLATE, encoding="utf-8", newline="\n")
    ownership_path.write_text(render_ownership(rules, root_project), encoding="utf-8", newline="\n")
    return [teams_path, ownership_path]
