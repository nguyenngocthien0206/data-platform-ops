"""Shared foundations: config loading, DuckDB access, simulated time, logging."""

from platform_ops.common.clock import Clock, SimulatedClock
from platform_ops.common.config import Settings, load_settings
from platform_ops.common.db import OPS_SCHEMA, RAW_SCHEMA, connect, open_connection
from platform_ops.common.logging import configure_logging, get_logger, log_event

__all__ = [
    "OPS_SCHEMA",
    "RAW_SCHEMA",
    "Clock",
    "Settings",
    "SimulatedClock",
    "configure_logging",
    "connect",
    "get_logger",
    "load_settings",
    "log_event",
    "open_connection",
]
