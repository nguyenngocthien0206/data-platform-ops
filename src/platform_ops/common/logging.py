"""Logging that carries simulated time.

An event logged during a simulated run has two timestamps that both matter: when
it really happened, which tells you how long the run took, and when it happened
in the simulated world, which is what MTTD, MTTR and monthly cost rollups are
computed from. Records here carry both.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from platform_ops.common.clock import Clock

LOGGER_NAMESPACE = "platform_ops"
_SIMULATED_FIELD = "simulated_time"


class _SimulatedTimeFilter(logging.Filter):
    """Stamp every record with the current simulated time, or a dash if none."""

    def __init__(self, clock: Clock | None = None) -> None:
        super().__init__()
        self.clock = clock

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, _SIMULATED_FIELD):
            value = self.clock.now().isoformat() if self.clock is not None else "-"
            setattr(record, _SIMULATED_FIELD, value)
        return True


_FORMAT = "%(asctime)s real | %(simulated_time)s sim | %(levelname)-7s | %(name)s | %(message)s"


def configure_logging(level: int | str = logging.INFO, clock: Clock | None = None) -> None:
    """Install the project handler on the ``platform_ops`` logger.

    Idempotent: calling it again replaces the handler rather than doubling
    output, which matters because the CLI configures logging per command.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(level)
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_SimulatedTimeFilter(clock))
    logger.addHandler(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the project namespace."""
    if name is None or name == LOGGER_NAMESPACE:
        return logging.getLogger(LOGGER_NAMESPACE)
    if name.startswith(f"{LOGGER_NAMESPACE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")


def log_event(
    logger: logging.Logger,
    message: str,
    *,
    clock: Clock | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Log one event, pinning the simulated time of the clock that produced it."""
    extra: dict[str, Any] = dict(fields)
    if clock is not None:
        extra[_SIMULATED_FIELD] = clock.now().isoformat()
    logger.log(level, message, extra=extra)
