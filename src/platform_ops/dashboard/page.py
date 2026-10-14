"""Helpers every dashboard page shares: settings, the ops store, the theme."""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from platform_ops.common.config import Settings, load_settings
from platform_ops.dashboard.charts import Palette, palette
from platform_ops.dashboard.queries import MissingData, Store
from platform_ops.metadata.registry import Registry

# The store can be watched while a run is still writing it (a parallel run's
# nights land one by one), so cached frames expire quickly.
REFRESH_SECONDS = 30


@st.cache_resource
def settings() -> Settings:
    """Settings from ``PLATFORM_OPS_CONFIG`` or ``config/settings.yaml``, read once."""
    return load_settings()


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def _frame(root: str, name: str) -> pd.DataFrame:
    del root  # part of the cache key only: one entry per configuration
    return Store(settings()).frame(name)


def frame(name: str) -> pd.DataFrame | None:
    """A named query's result, or ``None`` after telling the reader what to run."""
    try:
        return _frame(str(settings().root), name)
    except MissingData as missing:
        st.info(str(missing), icon=":material/info:")
        return None


def teams() -> list[str]:
    """Teams in ``config/teams.yaml`` order: each keeps one colour everywhere."""
    registry = Registry.from_config_dir(settings().root / "config")
    return list(registry.teams)


def colors() -> Palette:
    theme = getattr(st.context, "theme", None)
    return palette(getattr(theme, "type", None))


def chart(figure: alt.Chart | alt.LayerChart, data: pd.DataFrame, label: str = "Data") -> None:
    """A chart with its data table under it (the relief for low-contrast slots)."""
    st.altair_chart(figure, width="stretch")
    with st.expander(label):
        st.dataframe(data, hide_index=True, width="stretch")


def usd(value: float) -> str:
    """Money as the cost report writes it: cents above a dollar, four places below."""
    return f"${value:,.2f}" if abs(value) >= 1 or value == 0 else f"${value:.4f}"


def size(value: float) -> str:
    """Bytes in MB below a gigabyte and in GB above, as the cost report writes them."""
    return f"{value / 10**6:,.1f} MB" if value < 10**9 else f"{value / 10**9:,.2f} GB"


def table(
    data: pd.DataFrame,
    counts: tuple[str, ...] = (),
    dollars: tuple[str, ...] = (),
    cents: tuple[str, ...] = (),
) -> None:
    """A data table with counts grouped by thousands and money as money.

    ``dollars`` show cents; ``cents`` (amounts that are often below a dollar,
    such as a month's saving) show four places, as the cost report does.
    """
    config: dict[str, Any] = {c: st.column_config.NumberColumn(format="localized") for c in counts}
    config.update({c: st.column_config.NumberColumn(format="$%.2f") for c in dollars})
    config.update({c: st.column_config.NumberColumn(format="$%.4f") for c in cents})
    st.dataframe(data, hide_index=True, width="stretch", column_config=config)


def caption(text: str) -> None:
    """A caption whose dollar signs stay dollar signs (two of them start LaTeX)."""
    st.caption(text.replace("$", r"\$"))
