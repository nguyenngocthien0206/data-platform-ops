"""The self-managed ClickHouse's bill, shared out (version 2.0, Phase 13).

A self-managed cluster does not bill per query: the company pays for machines
and disks by the month (``cost.clickhouse``, from public list prices). To show
each team its share, that stated cost is split, month by month, over the days
of the window that fall in each month:

- **compute** (the machines) is shared by the bytes each query read, as
  ClickHouse measured them; rows read are reported alongside;
- **storage** (the disks) is shared by the uncompressed bytes of each table at
  the month's end (``cost.storage``).

Every share is exact to 1e-12 dollars, and whatever rounding leaves over goes
to the largest share, so the shares always add up to the stated cost: the
platform team can hand the finance team a split that reconciles to the invoice.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from platform_ops.common.config import StatedCost

MICRO = Decimal("0.000000000001")


def window_months(start: datetime, days: int) -> dict[str, int]:
    """Days of the window in each month, as ``YYYY-MM``."""
    found: dict[str, int] = {}
    for offset in range(days):
        month = f"{start + timedelta(days=offset):%Y-%m}"
        found[month] = found.get(month, 0) + 1
    return found


def _fraction(month: str, days: int) -> Decimal:
    year, number = (int(part) for part in month.split("-"))
    return Decimal(days) / Decimal(calendar.monthrange(year, number)[1])


@dataclass(frozen=True)
class MonthPool:
    month: str
    days: int
    compute_usd: Decimal
    storage_usd: Decimal

    @property
    def total_usd(self) -> Decimal:
        return self.compute_usd + self.storage_usd


def pools(stated: StatedCost, months: Mapping[str, int]) -> list[MonthPool]:
    """The stated monthly cost, prorated to the window's days in each month."""
    compute = Decimal(str(stated.compute_usd))
    storage = Decimal(str(stated.storage_usd))
    return [
        MonthPool(
            month,
            days,
            (compute * _fraction(month, days)).quantize(MICRO),
            (storage * _fraction(month, days)).quantize(MICRO),
        )
        for month, days in sorted(months.items())
    ]


def share(total: Decimal, weights: Mapping[str, int]) -> dict[str, Decimal]:
    """Split ``total`` by ``weights``, exactly: the rounding remainder goes to the
    largest weight (ties to the smallest key). Nothing to weigh, nothing shared."""
    positive = {k: w for k, w in weights.items() if w > 0}
    if not positive:
        return {}
    whole = sum(positive.values())
    shares = {k: (total * Decimal(w) / Decimal(whole)).quantize(MICRO) for k, w in positive.items()}
    largest = min(positive, key=lambda k: (-positive[k], k))
    shares[largest] += total - sum(shares.values())
    return shares


def compute_shares(
    pools_by_month: Mapping[str, MonthPool],
    queries: Iterable[tuple[str, str, int]],
) -> dict[str, Decimal]:
    """Each billable query's share of its month's compute: ``(query_id, month, bytes read)``."""
    by_month: dict[str, dict[str, int]] = {}
    for query_id, month, read_bytes in queries:
        by_month.setdefault(month, {})[query_id] = read_bytes
    out: dict[str, Decimal] = {}
    for month, weights in by_month.items():
        pool = pools_by_month.get(month)
        if pool is not None:
            out.update(share(pool.compute_usd, weights))
    return out


def storage_shares(
    pools_by_month: Mapping[str, MonthPool],
    tables: Iterable[tuple[str, str, int]],
) -> dict[tuple[str, str], Decimal]:
    """Each table's share of its month's storage: ``(month, table, uncompressed bytes)``."""
    by_month: dict[str, dict[str, int]] = {}
    for month, table, size in tables:
        by_month.setdefault(month, {})[table] = size
    out: dict[tuple[str, str], Decimal] = {}
    for month, weights in by_month.items():
        pool = pools_by_month.get(month)
        if pool is not None:
            for table, usd in share(pool.storage_usd, weights).items():
                out[(month, table)] = usd
    return out
