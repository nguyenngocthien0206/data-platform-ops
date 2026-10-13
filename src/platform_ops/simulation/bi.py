"""The company's dashboards, generated from the dbt exposures (version 2.0, Phase 11b).

Every dbt exposure is a dashboard someone looks at, and its ``depends_on`` lists
the models that dashboard reads. This module turns that into dashboard
definitions: one dashboard per exposure, in its owning team's collection, with
one card per model. Metabase is built from them (``simulation.metabase``), and
Phase 13 replays the very same SQL as the BI load in the query log, so the
demo's BI traffic and a real Metabase's are one set of queries.

A card's SQL is decided by rule, from the model's columns in ClickHouse:

- a ``dashboard`` entry in the model's ``config.meta`` wins: ``display`` (line,
  bar or table), ``x``, ``y`` and an optional ``breakout`` column. Owners set
  it next to the model, so changing a chart is reviewed with the model;
- a generated rollup (``period_start``, a dimension, then its measures) is a
  line of its first measure per period, one line per dimension value. A daily
  rollup shows its last ``RECENT_DAYS`` days, the operations view it exists
  for: Metabase draws at most ``CHART_ROWS`` rows of a native query, and a
  year of days times ten countries would be cut off without a word
  (``metabase check`` fails a chart that reaches the limit);
- anything else is a table, sorted by the model's key, capped at
  ``TABLE_ROWS``.

Everything is derived from the manifest, the registry and the columns, so the
same project always yields the same dashboards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from platform_ops.metadata.manifest import load_manifest
from platform_ops.metadata.registry import Registry

TABLE_ROWS = 2000
# Metabase shows at most this many rows of a native query.
CHART_ROWS = 2000
# A daily rollup's chart covers this many days back from its latest one.
RECENT_DAYS = 91
Display = Literal["line", "bar", "table"]

_TIME = re.compile(r"^(Nullable\()?(Date|Date32|DateTime|DateTime64)\b")
_NUMBER = re.compile(r"^(Nullable\()?(U?Int\d+|Float\d+|Decimal)")


@dataclass(frozen=True)
class Column:
    name: str
    type: str

    @property
    def is_time(self) -> bool:
        return bool(_TIME.match(self.type))

    @property
    def is_number(self) -> bool:
        return bool(_NUMBER.match(self.type))


@dataclass(frozen=True)
class Card:
    model: str
    title: str
    sql: str
    display: Display
    dimensions: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dashboard:
    exposure: str
    name: str
    description: str
    team: str
    owner: str
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class ModelShape:
    """What a card needs to know about a model: where it is, its columns, its key."""

    name: str
    database: str
    columns: tuple[Column, ...]
    sorting_key: str = ""
    hint: dict[str, Any] | None = None


def _title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def _q(name: str) -> str:
    return f"`{name}`"


def card_for(model: ModelShape) -> Card:
    """The card that shows one model, by the rules in the module docstring."""
    table = f"{_q(model.database)}.{_q(model.name)}"
    names = [c.name for c in model.columns]
    if model.hint:
        display: Display = model.hint.get("display", "line")
        x = str(model.hint["x"])
        y = tuple(str(v) for v in model.hint["y"])
        breakout = model.hint.get("breakout")
        missing = [c for c in (x, *y, *([breakout] if breakout else [])) if c not in names]
        if missing:
            raise ValueError(f"{model.name}: dashboard hint names unknown columns {missing}")
        keys = (x, str(breakout)) if breakout else (x,)
        measures = ", ".join(f"sum({_q(m)}) AS {_q(m)}" for m in y)
        group = ", ".join(_q(k) for k in keys)
        sql = f"SELECT {group}, {measures} FROM {table} GROUP BY {group} ORDER BY {group}"
        return Card(model.name, _title(model.name), sql, display, keys, y)
    if "period_start" in names:
        rest = [c for c in model.columns if c.name != "period_start"]
        dimension = next((c.name for c in rest if not c.is_number), None)
        measure = next((c.name for c in rest if c.is_number), None)
        if measure is not None:
            keys = ("period_start", dimension) if dimension else ("period_start",)
            group = ", ".join(_q(k) for k in keys)
            recent = ""
            if "_daily_" in model.name:
                recent = (
                    f" WHERE `period_start` > (SELECT max(`period_start`) FROM {table})"
                    f" - INTERVAL {RECENT_DAYS} DAY"
                )
            sql = (
                f"SELECT {group}, sum({_q(measure)}) AS {_q(measure)} FROM {table}{recent} "
                f"GROUP BY {group} ORDER BY {group}"
            )
            return Card(model.name, _title(model.name), sql, "line", keys, (measure,))
    order = f" ORDER BY {model.sorting_key}" if model.sorting_key else ""
    sql = f"SELECT * FROM {table}{order} LIMIT {TABLE_ROWS}"
    return Card(model.name, _title(model.name), sql, "table")


def _extras(manifest_path: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    """Exposure labels and descriptions, and each model's ``dashboard`` hint."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    exposures = {
        uid: {"label": raw.get("label") or raw["name"], "description": raw.get("description", "")}
        for uid, raw in data.get("exposures", {}).items()
    }
    hints = {}
    for uid, raw in data.get("nodes", {}).items():
        meta = (raw.get("config") or {}).get("meta") or raw.get("meta") or {}
        if raw.get("resource_type") == "model" and "dashboard" in meta:
            hints[uid] = meta["dashboard"]
    return exposures, hints


