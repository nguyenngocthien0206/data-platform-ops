"""Both pricing models (a SPEC Phase 2 acceptance item)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from platform_ops.common.config import ComputeRates, ScanRates
from platform_ops.cost.pricing import TIB, ComputePricing, EstimatedQuery, ScanPricing

T0 = datetime(2026, 1, 5, 2, 0, 0)
MB = 1024 * 1024

SCAN = ScanRates(usd_per_tib=6.25, minimum_billed_bytes_per_table=10 * MB)
WAREHOUSES = {"dbt": "transform_wh", "dashboard": "bi_wh", "adhoc": "adhoc_wh"}


def compute(idle: int = 300, minimum: int = 60) -> ComputePricing:
    return ComputePricing(
        ComputeRates(
            usd_per_credit=2.0,
            credits_per_hour=1.0,
            minimum_billed_seconds=minimum,
            idle_timeout_seconds=idle,
            warehouses=WAREHOUSES,  # type: ignore[arg-type]
        )
    )


def q(
    query_id: str,
    *,
    at: timedelta = timedelta(0),
    ms: int = 1000,
    tables: tuple[int, ...] = (),
    actor: str = "dbt",
) -> EstimatedQuery:
    return EstimatedQuery(query_id, actor, T0 + at, ms, tables)  # type: ignore[arg-type]


PER_SECOND = Decimal(2) / Decimal(3600)


# --- ScanPricing -------------------------------------------------------------------


def test_scan_cost_is_proportional_to_bytes() -> None:
    small, large = ScanPricing(SCAN).price([q("a", tables=(100 * MB,)), q("b", tables=(200 * MB,))])
    assert large.usd == pytest.approx(small.usd * 2)
    assert small.usd == (Decimal(100 * MB) * Decimal("6.25") / TIB).quantize(Decimal("1e-12"))


def test_scan_bills_the_minimum_per_table_referenced() -> None:
    priced = ScanPricing(SCAN).price([q("tiny", tables=(1, 0, 5 * MB))])[0]
    assert priced.billed_bytes == 3 * 10 * MB, "each of the three tables bills at least 10 MB"


def test_scan_query_touching_no_table_costs_nothing() -> None:
    assert ScanPricing(SCAN).price([q("select_1")])[0].usd == 0


def test_scan_one_tebibyte_costs_the_list_price() -> None:
    assert ScanPricing(SCAN).price([q("tib", tables=(TIB,))])[0].usd == Decimal("6.25")


# --- ComputePricing ----------------------------------------------------------------


def test_short_query_is_billed_the_minimum() -> None:
    pricing = compute(idle=0, minimum=60)
    (priced,) = pricing.price([q("a", ms=500)])
    assert priced.usd == (60 * PER_SECOND).quantize(Decimal("1e-12"))


def test_idle_tail_is_billed_after_the_last_query() -> None:
    pricing = compute(idle=300)
    (burst,) = pricing.bursts([q("a", ms=2000)])
    assert pricing.burst_seconds(burst) == 302


def test_queries_close_together_share_one_burst() -> None:
    pricing = compute(idle=300)
    bursts = pricing.bursts([q("a"), q("b", at=timedelta(seconds=200))])
    assert len(bursts) == 1
    assert pricing.burst_seconds(bursts[0]) == 200 + 1 + 300


def test_queries_further_apart_than_the_idle_timeout_are_separate_bursts() -> None:
    pricing = compute(idle=300)
    bursts = pricing.bursts([q("a"), q("b", at=timedelta(seconds=400))])
    assert len(bursts) == 2


def test_queries_submitted_together_queue_one_after_another() -> None:
    pricing = compute(idle=0, minimum=0)
    (burst,) = pricing.bursts([q("a", ms=3000), q("b", ms=2000)])
    assert burst.last_end_ms - burst.started_ms == 5000


def test_each_workload_runs_on_its_own_warehouse() -> None:
    pricing = compute(idle=300)
    bursts = pricing.bursts([q("etl", actor="dbt"), q("bi", actor="dashboard")])
    assert sorted(b.warehouse for b in bursts) == ["bi_wh", "transform_wh"]


def test_burst_cost_is_shared_by_modelled_duration() -> None:
    pricing = compute(idle=300)
    priced = {p.query_id: p for p in pricing.price([q("big", ms=3000), q("small", ms=1000)])}
    assert abs(priced["big"].usd - priced["small"].usd * 3) < Decimal("1e-9")
    total = priced["big"].usd + priced["small"].usd
    expected = Decimal(304) * PER_SECOND  # 4 s of work + 300 s idle
    assert abs(total - expected) < Decimal("1e-9")


def test_compute_result_does_not_depend_on_input_order() -> None:
    queries = [q(f"q{i}", at=timedelta(seconds=37 * i), ms=500 + i) for i in range(40)]
    shuffled = queries[:]
    random.Random(7).shuffle(shuffled)
    pricing = compute()
    assert pricing.price(queries) == pricing.price(shuffled)
