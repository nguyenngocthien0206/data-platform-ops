"""Draw the architecture diagram, ``docs/images/architecture.svg``, from the project itself.

The diagram is code, so it is reviewed like code and cannot drift from the
project: its numbers (models, exposures, tables, reports, teams, the length of
the parallel run, the clean nights a cutover needs) are read from the dbt
project and the config, and ``tests/test_docs_images.py`` fails when the
committed file differs from what this script writes. After changing either,
run it again::

    uv run python scripts/make_architecture.py

The logos are the vendors' own files in ``docs/images/logos/`` (see its
``LICENSES.md``), embedded as data URIs so the SVG stands alone: GitHub shows a
README image without letting it load anything else.

Standard library and PyYAML only. Text widths are estimated, not measured
(an SVG shown as an image cannot load a font), so every piece of text is
wrapped to a width and a line limit, and a text that does not fit fails the
script instead of overflowing its box.
"""

from __future__ import annotations

import argparse
import base64
import sys
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "docs" / "images" / "logos"
OUTPUT = ROOT / "docs" / "images" / "architecture.svg"

WIDTH, HEIGHT = 1600, 1090
FONT = "'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
# Average glyph width as a share of the font size, on the safe side.
GLYPH = 0.56

INK = "#1A2233"
MUTED = "#5B6475"
LINE = "#C9D3E3"
CARD = "#FFFFFF"
COMPANY_FILL = "#F5F8FD"
BLUE = "#2F6BD8"
ORANGE = "#E8762D"
TOOLKIT_FILL = "#FFF7F0"
GREY = "#98A1AE"

CARD_W, CARD_H = 220, 145
COLUMNS = (60, 310, 560, 810, 1060, 1310)


# -- the project's numbers ---------------------------------------------------------------


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def facts(root: Path = ROOT) -> dict[str, int]:
    """What the diagram states, read from the dbt project and the config."""
    models_dir = root / "dbt" / "models"
    ymls = [_yaml(p) for p in sorted(models_dir.rglob("*.yml"))]
    reconcile = _yaml(root / "config" / "reconcile.yaml")
    settings = _yaml(root / "config" / "settings.yaml")
    return {
        "models": len(list(models_dir.rglob("*.sql"))),
        "exposures": sum(len(y.get("exposures") or []) for y in ymls),
        "tables": len(reconcile["tables"]),
        "reports": len(reconcile["reports"]),
        "teams": len(_yaml(root / "config" / "teams.yaml")["teams"]),
        "overlap": int(settings["company"]["ingest_overlap_minutes"]),
        "parallel_weeks": int(settings["company"]["parallel_run_weeks"]),
        "clean_nights": int(settings["reconcile"]["clean_nights_for_cutover"]),
    }


# -- drawing -------------------------------------------------------------------------------


def wrap(text: str, width: float, size: float, max_lines: int) -> list[str]:
    """Words into lines no wider than ``width`` at ``size``; fails if more than ``max_lines``."""
    per_line = int(width / (size * GLYPH))
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= per_line:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > max_lines:
        raise ValueError(f"does not fit in {max_lines} lines of {per_line} characters: {text!r}")
    return lines


def _data_uri(name: str) -> str:
    path = LOGOS / name
    kind = "image/svg+xml" if path.suffix == ".svg" else "image/png"
    return f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


