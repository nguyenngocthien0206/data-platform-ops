"""Metabase as the company's BI, built from the dbt exposures (version 2.0, Phase 11b).

``platform-ops metabase provision`` makes a running Metabase match the dbt
project, and can be run again at any time:

1. first start: create the admin account from ``MB_ADMIN_EMAIL`` and
   ``MB_ADMIN_PASSWORD``; afterwards, log in with it;
2. one ClickHouse connection, as the company's ``metabase`` user, on the marts;
3. a "Company dashboards" collection with one collection per team;
4. one dashboard per exposure in its owning team's collection, one card per
   model it reads (``simulation.bi``), each card a native SQL question.

Cards and dashboards are matched by name and updated in place; anything left
in those collections that no exposure asks for any more is archived, so the
BI tool never drifts from the project. ``platform-ops metabase check`` then
runs every card of every dashboard through Metabase and fails on any error.

Metabase is never part of ``make demo`` or ``make test``, and its own queries
never feed a report. Its API is called with the standard library only, and
was checked against the pinned image (``docker-compose.yml``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.env import env_value
from platform_ops.common.servers import clickhouse_client, connect_postgres, require_passwords
from platform_ops.metadata.registry import Registry
from platform_ops.simulation import bi

ROOT_COLLECTION = "Company dashboards"
# Metabase's dashboard grid is 24 columns wide: two cards per row.
CARD_WIDTH, CARD_HEIGHT = 12, 6


class MetabaseError(RuntimeError):
    """Metabase could not be reached, set up, or asked; the message says what to do."""


def metabase_url(settings: Settings) -> str:
    env_file = settings.root / ".env"
    url = env_value("METABASE_URL", env_file)
    if url:
        return url.rstrip("/")
    return f"http://localhost:{env_value('METABASE_PORT', env_file, '3000')}"


def _admin(settings: Settings) -> tuple[str, str]:
    env_file = settings.root / ".env"
    email = env_value("MB_ADMIN_EMAIL", env_file)
    password = env_value("MB_ADMIN_PASSWORD", env_file)
    missing = [n for n, v in (("MB_ADMIN_EMAIL", email), ("MB_ADMIN_PASSWORD", password)) if not v]
    if missing or email is None or password is None:
        raise MetabaseError("set these in .env (see .env.example): " + ", ".join(missing))
    return email, password


def prepare_app_database(settings: Settings) -> None:
    """Create the Postgres database Metabase keeps its own state in. Safe to repeat."""
    database = settings.company.metabase_database
    with connect_postgres(settings) as connection:
        connection.autocommit = True
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [database]
        ).fetchone()
        if exists is None:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


@dataclass
class MetabaseApi:
    url: str
    session: str | None = None

    def call(self, method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(self.url + path, method=method)
        request.add_header("Content-Type", "application/json")
        if self.session:
            request.add_header("X-Metabase-Session", self.session)
        data = None if body is None else json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(request, data, timeout=300) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise MetabaseError(f"{method} {path} returned {error.code}: {detail}") from error
        except OSError as error:
            raise MetabaseError(
                f"Metabase is not reachable at {self.url} ({type(error).__name__}); "
                "start it with `make metabase`"
            ) from error
        return json.loads(raw) if raw else None

    def wait_until_healthy(self, seconds: int = 300) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                if (self.call("GET", "/api/health") or {}).get("status") == "ok":
                    return
            except MetabaseError:
                if time.monotonic() > deadline:
                    raise
            if time.monotonic() > deadline:
                raise MetabaseError(f"Metabase at {self.url} did not become healthy")
            time.sleep(2)


def login(settings: Settings) -> MetabaseApi:
    """A logged-in session, creating the admin account on the very first start."""
    api = MetabaseApi(metabase_url(settings))
    api.wait_until_healthy()
    email, password = _admin(settings)
    properties = api.call("GET", "/api/session/properties")
    if not properties.get("has-user-setup"):
        api.call(
            "POST",
            "/api/setup",
            {
                "token": properties["setup-token"],
                "user": {
                    "email": email,
                    "password": password,
                    "first_name": "Data",
                    "last_name": "Platform",
                    "site_name": "Company",
                },
                "prefs": {"site_name": "Company", "allow_tracking": False},
            },
        )
    api.session = api.call("POST", "/api/session", {"username": email, "password": password})["id"]
    return api


def _warehouse_name(settings: Settings) -> str:
    namespace = settings.company.namespace
    return f"Company warehouse ({namespace.rstrip('_')})" if namespace else "Company warehouse"


def ensure_warehouse(api: MetabaseApi, settings: Settings) -> int:
    """The ClickHouse connection, as the ``metabase`` user. Returns its id."""
    company = settings.company
    host, _, port = company.clickhouse_from_metabase.partition(":")
    payload = {
        "engine": "clickhouse",
        "name": _warehouse_name(settings),
        "details": {
            "host": host,
            "port": int(port or 8123),
            "user": company.name("metabase"),
            "password": require_passwords(settings, ["metabase"])["metabase"],
            "dbname": company.name("marts"),
            "ssl": False,
            "enable-multiple-db": False,
        },
    }
    existing = [d for d in api.call("GET", "/api/database")["data"] if d["name"] == payload["name"]]
    if existing:
        database_id = int(existing[0]["id"])
        api.call("PUT", f"/api/database/{database_id}", payload)
        return database_id
    return int(api.call("POST", "/api/database", payload)["id"])


def _collection(api: MetabaseApi, name: str, parent: int | None) -> int:
    for found in api.call("GET", "/api/collection"):
        location = found.get("location") or "/"
        wanted = "/" if parent is None else f"/{parent}/"
        if found.get("name") == name and location == wanted and not found.get("archived"):
            return int(found["id"])
    body: dict[str, Any] = {"name": name}
    if parent is not None:
        body["parent_id"] = parent
    return int(api.call("POST", "/api/collection", body)["id"])


def _items(api: MetabaseApi, collection: int, model: str) -> dict[str, int]:
    found = api.call("GET", f"/api/collection/{collection}/items?models={model}")
    rows = found["data"] if isinstance(found, dict) else found
    return {row["name"]: int(row["id"]) for row in rows}


@dataclass
class ProvisionSummary:
    dashboards: int = 0
    cards: int = 0
    archived: int = 0
    collections: list[str] = field(default_factory=list)


def _card_name(dashboard: bi.Dashboard, card: bi.Card) -> str:
    return f"{dashboard.name}: {card.title}"


def _card_payload(card: bi.Card, name: str, database_id: int, collection: int) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if card.display != "table":
        settings = {"graph.dimensions": list(card.dimensions), "graph.metrics": list(card.metrics)}
    return {
        "name": name,
        "collection_id": collection,
        "display": card.display,
        "dataset_query": {
            "type": "native",
            "native": {"query": card.sql},
            "database": database_id,
        },
        "visualization_settings": settings,
    }


def wanted_dashboards(settings: Settings) -> list[bi.Dashboard]:
    """The dashboards the dbt project asks for, from its manifest and the built marts."""
    manifest = invocation_from_settings(settings).manifest_path
    registry = Registry.from_config_dir(settings.root / "config")
    client = clickhouse_client(settings, "metabase")
    try:
        shapes = bi.model_shapes(
            client, settings.company.name("marts"), bi.exposed_models(manifest)
        )
    finally:
        client.close()
    return bi.dashboards(manifest, registry, shapes)


def provision(settings: Settings) -> ProvisionSummary:
    """Make Metabase show exactly the dashboards the dbt exposures describe."""
    dashboards = wanted_dashboards(settings)
    teams = Registry.from_config_dir(settings.root / "config").teams
    api = login(settings)
    database_id = ensure_warehouse(api, settings)
    root = _collection(api, ROOT_COLLECTION, None)
    summary = ProvisionSummary()
    for team in sorted({d.team for d in dashboards}):
        collection = _collection(api, teams[team].name, root)
        summary.collections.append(teams[team].name)
        mine = [d for d in dashboards if d.team == team]
        cards = _items(api, collection, "card")
        boards = _items(api, collection, "dashboard")
        keep_cards: set[str] = set()
        for dashboard in mine:
            placed = []
            for index, card in enumerate(dashboard.cards):
                name = _card_name(dashboard, card)
                payload = _card_payload(card, name, database_id, collection)
                if name in cards:
                    card_id = cards[name]
                    api.call("PUT", f"/api/card/{card_id}", payload)
                else:
                    card_id = int(api.call("POST", "/api/card", payload)["id"])
                keep_cards.add(name)
                placed.append(
                    {
                        "id": -(index + 1),
                        "card_id": card_id,
                        "row": (index // 2) * CARD_HEIGHT,
                        "col": (index % 2) * CARD_WIDTH,
                        "size_x": CARD_WIDTH,
                        "size_y": CARD_HEIGHT,
                        # The saved question's name is unique in the team's
                        # collection; on its dashboard, the short title is enough.
                        "visualization_settings": {"card.title": card.title},
                    }
                )
            body = {
                "name": dashboard.name,
                "description": f"{dashboard.description} Owner: {dashboard.owner}.".strip(),
                "collection_id": collection,
            }
            if dashboard.name in boards:
                dashboard_id = boards[dashboard.name]
                api.call("PUT", f"/api/dashboard/{dashboard_id}", body)
            else:
                dashboard_id = int(api.call("POST", "/api/dashboard", body)["id"])
            api.call("PUT", f"/api/dashboard/{dashboard_id}", {"dashcards": placed})
            summary.dashboards += 1
            summary.cards += len(placed)
        for name, card_id in cards.items():
            if name not in keep_cards:
                api.call("PUT", f"/api/card/{card_id}", {"archived": True})
                summary.archived += 1
        for name, dashboard_id in boards.items():
            if name not in {d.name for d in mine}:
                api.call("PUT", f"/api/dashboard/{dashboard_id}", {"archived": True})
                summary.archived += 1
    return summary


@dataclass
class CheckResult:
    dashboards: int = 0
    cards: int = 0
    rows: int = 0
    failures: list[str] = field(default_factory=list)


def check(settings: Settings) -> CheckResult:
    """Run every card of every exposure's dashboard through Metabase."""
    dashboards = wanted_dashboards(settings)
    teams = Registry.from_config_dir(settings.root / "config").teams
    api = login(settings)
    root = _collection(api, ROOT_COLLECTION, None)
    result = CheckResult()
    for dashboard in dashboards:
        collection = _collection(api, teams[dashboard.team].name, root)
        boards = _items(api, collection, "dashboard")
        if dashboard.name not in boards:
            result.failures.append(f"{dashboard.name}: no such dashboard; run provision")
            continue
        found = api.call("GET", f"/api/dashboard/{boards[dashboard.name]}")
        cards = {c["card"]["name"]: c["card_id"] for c in found.get("dashcards", [])}
        expected = [_card_name(dashboard, card) for card in dashboard.cards]
        displays = {_card_name(dashboard, card): card.display for card in dashboard.cards}
        if sorted(cards) != sorted(expected):
            result.failures.append(f"{dashboard.name}: cards differ from the exposure")
        result.dashboards += 1
        for name in expected:
            if name not in cards:
                continue
            outcome = api.call("POST", f"/api/card/{cards[name]}/query", {})
            result.cards += 1
            rows = int(outcome.get("row_count") or 0)
            if outcome.get("status") != "completed" or outcome.get("error"):
                result.failures.append(f"{name}: {outcome.get('error') or outcome.get('status')}")
            elif displays[name] != "table" and rows >= bi.CHART_ROWS:
                result.failures.append(f"{name}: {rows} rows, so Metabase cuts the chart off")
            else:
                result.rows += rows
    return result
