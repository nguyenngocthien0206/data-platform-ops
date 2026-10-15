"""End to end through a real orchestrator: one fault, one incident, one page, resolved.

Run by ``make docker-test-orchestrators`` in the toolkit container, next to the
orchestrator's stack and the chat double (Phase 14b, ADR 0020). On the
orchestrator's own company (namespace ``af_`` for Airflow, ``dg_`` for
Dagster), from scratch:

1. seed the company, empty its incident store and the chat double;
2. plant F1, NULLs in ``raw.orders.customer_id`` (``simulation.faults``);
3. start the job through the orchestrator's API (Airflow's REST API, Dagster's
   GraphQL) and wait for it: it fails, as it should, on ``stg_orders``'s
   not_null test;
4. check that the run's invocations (one per Cosmos task in Airflow; freshness
   and the build in Dagster) became one incident at ``stg_orders``, opened by
   that run, with one page in the double;
5. resync ``raw.orders`` from Postgres, the team's fix, run again, and check
   the run succeeds, the incident is resolved by it, and the double got one
   resolution and nothing else.

The cycle is the same for both; each orchestrator brings its client. It exits
non-zero on the first check that fails, saying which. Standard library and the
toolkit only::

    python scripts/check_orchestrators.py airflow
    python scripts/check_orchestrators.py dagster
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg import sql

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.servers import clickhouse_client
from platform_ops.common.store import open_store
from platform_ops.incidents import store
from platform_ops.simulation import faults
from platform_ops.simulation.ingest import Ingestion, company_clock

JOB = "company_dbt"
ROOT = "model.company.stg_orders"
F1 = faults.CATALOGUE[0]
POLL_SECONDS = 10


class CheckFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)
    print(f"  ok  {message}")


def _request(method: str, url: str, body: Any = None, token: str | None = None) -> Any:
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    return json.loads(text) if text else None


class Orchestrator(Protocol):
    name: str
    namespace: str

    def run_id(self, run: str) -> str:
        """The incident desk's run id for one of the orchestrator's runs."""
        ...

    def switch(self, on: bool) -> None:
        """Let the job run (Airflow's pause); a no-op where manual runs always run."""
        ...

    def run(self, timeout: float) -> tuple[str, bool, float]:
        """Start the job, wait for it; (its run id, whether it succeeded, seconds)."""
        ...


@dataclass
class Airflow:
    """The few calls of Airflow 3's REST API (v2) the check needs, with a JWT."""

    url: str
    password: str
    user: str = "admin"
    name: str = "airflow"
    namespace: str = "af_"

    def _token(self) -> str:
        body = {"username": self.user, "password": self.password}
        return str(_request("POST", f"{self.url}/auth/token", body)["access_token"])

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return _request(method, f"{self.url}/api/v2{path}", body, self._token())

    def run_id(self, run: str) -> str:
        return f"airflow:{JOB}:{run}"

    def switch(self, on: bool) -> None:
        self.call("PATCH", f"/dags/{JOB}", {"is_paused": not on})

    def run(self, timeout: float) -> tuple[str, bool, float]:
        started = time.monotonic()
        run = self.call("POST", f"/dags/{JOB}/dagRuns", {"logical_date": None})
        run_id = str(run["dag_run_id"])
        while time.monotonic() - started < timeout:
            state = str(self.call("GET", f"/dags/{JOB}/dagRuns/{run_id}")["state"])
            if state in ("success", "failed"):
                return run_id, state == "success", time.monotonic() - started
            time.sleep(POLL_SECONDS)
        raise CheckFailed(f"{run_id} did not finish within {timeout:.0f} s")


LAUNCH = """
mutation($params: ExecutionParams!) {
  launchRun(executionParams: $params) {
    __typename
    ... on LaunchRunSuccess { run { runId } }
    ... on Error { message }
  }
}"""
STATUS = """
query($runId: ID!) {
  runOrError(runId: $runId) { __typename ... on Run { status } ... on Error { message } }
}"""


@dataclass
class Dagster:
    """The two calls of Dagster's GraphQL API the check needs (no login in open source)."""

    url: str
    name: str = "dagster"
    namespace: str = "dg_"
    location: str = "company"

    def call(self, query: str, **variables: Any) -> Any:
        body = _request("POST", f"{self.url}/graphql", {"query": query, "variables": variables})
        if body.get("errors"):
            raise CheckFailed(f"Dagster's GraphQL said {body['errors']}")
        return body["data"]

    def run_id(self, run: str) -> str:
        return f"dagster:{run}"

    def switch(self, on: bool) -> None:
        """Manual runs never wait for the schedule, which stays stopped."""

    def run(self, timeout: float) -> tuple[str, bool, float]:
        started = time.monotonic()
        selector = {
            "repositoryLocationName": self.location,
            "repositoryName": "__repository__",
            "jobName": JOB,
        }
        launch = self.call(LAUNCH, params={"selector": selector, "runConfigData": {}})["launchRun"]
        if launch["__typename"] != "LaunchRunSuccess":
            raise CheckFailed(f"Dagster did not launch {JOB}: {launch}")
        run_id = str(launch["run"]["runId"])
        while time.monotonic() - started < timeout:
            status = str(self.call(STATUS, runId=run_id)["runOrError"]["status"])
            if status in ("SUCCESS", "FAILURE", "CANCELED"):
                return run_id, status == "SUCCESS", time.monotonic() - started
            time.sleep(POLL_SECONDS)
        raise CheckFailed(f"{run_id} did not finish within {timeout:.0f} s")


@dataclass
class Chat:
    url: str

    def messages(self) -> list[dict[str, Any]]:
        return list(_request("GET", f"{self.url}/messages"))

    def clear(self) -> None:
        _request("DELETE", f"{self.url}/messages")


def _rows(settings: Settings, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with open_store(settings) as connection:
        statement = sql.SQL(query).format(
            incidents=store.table(settings, "incidents"),
            invocations=store.table(settings, "incident_invocations"),
            events=store.table(settings, "incident_check_events"),
        )
        return list(connection.execute(statement, params).fetchall())


def _incidents(settings: Settings) -> list[tuple[Any, ...]]:
    return _rows(
        settings,
        "SELECT incident_id, root, opened_by_run, resolved_by_run FROM {incidents} "
        "WHERE merged_into IS NULL ORDER BY seq",
    )


def start_company(settings: Settings, chat: Chat) -> datetime:
    """A fresh company with F1 planted; returns its clock."""
    print(f"seeding the {settings.company.namespace} company at scale {settings.scale_factor}")
    subprocess.run(["platform-ops", "seed"], check=True)
    store.reset(settings)
    faults.reset_ground_truth(settings)
    chat.clear()
    clock = company_clock(settings)
    check(clock is not None, f"the company's clock reads {clock}")
    assert clock is not None
    client = clickhouse_client(settings)
    try:
        rows = faults.plant(settings, client, F1, clock)
    finally:
        client.close()
    check(rows > 0, f"{F1.fault_id} planted: {rows} NULLs in raw.orders.customer_id")
    return clock


def check_cycle(settings: Settings, orchestrator: Orchestrator, chat: Chat, timeout: float) -> None:
    clock = start_company(settings, chat)
    orchestrator.switch(True)
    try:
        run_id, succeeded, seconds = orchestrator.run(timeout)
        run = orchestrator.run_id(run_id)
        print(f"{run}: {'succeeded' if succeeded else 'failed'} in {seconds:.0f} s")
        check(not succeeded, "the faulted run fails, on the not_null test")
        live = _incidents(settings)
        check(len(live) == 1, f"one incident ({len(live)})")
        incident_id, root, opened_by, _ = live[0]
        check(root == ROOT, f"{incident_id} is at {ROOT}")
        check(opened_by == run, f"{incident_id} was opened by {run}")
        ((invocations,),) = _rows(
            settings, "SELECT count(*) FROM {invocations} WHERE run_id = %s", run
        )
        check(invocations > 1, f"the run's {invocations} invocations were grouped as one run")
        ((checks,),) = _rows(
            settings,
            "SELECT count(*) FROM {events} WHERE run_id = %s AND incident_id = %s",
            run,
            incident_id,
        )
        check(checks >= 1, f"{checks} failing check(s) attached to {incident_id}")
        messages = chat.messages()
        check(len(messages) == 1, f"one message in the chat double ({len(messages)})")
        check(
            f"{incident_id} (page)" in messages[0]["text"],
            f"it is {incident_id}'s page: {messages[0]['text'][:80]}",
        )

        print("resyncing raw.orders from Postgres (the fix)")
        ingestion = Ingestion(settings, delivered=settings.company.parallel_run_weeks > 0)
        try:
            ingestion.resync(F1.table, clock)
        finally:
            ingestion.close()
        run_id, succeeded, seconds = orchestrator.run(timeout)
        fixed = orchestrator.run_id(run_id)
        print(f"{fixed}: {'succeeded' if succeeded else 'failed'} in {seconds:.0f} s")
        check(succeeded, "the run after the fix succeeds")
        live = _incidents(settings)
        check(len(live) == 1, f"still one incident, nothing new ({len(live)})")
        check(live[0][3] == fixed, f"{incident_id} was resolved by {fixed}")
        messages = chat.messages()
        check(len(messages) == 2, f"one more message, two in all ({len(messages)})")
        check(
            f"{incident_id} (resolved)" in messages[1]["text"],
            f"it is the resolution: {messages[1]['text'][:80]}",
        )
    finally:
        orchestrator.switch(False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("orchestrator", choices=["airflow", "dagster"])
    parser.add_argument("--airflow-url", default="http://airflow:8080")
    parser.add_argument("--dagster-url", default="http://dagster:3000")
    parser.add_argument("--chat-url", default="http://chat:8099")
    parser.add_argument("--timeout", type=float, default=3600, help="Seconds per job run.")
    args = parser.parse_args()

    orchestrator: Orchestrator
    if args.orchestrator == "airflow":
        password = os.environ.get("AIRFLOW_ADMIN_PASSWORD", "")
        if not password:
            sys.exit("error: AIRFLOW_ADMIN_PASSWORD is not set; add it to .env")
        orchestrator = Airflow(args.airflow_url, password)
    else:
        orchestrator = Dagster(args.dagster_url)
    settings = load_settings()
    if settings.company.namespace != orchestrator.namespace:
        sys.exit(
            f"error: set PLATFORM_OPS_NAMESPACE={orchestrator.namespace}; "
            "this seeds its company anew"
        )
    started = time.monotonic()
    try:
        check_cycle(settings, orchestrator, Chat(args.chat_url), args.timeout)
    except CheckFailed as failure:
        sys.exit(f"FAILED: {failure}")
    print(f"{orchestrator.name}: passed in {time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
