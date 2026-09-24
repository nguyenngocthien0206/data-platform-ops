"""Why a cell differs, from its two canonical values.

The classes are ordered from most to least forgiving, and the first that fits
wins. A reviewer signing off a migration treats them very differently:
rounding within tolerance may be acceptable, a whole-hour shift is almost
always a time zone bug in the job, whitespace and case differences depend on
what the source system itself considered equal, and a value mismatch is data
that changed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from platform_ops.common.config import DiscrepancyClass
from platform_ops.reconcile.canonical import NULL, TIMESTAMP_FORMAT
from platform_ops.reconcile.schema import Column


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _timestamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        return None


def classify(column: Column, source: str, target: str, tolerance: Decimal) -> DiscrepancyClass:
    """The class of a difference between two canonical values that are not equal."""
    if source == NULL or target == NULL:
        return "value_mismatch"
    if column.kind == "decimal":
        a, b = _decimal(source), _decimal(target)
        if a is not None and b is not None and abs(a - b) <= tolerance:
            return "rounding"
        return "value_mismatch"
    if column.kind in ("local_ts", "utc_ts"):
        a_time, b_time = _timestamp(source), _timestamp(target)
        if a_time is not None and b_time is not None:
            seconds = (b_time - a_time).total_seconds()
            if seconds and seconds % 3600 == 0:
                return "timezone_shift"
        return "value_mismatch"
    if column.kind == "text":
        if source.strip(" ") == target.strip(" "):
            return "whitespace"
        if source.lower() == target.lower():
            return "case_only"
    return "value_mismatch"