@dataclass
class Drawing:
    parts: list[str] = field(default_factory=list)
    logos: set[str] = field(default_factory=set)

    def add(self, element: str) -> None:
        self.parts.append(element)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill: str = CARD,
        stroke: str = LINE,
        dashed: bool = False,
        radius: int = 10,
        width: float = 1.5,
    ) -> None:
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{width}"{dash}/>'
        )

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = 12,
        weight: int = 400,
        color: str = MUTED,
        anchor: str = "start",
    ) -> None:
        self.add(
            f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}">{escape(content)}</text>'
        )

    def lines(
        self,
        x: float,
        y: float,
        content: str,
        *,
        width: float,
        max_lines: int,
        size: float = 12,
        leading: float = 16,
        **style: object,
    ) -> float:
        """Wrapped text from ``y`` down; returns the baseline after the last line."""
        for line in wrap(content, width, size, max_lines):
            self.text(x, y, line, size=size, **style)  # type: ignore[arg-type]
            y += leading
        return y

    def logo(self, name: str, x: float, y: float, size: float) -> None:
        self.logos.add(name)
        self.add(
            f'<image x="{x}" y="{y}" width="{size}" height="{size}" '
            f'preserveAspectRatio="xMidYMid meet" href="{_data_uri(name)}"/>'
        )

    def arrow(self, *points: tuple[float, float], label: str = "", at: int = 0) -> None:
        """A line through ``points`` ending in an arrowhead; ``label`` sits on segment ``at``."""
        path = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y) in enumerate(points))
        self.add(
            f'<path d="{path}" fill="none" stroke="{BLUE}" stroke-width="2" '
            f'stroke-linejoin="round" marker-end="url(#arrow)"/>'
        )
        if label:
            (x1, y1), (x2, y2) = points[at], points[at + 1]
            self.tag((x1 + x2) / 2, (y1 + y2) / 2, label)

    def tag(self, cx: float, cy: float, label: str) -> None:
        w = len(label) * 11 * GLYPH + 14
        self.rect(cx - w / 2, cy - 10, w, 20, fill=CARD, stroke=LINE, radius=4, width=1)
        self.text(cx, cy + 4, label, size=11, color=MUTED, anchor="middle")

    def render(self) -> str:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="{escape(FONT)}" role="img" '
            f'aria-label="data-platform-ops architecture">'
        )
        defs = (
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
            f'fill="{BLUE}"/></marker></defs>'
        )
        body = "\n".join(self.parts)
        return f"{head}\n{defs}\n{body}\n</svg>\n"


# -- icons for the parts that are not a vendor's product ---------------------------------


def icon(d: Drawing, kind: str, x: float, y: float, color: str) -> None:
    """A simple 40 px pictogram: the toolkit's modules and the company's own jobs."""
    d.rect(x, y, 40, 40, fill=color, stroke=color, radius=9, width=1)
    white = 'fill="none" stroke="#FFFFFF" stroke-width="2.4" stroke-linecap="round"'
    cx, cy = x + 20, y + 20
    if kind == "copy":
        d.add(
            f'<path d="M{x + 9},{cy - 5} H{x + 30} M{x + 25},{cy - 10} L{x + 30},{cy - 5} '
            f'L{x + 25},{cy}" {white} stroke-linejoin="round"/>'
        )
        d.add(
            f'<path d="M{x + 31},{cy + 6} H{x + 10} M{x + 15},{cy + 1} L{x + 10},{cy + 6} '
            f'L{x + 15},{cy + 11}" {white} stroke-linejoin="round"/>'
        )
    elif kind == "teams":
        for dx in (-9, 9):
            d.add(f'<circle cx="{cx + dx}" cy="{cy - 5}" r="4.5" {white}/>')
            d.add(f'<path d="M{cx + dx - 7},{cy + 11} a7,7 0 0 1 14,0" {white}/>')
    elif kind == "reconcile":
        d.add(f'<rect x="{x + 8}" y="{y + 8}" width="15" height="19" rx="2" {white}/>')
        d.add(
            f'<rect x="{x + 17}" y="{y + 13}" width="15" height="19" rx="2" '
            f'fill="{color}" stroke="#FFFFFF" stroke-width="2.4"/>'
        )
        d.add(f'<path d="M{x + 20},{y + 23} l3,3 l6,-7" {white} stroke-linejoin="round"/>')
    elif kind == "incidents":
        d.add(
            f'<path d="M{cx},{y + 8} L{x + 33},{y + 31} H{x + 7} Z" {white} '
            f'stroke-linejoin="round"/>'
        )
        d.add(f'<path d="M{cx},{y + 17} V{y + 24}" {white}/>')
        d.add(f'<circle cx="{cx}" cy="{y + 28}" r="1.4" fill="#FFFFFF"/>')
    elif kind == "metadata":
        nodes = ((x + 10, y + 12), (x + 30, y + 12), (x + 20, y + 29))
        for (ax, ay), (bx, by) in zip(nodes, nodes[1:] + nodes[:1], strict=True):
            d.add(f'<path d="M{ax},{ay} L{bx},{by}" {white}/>')
        for nx, ny in nodes:
            d.add(
                f'<circle cx="{nx}" cy="{ny}" r="4" fill="{color}" stroke="#FFFFFF" '
                f'stroke-width="2.4"/>'
            )


