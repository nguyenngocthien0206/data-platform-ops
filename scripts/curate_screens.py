"""Pick the screenshots the docs show, from a real run, into ``docs/images/screens/``.

Run by ``make docker-docs-images`` after the checks that take the screenshots:
``docker-browser-check`` (the Streamlit dashboards), ``docker-metabase-check``
(Metabase) and ``docs-screens`` (Airflow and Dagster, after
``docker-test-orchestrators``). Each picked image is kept in the light theme,
cut to a height a README can show (the top of the page, where its point is),
and saved as an optimised PNG. A missing source fails with the target that
makes it, so the committed set always comes from one run.

Pillow comes with the toolkit's environment (Streamlit needs it).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "reports" / "screenshots"
OUT = ROOT / "docs" / "images" / "screens"
# The whole set, so the README stays light to load.
BUDGET_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True)
class Screen:
    source: str
    name: str
    made_by: str
    max_height: int = 1600


SCREENS = (
    Screen("overview-light.png", "streamlit-overview.png", "make docker-browser-check"),
    Screen(
        "reconciliation-light.png",
        "streamlit-reconciliation.png",
        "make docker-browser-check",
        2000,
    ),
    Screen("cost-light.png", "streamlit-cost.png", "make docker-browser-check", 2000),
    Screen("incidents-light.png", "streamlit-incidents.png", "make docker-browser-check"),
    Screen(
        "metabase/sales-executive-dashboard.png", "metabase-sales.png", "make docker-metabase-check"
    ),
    Screen("metabase/campaign-roi.png", "metabase-marketing.png", "make docker-metabase-check"),
    Screen("metabase/revenue-dashboard.png", "metabase-finance.png", "make docker-metabase-check"),
    Screen(
        "metabase/engagement-dashboard.png", "metabase-product.png", "make docker-metabase-check"
    ),
    Screen("orchestrators/airflow-grid.png", "airflow-grid.png", "make docker-docs-images"),
    Screen("orchestrators/dagster-runs.png", "dagster-runs.png", "make docker-docs-images"),
    Screen(
        "orchestrators/dagster-failed-run.png", "dagster-failed-run.png", "make docker-docs-images"
    ),
)


def curate(shots: Path = SHOTS, out: Path = OUT) -> int:
    """Write every screen to ``out``; returns the bytes written."""
    missing = [
        f"{s.source} (from {s.made_by})" for s in SCREENS if not (shots / s.source).is_file()
    ]
    if missing:
        raise SystemExit("missing screenshots:\n  " + "\n  ".join(missing))
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for screen in SCREENS:
        with Image.open(shots / screen.source) as image:
            width, height = image.size
            picked = image.crop((0, 0, width, min(height, screen.max_height))).convert("RGB")
            target = out / screen.name
            picked.save(target, optimize=True)
        size = target.stat().st_size
        total += size
        print(f"{screen.name}: {picked.size[0]}x{picked.size[1]}, {size // 1024} KB")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    total = curate()
    print(f"{len(SCREENS)} screens, {total / 1024 / 1024:.2f} MB")
    if total > BUDGET_BYTES:
        print(f"error: over the {BUDGET_BYTES // 1024 // 1024} MB budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
