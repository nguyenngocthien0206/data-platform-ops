"""The data-platform-ops dashboards: one page per module, read-only over ``ops``.

Run with ``make dashboard`` (or ``platform-ops dashboard``). Every number here
was computed and stored by the modules; the pages only read it.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

PAGES = Path(__file__).parent / "pages"

st.set_page_config(page_title="data-platform-ops", page_icon=":material/monitoring:", layout="wide")

navigation = st.navigation(
    [
        st.Page(PAGES / "overview.py", title="Overview", icon=":material/dashboard:", default=True),
        st.Page(PAGES / "cost.py", title="Cost", icon=":material/payments:"),
        st.Page(PAGES / "incidents.py", title="Incidents", icon=":material/notifications_active:"),
        st.Page(PAGES / "reconciliation.py", title="Reconciliation", icon=":material/rule:"),
    ]
)
navigation.run()
