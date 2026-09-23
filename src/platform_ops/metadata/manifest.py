"""A small, typed view of dbt's manifest.json.

The manifest is large and version-specific. Everything the metadata layer needs
from it is pulled into :class:`Node` here, so the registry and lineage code do
not depend on the manifest's layout, and a fixture graph in a test looks exactly
like a real one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The resource types the metadata layer reasons about. Seeds, snapshots,
# analyses and semantic objects are not used by this project.
OWNED_TYPES: tuple[str, ...] = ("source", "model", "exposure")
GRAPH_TYPES: tuple[str, ...] = ("source", "model", "test", "exposure")


@dataclass(frozen=True)
class Node:
    unique_id: str
    resource_type: str
    name: str
    path: str = ""
    depends_on: tuple[str, ...] = ()
    owner_name: str | None = None

    @property
    def folder(self) -> str:
        """First folder under models/, for example ``abandoned`` or ``marts``."""
        parts = Path(self.path).parts
        return parts[1] if len(parts) > 2 and parts[0] == "models" else ""


def _node_from(raw: dict[str, Any]) -> Node:
    owner = raw.get("owner") or {}
    return Node(
        unique_id=raw["unique_id"],
        resource_type=raw["resource_type"],
        name=raw["name"],
        path=str(raw.get("original_file_path", "")).replace("\\", "/"),
        depends_on=tuple(sorted(raw.get("depends_on", {}).get("nodes", []) or [])),
        owner_name=owner.get("name") if isinstance(owner, dict) else None,
    )


def load_manifest(path: Path) -> dict[str, Node]:
    """Read a manifest and return every source, model, test and exposure by id."""
    if not path.is_file():
        raise FileNotFoundError(
            f"No dbt manifest at {path}. Run `make build`, or pass --parse to generate one."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes: dict[str, Node] = {}
    for section in ("nodes", "sources", "exposures"):
        for raw in data.get(section, {}).values():
            if raw.get("resource_type") in GRAPH_TYPES:
                node = _node_from(raw)
                nodes[node.unique_id] = node
    return nodes
