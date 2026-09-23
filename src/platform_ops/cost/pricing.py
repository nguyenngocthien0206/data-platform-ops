"""Turning what queries did into money, through a pluggable ``PricingModel``.

Attribution and recommendations only see :class:`PricedQuery` rows, so a new
pricing model (or a vendor's real bill) can replace these without touching
them. Two models ship:

- :class:`ScanPricing`: on-demand, pay per byte scanned, with a minimum billed
  per table referenced (BigQuery style).
- :class:`ComputePricing`: pay for the time a warehouse is running (Snowflake
  style). Queries queue on their workload's warehouse; the warehouse starts on
  the first query, bills at least a minimum per start, and keeps running (and
  billing) until it has been idle for the timeout. A burst's cost is shared by
  its queries in proportion to their modelled duration, so the idle tail is
  charged to the workload whose bursty traffic caused it.

All money is ``Decimal``, and all times are integer milliseconds, so totals are
exact and identical on every run.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from platform_ops.common.config import ActorType, ComputeRates, ScanRates

TIB = 2**40
MICRO = Decimal("0.000000000001")  # money is kept to 1e-12 dollars


@dataclass(frozen=True)
class EstimatedQuery:
    query_id: str
    actor_type: ActorType
    started_at: datetime
    modeled_ms: int
    table_bytes: tuple[int, ...]


@dataclass(frozen=True)
class PricedQuery:
    query_id: str
    model: str
    usd: Decimal
    billed_bytes: int = 0
    billed_ms: int = 0
    warehouse: str | None = None
    burst: str | None = None


class PricingModel(Protocol):
    name: str

    def price(self, queries: Sequence[EstimatedQuery]) -> list[PricedQuery]:  # pragma: no cover
        ...


@dataclass(frozen=True)
class ScanPricing:
    rates: ScanRates
    name: str = "scan"

    def billed_bytes(self, query: EstimatedQuery) -> int:
        minimum = self.rates.minimum_billed_bytes_per_table
        return sum(max(b, minimum) for b in query.table_bytes)

    def price(self, queries: Sequence[EstimatedQuery]) -> list[PricedQuery]:
        rate = Decimal(str(self.rates.usd_per_tib))
        priced = []
        for query in sorted(queries, key=lambda q: q.query_id):
            billed = self.billed_bytes(query)
            usd = (Decimal(billed) * rate / TIB).quantize(MICRO)
            priced.append(PricedQuery(query.query_id, self.name, usd, billed_bytes=billed))
        return priced


@dataclass(frozen=True)
class Burst:
    """One stretch of time a warehouse was running."""

    warehouse: str
    started_ms: int
    last_end_ms: int
    queries: tuple[tuple[str, int], ...]  # (query_id, modelled ms)


@dataclass(frozen=True)
class ComputePricing:
    rates: ComputeRates
    name: str = "compute"

    def bursts(self, queries: Sequence[EstimatedQuery]) -> list[Burst]:
        """Lay queries out on their warehouses and group them into running periods."""
        idle_ms = self.rates.idle_timeout_seconds * 1000
        by_warehouse: dict[str, list[EstimatedQuery]] = defaultdict(list)
        for query in queries:
            by_warehouse[self.rates.warehouses[query.actor_type]].append(query)

        bursts: list[Burst] = []
        for warehouse in sorted(by_warehouse):
            ordered = sorted(by_warehouse[warehouse], key=lambda q: (q.started_at, q.query_id))
            current: list[tuple[str, int]] = []
            burst_start = last_end = 0
            for query in ordered:
                submitted = _ms(query.started_at)
                if current and submitted > last_end + idle_ms:
                    bursts.append(Burst(warehouse, burst_start, last_end, tuple(current)))
                    current = []
                if not current:
                    burst_start = submitted
                    last_end = submitted
                start = max(submitted, last_end)  # one query at a time
                last_end = start + query.modeled_ms
                current.append((query.query_id, query.modeled_ms))
            if current:
                bursts.append(Burst(warehouse, burst_start, last_end, tuple(current)))
        return bursts

    def burst_seconds(self, burst: Burst) -> int:
        """Billed seconds: running time plus the idle tail, at least the minimum."""
        running_ms = burst.last_end_ms - burst.started_ms + self.rates.idle_timeout_seconds * 1000
        seconds = -(-running_ms // 1000)  # per-second billing, rounded up
        return max(self.rates.minimum_billed_seconds, seconds)

    def price(self, queries: Sequence[EstimatedQuery]) -> list[PricedQuery]:
        per_second = (
            Decimal(str(self.rates.credits_per_hour))
            * Decimal(str(self.rates.usd_per_credit))
            / Decimal(3600)
        )
        priced: list[PricedQuery] = []
        for burst in self.bursts(queries):
            seconds = self.burst_seconds(burst)
            cost = Decimal(seconds) * per_second
            total_ms = sum(ms for _, ms in burst.queries)
            burst_id = f"{burst.warehouse}@{burst.started_ms}"
            for query_id, ms in burst.queries:
                share = cost * Decimal(ms) / Decimal(total_ms) if total_ms else Decimal(0)
                billed_ms = (seconds * 1000 * ms) // total_ms if total_ms else 0
                priced.append(
                    PricedQuery(
                        query_id,
                        self.name,
                        share.quantize(MICRO),
                        billed_ms=billed_ms,
                        warehouse=burst.warehouse,
                        burst=burst_id,
                    )
                )
        return sorted(priced, key=lambda p: p.query_id)


def _ms(moment: datetime) -> int:
    """Milliseconds since the epoch for a naive simulated timestamp."""
    epoch = datetime(1970, 1, 1)
    delta = moment - epoch
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000
