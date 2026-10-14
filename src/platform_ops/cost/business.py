"""The migration's business case, from the same window (version 2.0, Phase 13).

The legacy Postgres stays as the company's operational database; what the
migration retires is its reporting replica, which ran the hand-written reports
(``cost.legacy_reporting``). ClickHouse (``cost.clickhouse``) runs for the
whole window: built up from the first day, in parallel with the replica during
the parallel run, and alone after the cutover the reconciliation recommends.

So, month by month: the replica until the cutover, ClickHouse throughout, and
the overlap, when the company pays for both. The run rates before and after,
the one-off cost of running both until cutover, and the months the saving
takes to pay it back, all follow from those stated prices and the cutover date,
with the reconciliation's own share of ClickHouse's compute shown on the side.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from platform_ops.common.config import StatedCost
from platform_ops.cost.cluster import MICRO, pools, window_months


@dataclass(frozen=True)
class MonthCase:
    month: str
    days: int
    legacy_days: int
    legacy_usd: Decimal
    clickhouse_usd: Decimal
    reconcile_usd: Decimal

    @property
    def both(self) -> bool:
        return self.legacy_days > 0


@dataclass(frozen=True)
class BusinessCase:
    cutover: datetime | None
    months: tuple[MonthCase, ...]
    legacy_monthly: Decimal
    clickhouse_monthly: Decimal

    @property
    def monthly_saving(self) -> Decimal:
        return self.legacy_monthly - self.clickhouse_monthly

    @property
    def overlap_usd(self) -> Decimal:
        """What running ClickHouse cost while the replica still served the reports."""
        total = Decimal(0)
        for m in self.months:
            if m.days:
                total += m.clickhouse_usd * Decimal(m.legacy_days) / Decimal(m.days)
        return total.quantize(MICRO)

    @property
    def payback_months(self) -> Decimal | None:
        if self.monthly_saving <= 0:
            return None
        return (self.overlap_usd / self.monthly_saving).quantize(Decimal("0.1"))


def business_case(
    clickhouse: StatedCost,
    legacy: StatedCost,
    start: datetime,
    days: int,
    cutover: datetime | None,
    reconcile_by_month: Mapping[str, Decimal],
) -> BusinessCase:
    months = window_months(start, days)
    legacy_months = window_months(
        start, days if cutover is None else max(0, (cutover.date() - start.date()).days + 1)
    )
    ch = {p.month: p.total_usd for p in pools(clickhouse, months)}
    old = {p.month: p.total_usd for p in pools(legacy, legacy_months)}
    rows = tuple(
        MonthCase(
            month=month,
            days=n,
            legacy_days=legacy_months.get(month, 0),
            legacy_usd=old.get(month, Decimal(0)),
            clickhouse_usd=ch[month],
            reconcile_usd=reconcile_by_month.get(month, Decimal(0)),
        )
        for month, n in sorted(months.items())
    )
    return BusinessCase(
        cutover=cutover,
        months=rows,
        legacy_monthly=Decimal(str(legacy.compute_usd + legacy.storage_usd)),
        clickhouse_monthly=Decimal(str(clickhouse.compute_usd + clickhouse.storage_usd)),
    )
