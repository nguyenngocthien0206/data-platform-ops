"""Screenshots of Airflow and Dagster running the company's project, for the docs.

Run by ``make docker-docs-images`` in the Playwright image, after
``make docker-test-orchestrators`` has left each stack with a faulted run and
the run after the fix. It saves, in ``reports/screenshots/orchestrators/``:

- ``airflow-grid.png``: the ``company_dbt`` DAG's grid with its task groups
  expanded, the faulted run red and the next one green, every Cosmos task a cell;
- ``dagster-runs.png``: the job's runs, the failure and the success;
- ``dagster-failed-run.png``: the faulted run, the build and its failed check.

Airflow is opened with a JWT from its ``/auth/token`` endpoint, set as the
``_token`` cookie its UI reads, so no login form is scripted; the password is
``AIRFLOW_ADMIN_PASSWORD`` from ``.env``. Dagster's open source UI has no login.
Playwright and the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

VIEWPORT = {"width": 1440, "height": 900}
SETTLE_MS = 4000


def airflow_token(url: str, password: str) -> str:
    request = urllib.request.Request(
        f"{url}/auth/token",
        data=json.dumps({"username": "admin", "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return str(json.loads(response.read())["access_token"])


def dagster_failed_run(url: str) -> str:
    """The id of the job's latest failed run, from Dagster's GraphQL API."""
    query = (
        "{ runsOrError(filter: {statuses: [FAILURE]}, limit: 1) "
        "{ ... on Runs { results { runId } } } }"
    )
    request = urllib.request.Request(
        f"{url}/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        runs = json.loads(response.read())["data"]["runsOrError"]["results"]
    if not runs:
        raise SystemExit("Dagster has no failed run: run make docker-test-orchestrators first")
    return str(runs[0]["runId"])


def shoot(page: Page, url: str, out: Path, click: str = "") -> None:
    """Open ``url``, press the button labelled ``click`` if given, and save the viewport."""
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(SETTLE_MS)
    if click:
        page.get_by_role("button", name=click).click()
        page.wait_for_timeout(SETTLE_MS)
    page.screenshot(path=str(out))
    print(f"saved {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--airflow-url", default="http://airflow:8080")
    parser.add_argument("--dagster-url", default="http://dagster:3000")
    parser.add_argument("--out", type=Path, default=Path("reports/screenshots/orchestrators"))
    args = parser.parse_args()
    password = os.environ.get("AIRFLOW_ADMIN_PASSWORD", "")
    if not password:
        print("error: AIRFLOW_ADMIN_PASSWORD is not set", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    token = airflow_token(args.airflow_url, password)
    host = urllib.parse.urlparse(args.airflow_url).hostname or "airflow"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT, color_scheme="light")
        context.add_cookies([{"name": "_token", "value": token, "domain": host, "path": "/"}])
        page = context.new_page()
        shoot(
            page,
            f"{args.airflow_url}/dags/company_dbt",
            args.out / "airflow-grid.png",
            click="Expand all task groups",
        )
        shoot(page, f"{args.dagster_url}/runs", args.out / "dagster-runs.png")
        failed = dagster_failed_run(args.dagster_url)
        shoot(page, f"{args.dagster_url}/runs/{failed}", args.out / "dagster-failed-run.png")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