# -- the diagram ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    column: int
    title: str
    text: str
    logos: tuple[str, ...] = ()
    pictogram: str = ""
    step: str = ""


def card(d: Drawing, c: Card, y: float, color: str, x: float | None = None) -> None:
    x = COLUMNS[c.column] if x is None else x
    d.rect(x, y, CARD_W, CARD_H, fill=CARD, stroke=LINE)
    if c.step:
        d.text(x + 14, y + 24, c.step, size=13, weight=700, color=GREY)
    if c.logos:
        size = 40 if len(c.logos) == 1 else 34
        gap = 10
        total = len(c.logos) * size + (len(c.logos) - 1) * gap
        lx = x + (CARD_W - total) / 2
        for name in c.logos:
            d.logo(name, lx, y + 14 + (40 - size) / 2, size)
            lx += size + gap
    else:
        icon(d, c.pictogram, x + (CARD_W - 40) / 2, y + 14, color)
    d.text(x + CARD_W / 2, y + 76, c.title, size=15, weight=700, color=INK, anchor="middle")
    d.lines(x + CARD_W / 2, y + 96, c.text, width=CARD_W - 24, max_lines=3, anchor="middle")


def panel(
    d: Drawing,
    x: float,
    title: str,
    items: list[tuple[str, str]],
    color: str,
    highlight: bool = False,
) -> None:
    y, w, h = 815, 493, 215
    d.rect(
        x, y, w, h, fill=TOOLKIT_FILL if highlight else CARD, stroke=color if highlight else LINE
    )
    d.text(x + 20, y + 30, title, size=13, weight=700, color=color)
    top = y + 58
    for lead, text in items:
        d.text(x + 20, top, lead, size=13, weight=700, color=INK)
        d.lines(x + 20, top + 18, text, width=w - 40, max_lines=2)
        top += 54


