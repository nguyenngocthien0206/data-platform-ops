"""Load every dashboard page in headless Chromium and fail on anything broken.

Run by ``make docker-browser-check`` in the official Playwright image, against
the dashboards served from the toolkit container. AppTest proves each page runs
in Python; this proves each page renders in a real browser: no exception boxes,
no error alerts, no browser console errors, and at least one chart and one data
table per page. Every page is loaded in the light and the dark theme, and a
full-page screenshot of each lands in ``reports/screenshots/`` for a human look.

Uses nothing but Playwright and the standard library, so it needs no install in
the Playwright image.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Page, Response, sync_playwright

# (screenshot name, URL path); the paths are the ones st.navigation derives in
# dashboards/app.py, with the default page at the root.
PAGES = [
    ("overview", "/"),
    ("cost", "/cost"),
    ("incidents", "/incidents"),
    ("reconciliation", "/reconciliation"),
]
THEMES = ("light", "dark")
VIEWPORT = {"width": 1440, "height": 900}
LOAD_TIMEOUT_MS = 60_000


@dataclass
class PageResult:
    name: str
    theme: str
    charts: int = 0
    tables: int = 0
    notices: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _wait_until_rendered(page: Page) -> None:
    """Wait until Streamlit has run the script to the end and Vega has drawn."""
    # Streamlit stamps its script state on the app root; a page is done once it
    # has connected and the script has finished at least one run.
    page.wait_for_selector(
        '[data-testid="stApp"][data-test-connection-state="CONNECTED"]'
        '[data-test-script-state="notRunning"]',
        timeout=LOAD_TIMEOUT_MS,
    )
    page.wait_for_selector('[data-testid="stMainBlockContainer"] h1', timeout=LOAD_TIMEOUT_MS)
    # Vega draws each chart into a canvas or an svg after its data arrives.
    page.wait_for_function(
        """() => [...document.querySelectorAll('[data-testid="stVegaLiteChart"]')]
            .every(chart => chart.querySelector('canvas, svg'))""",
        timeout=LOAD_TIMEOUT_MS,
    )
    page.wait_for_load_state("networkidle", timeout=LOAD_TIMEOUT_MS)


def _check(page: Page, base_url: str, name: str, path: str, theme: str, out: Path) -> PageResult:
    result = PageResult(name, theme)
    console: list[str] = []

    def on_console(message: ConsoleMessage) -> None:
        # A failed request is reported below with its URL, which the console omits.
        if message.type == "error" and not message.text.startswith("Failed to load resource"):
            console.append(message.text)

    def on_response(response: Response) -> None:
        # On a deep link such as /cost, Streamlit's frontend looks for the server
        # under the page path first (/cost/_stcore/health and host-config), gets
        # a 404, and falls back to the root. Those two probes are expected.
        probe = response.url.endswith(("/_stcore/health", "/_stcore/host-config"))
        if response.status >= 400 and not (probe and path != "/"):
            console.append(f"HTTP {response.status} {response.url}")

    page.on("console", on_console)
    page.on("response", on_response)
    page.on("pageerror", lambda error: console.append(f"page error: {error}"))
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}{path}", timeout=LOAD_TIMEOUT_MS)
    _wait_until_rendered(page)

    for exception in page.locator('[data-testid="stException"]').all():
        result.problems.append(f"exception: {exception.inner_text()[:300]}")
    for alert in page.locator('[data-testid="stAlert"]').all():
        text = alert.inner_text().strip()
        kind = alert.locator('[data-testid^="stAlertContent"]').first.get_attribute("data-testid")
        if kind == "stAlertContentError":
            result.problems.append(f"error alert: {text[:300]}")
        else:
            result.notices.append(text[:300])
    result.charts = page.locator('[data-testid="stVegaLiteChart"]').count()
    result.tables = page.locator('[data-testid="stDataFrame"]').count()
    if result.charts == 0:
        result.problems.append("no chart rendered")
    if result.tables == 0:
        result.problems.append("no data table rendered")
    result.problems.extend(f"console: {text[:300]}" for text in console)

    # Streamlit scrolls an inner container, not the document, so a full-page
    # screenshot stops at the viewport. Grow the viewport to the content instead.
    height = page.evaluate("document.querySelector('[data-testid=\"stMain\"]').scrollHeight")
    page.set_viewport_size({"width": VIEWPORT["width"], "height": int(height)})
    page.wait_for_timeout(1_000)
    page.screenshot(path=str(out / f"{name}-{theme}.png"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8501", help="where the dashboards run")
    parser.add_argument("--out", type=Path, default=Path("reports/screenshots"))
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    args.out.mkdir(parents=True, exist_ok=True)

    results: list[PageResult] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for theme in THEMES:
            for name, path in PAGES:
                context = browser.new_context(viewport=VIEWPORT, color_scheme=theme)
                page = context.new_page()
                try:
                    results.append(_check(page, base_url, name, path, theme, args.out))
                except Exception as error:  # a timeout is a failed page, not a crash
                    results.append(PageResult(name, theme, problems=[f"did not load: {error}"]))
                finally:
                    context.close()
        browser.close()

    failed = [r for r in results if r.problems]
    for r in results:
        verdict = "FAIL" if r.problems else "ok"
        print(f"{verdict:4} {r.name:15} {r.theme:5} charts {r.charts:2}  tables {r.tables:2}")
        for notice in r.notices:
            print(f"       notice: {notice}")
        for problem in r.problems:
            print(f"       {problem}")
    passed = len(results) - len(failed)
    print(f"{passed} of {len(results)} page loads passed; screenshots in {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
