"""Why a cell differs, from its two canonical values.

The classes are ordered from most to least forgiving, and the first that fits
wins. A reviewer signing off a migration treats them very differently:
rounding within tolerance may be acceptable, a time zone shift is almost
always a bug in the job, whitespace and case differences depend on what the
source system itself considered equal, and a value mismatch is data that
changed.

A label names a mechanism, and is given only when that mechanism reproduces
the value. A difference that merely looks like one is a value mismatch: a cent
added on purpose is not rounding, and a two-hour shift is not a time zone the
company runs in.

- **rounding:** within the tolerance, and one side is the other rounded
  (half up, half to even) or truncated at the column's scale, directly or
  through a binary float on the way, the way ``toDecimal64(toFloat64(x), s)``
  scales the float and truncates.
- **timezone_shift:** one side is the other read as wall-clock time in one of
  ``reconcile.time_zones``, daylight saving time included.
- **whitespace** and **case_only:** equal once trailing spaces are trimmed or
  letters lower-cased.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from zoneinfo import ZoneInfo

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


def _rounded(value: Decimal, scale: int) -> set[Decimal]:
    """What rounding or truncating ``value`` at ``scale`` can leave of it."""
    step = Decimal(1).scaleb(-scale)
    forms = {value.quantize(step, rounding=r) for r in (ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN)}
    # Through a binary float: scaled in floating point (which rounds), then
    # truncated or rounded back to a decimal.
    scaled = float(value) * 10**scale
    for whole in (math.trunc(scaled), round(scaled)):
        forms.add((Decimal(whole) * step).quantize(step))
    return forms


@cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _read_as_local(value: datetime, zone: str) -> set[datetime]:
    """The UTC times ``value`` becomes when its wall clock is read as local time in ``zone``.

    Both folds, so an hour that happens twice when the clocks go back, or that
    does not exist when they go forward, is matched whichever way the job read it.
    """
    return {
        value.replace(tzinfo=_zone(zone), fold=fold).astimezone(UTC).replace(tzinfo=None)
        for fold in (0, 1)
    }


def classify(
    column: Column,
    source: str,
    target: str,
    tolerance: Decimal,
    time_zones: Sequence[str] = (),
) -> DiscrepancyClass:
    """The class of a difference between two canonical values that are not equal."""
    if source == NULL or target == NULL:
        return "value_mismatch"
    if column.kind == "decimal":
        a, b = _decimal(source), _decimal(target)
        if a is not None and b is not None and abs(a - b) <= tolerance:
            scale = column.scale
            if b in _rounded(a, scale) or a in _rounded(b, scale):
                return "rounding"
        return "value_mismatch"
    if column.kind == "ts":
        a_time, b_time = _timestamp(source), _timestamp(target)
        if a_time is not None and b_time is not None:
            for zone in time_zones:
                if b_time in _read_as_local(a_time, zone) or a_time in _read_as_local(b_time, zone):
                    return "timezone_shift"
        return "value_mismatch"
    if column.kind == "text":
        if source.strip(" ") == target.strip(" "):
            return "whitespace"
        if source.lower() == target.lower():
            return "case_only"
    return "value_mismatch"