def draw(f: dict[str, int]) -> Drawing:
    d = Drawing()
    d.rect(0, 0, WIDTH, HEIGHT, fill="#FFFFFF", stroke="#FFFFFF", radius=0)

    # Title, subtitle and legend.
    d.rect(40, 32, 6, 58, fill=ORANGE, stroke=ORANGE, radius=2)
    d.text(
        60,
        60,
        "data-platform-ops: a reporting migration from Postgres to ClickHouse, run like production",
        size=27,
        weight=700,
        color=INK,
    )
    d.text(
        60,
        86,
        f"A simulated company of {f['teams']} teams, its legacy reports and its new warehouse, "
        "with the platform team's toolkit on top: the migration sign-off, the bill per team, "
        "one incident per problem",
        size=14,
    )
    legend = (
        ("The company's systems", COMPANY_FILL, BLUE, False),
        ("The toolkit, platform-ops", TOOLKIT_FILL, ORANGE, True),
        ("Outside services and schedulers", CARD, GREY, True),
    )
    lx = 40
    for label, fill, stroke, dashed in legend:
        d.rect(lx, 112, 18, 18, fill=fill, stroke=stroke, dashed=dashed, radius=4)
        d.text(lx + 26, 126, label, size=13, color=INK)
        lx += 26 + len(label) * 13 * GLYPH + 36

    # Lane 1: the company.
    d.rect(40, 150, 1520, 295, fill=COMPANY_FILL, stroke=LINE)
    d.text(
        58,
        174,
        "THE COMPANY: REPORTING ON POSTGRES, MIGRATING TO CLICKHOUSE",
        size=14,
        weight=700,
        color=BLUE,
    )
    d.text(58, 193, "Simulated and seeded: every number it produces repeats run after run", size=12)
    company = (
        Card(
            0,
            "Legacy Postgres",
            f"{f['tables']} app tables and {f['reports']} hand-written reports: today's reporting",
            ("postgresql.svg",),
            step="1",
        ),
        Card(
            1,
            "Incremental copy",
            f"Rows past an updated_at watermark, re-reading {f['overlap']} minutes",
            pictogram="copy",
            step="2",
        ),
        Card(
            2,
            "ClickHouse",
            "raw as ReplacingMergeTree, one row per key: the new warehouse",
            ("clickhouse.svg",),
            step="3",
        ),
        Card(
            3,
            "dbt",
            f"{f['models']} models, a seed and a snapshot, built every night",
            ("dbt.png",),
            step="4",
        ),
        Card(
            4,
            "Metabase",
            f"{f['exposures']} dashboards generated from the dbt exposures",
            ("metabase.svg",),
            step="5",
        ),
        Card(
            5,
            "Business teams",
            f"{f['teams']} teams, a ClickHouse user each: nobody queries as admin",
            pictogram="teams",
            step="6",
        ),
    )
    for c in company:
        card(d, c, 205, BLUE)
    mid = 205 + CARD_H / 2
    for left in range(5):
        d.arrow((COLUMNS[left] + CARD_W, mid), (COLUMNS[left + 1] - 4, mid))

    # The schedulers, under dbt.
    sx, sy = 880, 372
    d.rect(sx, sy, 300, 56, fill=CARD, stroke=GREY, dashed=True)
    d.text(sx + 14, sy + 33, "Run nightly by", size=12, color=MUTED)
    d.logo("airflow.png", sx + 108, sy + 14, 28)
    d.text(sx + 141, sy + 33, "Airflow", size=13, weight=700, color=INK)
    d.logo("dagster.svg", sx + 202, sy + 14, 28)
    d.text(sx + 235, sy + 33, "Dagster", size=13, weight=700, color=INK)
    d.arrow((sx + 70, sy), (sx + 70, 205 + CARD_H + 4))

    # Lane 2: the toolkit.
    d.rect(40, 490, 1520, 305, fill=TOOLKIT_FILL, stroke=ORANGE, dashed=True)
    # Right-aligned: the arrows from the company come down on the left.
    d.text(
        1542,
        514,
        "THE TOOLKIT: PLATFORM-OPS, ONE COMMAND LINE IN ITS OWN CONTAINER",
        size=14,
        weight=700,
        color=ORANGE,
        anchor="end",
    )
    d.text(
        1542,
        533,
        "Reads what the company already produces; writes its results to one store",
        size=12,
        anchor="end",
    )
    modules_y = 555
    modules = (
        (
            60,
            Card(
                0,
                "reconcile",
                f"The parallel run: {f['tables']} tables and {f['reports']} reports "
                "compared every night",
                pictogram="reconcile",
            ),
        ),
        (
            435,
            Card(
                0,
                "cost",
                "The query log and parts: the bill per team, also priced on two clouds",
                ("bigquery.svg", "snowflake.svg"),
            ),
        ),
        (
            810,
            Card(
                0,
                "incidents",
                "Every dbt invocation ingested: one incident per root, paged to its owner",
                pictogram="incidents",
            ),
        ),
    )
    for x, c in modules:
        card(d, c, modules_y, ORANGE, x=x)
    # The shared metadata layer under the three modules.
    bar_y = 722
    d.rect(60, bar_y, 970, 48, fill=CARD, stroke=ORANGE)
    icon(d, "metadata", 70, bar_y + 4, ORANGE)
    d.text(122, bar_y + 21, "Shared metadata layer", size=14, weight=700, color=INK)
    d.text(
        122,
        bar_y + 38,
        "The ownership registry and the lineage from dbt's manifest: "
        "every module reads it, none calls another",
        size=12,
    )
    d.logo("github.svg", 880, bar_y + 12, 24)
    d.text(910, bar_y + 29, "a gate in CI", size=12, color=INK)
    for x, _ in modules:
        d.arrow((x + 30, bar_y), (x + 30, modules_y + CARD_H + 4))
    # Results, pages and dashboards.
    store = Card(
        4,
        "Ops store",
        "Every result, read by the dashboards as a read-only role",
        ("postgresql.svg",),
    )
    card(d, store, modules_y, ORANGE, x=1090)
    dash = Card(
        5, "Dashboards", "The run, the bill, the parallel run and the incidents", ("streamlit.svg",)
    )
    card(d, dash, modules_y, ORANGE, x=1330)
    d.arrow((1030, modules_y + 40), (1086, modules_y + 40), label="results")
    d.arrow((1310, modules_y + CARD_H / 2), (1326, modules_y + CARD_H / 2))
    d.rect(1090, 717, 460, 58, fill=CARD, stroke=GREY, dashed=True)
    d.logo("slack.svg", 1106, 731, 30)
    d.logo("telegram.svg", 1144, 731, 30)
    d.text(1186, 742, "Pages, updates and resolutions", size=13, weight=700, color=INK)
    d.text(1186, 760, "to each owning team's channel", size=12)
    d.arrow((1030, modules_y + 110), (1060, modules_y + 110), (1060, 746), (1086, 746))

    # From the company to the toolkit.
    bottom = 205 + CARD_H
    d.arrow((150, bottom), (150, modules_y - 4), label="tables, reports")
    d.arrow((585, bottom), (585, 462), (235, 462), (235, modules_y - 4), label="raw, marts", at=1)
    d.arrow((640, bottom), (640, modules_y - 4), label="query log, parts")
    d.arrow((850, bottom), (850, 472), (740, 472), (740, bar_y - 4), label="manifest", at=2)
    d.arrow((sx + 70, sy + 56), (sx + 70, modules_y - 4), label="every invocation")

    # What each part is for.
    panel(
        d,
        40,
        "01  THE MIGRATION IS SIGNED OFF ON EVIDENCE",
        [
            (
                "Both systems, every night",
                f"For {f['parallel_weeks']} weeks dbt builds nightly, and {f['tables']} tables and "
                f"{f['reports']} reports are compared with the legacy Postgres.",
            ),
            (
                "Only the segments that differ are opened",
                "A checksum per key range; only a mismatch reads rows, so a large table costs "
                "little to verify.",
            ),
            (
                f"A cutover after {f['clean_nights']} clean nights in a row",
                "A report that differs is explained through dbt's lineage by a table that differs, "
                "or it is a logic change.",
            ),
        ],
        BLUE,
    )
    panel(
        d,
        553,
        "02  THE BILL LANDS ON WHO CAUSED IT",
        [
            (
                "Every query is tagged",
                "A ClickHouse user per team and service, and a tag naming the model, the dashboard "
                "or the person.",
            ),
            (
                "The stated bill, shared by measured use",
                "The cluster's monthly cost split by bytes read and stored, "
                "adding up to it exactly.",
            ),
            (
                "Two clouds priced on the same workload",
                "BigQuery on demand and Snowflake warehouses, from the same query log.",
            ),
        ],
        BLUE,
    )
    panel(
        d,
        1066,
        "03  ONE PAGE PER PROBLEM",
        [
            (
                "One incident per root cause",
                "Checks failing below a failed node join its incident, across every invocation "
                "of a run.",
            ),
            (
                "Paged to the owner of the feed",
                "The registry says who; the dashboards downstream set the severity.",
            ),
            (
                "Resolved by the next healthy run",
                "From the nightly schedule, Airflow or Dagster: each calls the same command.",
            ),
        ],
        ORANGE,
        highlight=True,
    )

    # Footer.
    d.logo("docker.svg", 40, 1047, 26)
    d.text(74, 1065, "Runs on one machine with Docker Compose", size=12, color=INK)
    d.logo("github.svg", 350, 1048, 24)
    d.text(382, 1065, "CI and the release on GitHub Actions", size=12, color=INK)
    d.text(
        1560,
        1065,
        "Logical flow only. Logos are trademarks of their owners: docs/images/logos/LICENSES.md",
        size=11,
        color=GREY,
        anchor="end",
    )
    return d


def render(root: Path = ROOT) -> str:
    return draw(facts(root)).render()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="Exit 1 if the committed diagram is out of date."
    )
    args = parser.parse_args()
    svg = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if current != svg:
            sys.exit(f"{OUTPUT.relative_to(ROOT)} is out of date; run scripts/make_architecture.py")
        return
    OUTPUT.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(svg.encode('utf-8')):,} bytes)")


if __name__ == "__main__":
    main()
