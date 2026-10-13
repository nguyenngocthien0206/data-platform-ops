"""Open every Metabase dashboard in headless Chromium, fail on a broken card, save screenshots.

Run by ``make docker-metabase-check`` in the official Playwright image, after
``platform-ops metabase check`` has run every card's query through Metabase's
API. This proves the other half: each dashboard renders in a real browser, with
every card drawn and none showing Metabase's error message. A full-page
screenshot of each lands in ``reports/screenshots/metabase/`` for a human look.

Logs in through Metabase's API with ``MB_ADMIN_EMAIL`` and ``MB_ADMIN_PASSWORD``
and uses nothing but Playwright and the standard library.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

VIEWPORT = {"width": 1440, "height": 900}
LOAD_TIMEOUT_MS = 120_000
# What Metabase writes on a card whose query or visualization failed.
ERROR_TEXT = re.compile(r"There was a problem|Something.s gone wrong", re.IGNORECASE)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _check_dashboard(page: Page, url: str, out: Path) -> list[str]:
    page.goto(url, wait_until="networkidle", timeout=LOAD_TIMEOUT_MS)
    page.wait_for_selector('[data-testid="dashcard"]', timeout=LOAD_TIMEOUT_MS)
    # Every card has drawn: a chart's svg or canvas, or a table's cells.
    page.wait_for_function(
        """() => [...document.querySelectorAll('[data-testid="dashcard"]')].every(card =>
            card.querySelector('svg, canvas, [role="gridcell"], [data-testid="cell-data"]'))""",
        timeout=LOAD_TIMEOUT_MS,
    )
    problems = []
    for index, card in enumerate(page.query_selector_all('[data-testid="dashcard"]')):
        text = card.inner_text()
        if ERROR_TEXT.search(text):
            problems.append(f"card {index + 1}: {text.strip().splitlines()[0][:120]}")
    page.screenshot(path=str(out), full_page=True)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--out", default="reports/screenshots/metabase")
    args = parser.parse_args()
    email, password = os.environ.get("MB_ADMIN_EMAIL"), os.environ.get("MB_ADMIN_PASSWORD")
    if not email or not password:
        print("set MB_ADMIN_EMAIL and MB_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT)
        session = context.request.post(
            f"{args.url}/api/session", data={"username": email, "password": password}
        ).json()["id"]
        context.add_cookies(
            [
                {
                    "name": "metabase.SESSION",
                    "value": session,
                    "domain": urlparse(args.url).hostname,
                    "path": "/",
                }
            ]
        )
        found = context.request.get(f"{args.url}/api/search?models=dashboard").json()
        # Metabase ships an "Examples" collection of its own; only the company's count.
        ours = [i for i in found["data"] if i.get("collection", {}).get("name") != "Examples"]
        boards = sorted(ours, key=lambda item: item["name"])
        page = context.new_page()
        for board in boards:
            target = out / f"{_slug(board['name'])}.png"
            problems = _check_dashboard(page, f"{args.url}/dashboard/{board['id']}", target)
            status = "ok" if not problems else "FAILED"
            print(f"{status:6} {board['name']} -> {target}")
            failures += [f"{board['name']}: {p}" for p in problems]
        browser.close()
    if not boards:
        print("no dashboards found; run `platform-ops metabase provision`", file=sys.stderr)
        return 1
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
