"""Helpers every dashboard page shares: settings, the warehouse, the theme."""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from platform_ops.common.config import Settings, load_settings
from platform_ops.dashboard.charts import Palette, palette
from platform_ops.dashboard.queries import MissingData, Warehouse
from platform_ops.metadata.registry import Registry

REFRESH_SECONDS = 30


@st.cache_resource
def settings() -> Settings:
    """Settings from ``PLATFORM_OPS_CONFIG`` or ``config/settings.yaml``, read once."""
    return load_settings()


def warehouse() -> Warehouse:
    current = settings()
    return Warehouse(Path(current.resolve(current.paths.duckdb)))


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def _frame(path: str, name: str) -> pd.DataFrame:
    return Warehouse(Path(path)).frame(name)


def frame(name: str) -> pd.DataFrame | None:
    """A named query's result, or ``None`` after telling the reader what to run."""
    try:
        return _frame(str(warehouse().path), name)
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
    """A chart with its data table beside it (the relief for low-contrast slots)."""
    st.altair_chart(figure, width="stretch")
    with st.expander(label):
        st.dataframe(data, hide_index=True, width="stretch")


def status(passed: bool) -> str:
    """A verdict as icon, word and colour together, never colour alone."""
    return ":green[:material/check_circle: PASS]" if passed else ":red[:material/cancel: FAIL]"
