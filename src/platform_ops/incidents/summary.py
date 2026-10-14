"""``platform-ops incidents summary``: failing checks against pages, per person and week.

The number a pilot is judged on: how often each person would have been paged
if every failing check paged its dataset's owner, against how often the
incident desk actually paged them. Read straight from the store, so it works
on a real ingest history as well as on the company's.
"""

from __future__ import annotations

from collections import Counter

from platform_ops.common.config import Settings
from platform_ops.common.markdown import table
from platform_ops.incidents.desk import Context
from platform_ops.incidents.history import load_history
from platform_ops.incidents.routing import UNOWNED
from platform_ops.incidents.severity import dataset_of


def weekly(settings: Settings, context: Context) -> list[tuple[str, str, int, int]]:
    """(ISO week, person, failing checks on their datasets, pages they got)."""
    history = load_history(settings, context)
    before: Counter[tuple[str, str]] = Counter()
    for event, _ in history.events:
        rule = context.registry.resolve(dataset_of(event.subject, context.nodes)).rule
        week = event.detected_at.strftime("%G-W%V")
        before[(week, rule.owner if rule else UNOWNED)] += 1
    after: Counter[tuple[str, str]] = Counter(
        (n.sent_at.strftime("%G-W%V"), n.recipient)
        for n in history.notifications
        if n.kind == "page"
    )
    keys = sorted(set(before) | set(after))
    return [(week, person, before[(week, person)], after[(week, person)]) for week, person in keys]


def render(rows: list[tuple[str, str, int, int]]) -> str:
    if not rows:
        return "No failing checks in the incident store."
    return table(
        ["Week", "Person", "Failing checks", "Pages"],
        [(w, p, f"{b:,}", f"{a:,}") for w, p, b, a in rows],
        right=[2, 3],
    )