def dashboards(
    manifest_path: Path, registry: Registry, shapes: dict[str, ModelShape]
) -> list[Dashboard]:
    """One dashboard per exposure, sorted by name; ``shapes`` maps model names to columns."""
    nodes = load_manifest(manifest_path)
    exposures, hints = _extras(manifest_path)
    found = []
    for uid in sorted(exposures):
        node = nodes[uid]
        rule = registry.resolve(uid).rule
        cards = []
        for parent in node.depends_on:
            model = nodes.get(parent)
            if model is None or model.resource_type != "model":
                continue
            shape = shapes[model.name]
            if parent in hints:
                shape = ModelShape(
                    shape.name, shape.database, shape.columns, shape.sorting_key, hints[parent]
                )
            cards.append(card_for(shape))
        found.append(
            Dashboard(
                exposure=uid,
                name=exposures[uid]["label"],
                description=exposures[uid]["description"],
                team=rule.team if rule else "",
                owner=rule.owner if rule else "",
                cards=tuple(cards),
            )
        )
    return sorted(found, key=lambda d: d.name)


def exposed_models(manifest_path: Path) -> list[str]:
    """Every model some exposure reads, by name, sorted."""
    nodes = load_manifest(manifest_path)
    names = {
        nodes[parent].name
        for node in nodes.values()
        if node.resource_type == "exposure"
        for parent in node.depends_on
        if parent in nodes and nodes[parent].resource_type == "model"
    }
    return sorted(names)


def model_shapes(client: Any, database: str, models: list[str]) -> dict[str, ModelShape]:
    """Columns and sorting key of each model, read from ClickHouse with ``client``."""
    columns = client.query(
        "SELECT table, name, type FROM system.columns "
        "WHERE database = %(db)s AND table IN %(models)s ORDER BY table, position",
        parameters={"db": database, "models": models},
    ).result_rows
    keys = dict(
        client.query(
            "SELECT name, sorting_key FROM system.tables WHERE database = %(db)s",
            parameters={"db": database},
        ).result_rows
    )
    by_model: dict[str, list[Column]] = {}
    for table, name, data_type in columns:
        by_model.setdefault(table, []).append(Column(name, data_type))
    missing = sorted(set(models) - set(by_model))
    if missing:
        raise ValueError(f"not built in {database}: {missing}; run `make demo` first")
    return {
        name: ModelShape(name, database, tuple(by_model[name]), str(keys.get(name) or ""))
        for name in models
    }
