"""Shared foundations: config loading, DuckDB access, simulated time, logging."""

from platform_ops.common.clock import Clock, SimulatedClock, SystemClock
from platform_ops.common.config import Settings, get_settings, load_settings
from platform_ops.common.db import OPS_SCHEMA, RAW_SCHEMA, connect, open_connection
from platform_ops.common.logging import configure_logging, get_logger, log_event

__all__ = [
    "OPS_SCHEMA",
    "RAW_SCHEMA",
    "Clock",
    "Settings",
    "SimulatedClock",
    "SystemClock",
    "configure_logging",
    "connect",
    "get_logger",
    "get_settings",
    "load_settings",
    "log_event",
    "open_connection",
]
