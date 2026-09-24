"""The legacy system's data: one calendar year of customers, orders and payments.

It is generated rather than copied from the Phase 1 raw data because a legacy
system looks different: it stores local wall-clock time, pads some text,
carries high-precision decimals, and allows NULLs next to keys (guest orders,
unmatched payments). It also has to cover a whole year, so its local times
cross both daylight saving changes; the Phase 1 data only spans a winter.

Everything is derived from a stable hash of the seed and the row id inside
DuckDB, so two runs produce identical tables. Local times fall between 06:00
and midnight: a local time inside the hour the clocks go back is ambiguous,
and engines resolve that ambiguity differently (ADR 0009).
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from platform_ops.common.config import Settings
from platform_ops.reconcile.schema import TABLES, TableSpec, arrow_schema

FLOORS = {"customers": 200, "orders": 1000, "payments": 1000}

FIRST_NAMES = [
    "Anna", "Ben", "Chloe", "David", "Emma", "Farid", "Grace", "Hiro", "Isabel", "Jonas",
    "Karin", "Liam", "Maya", "Noah", "Olivia", "Pedro", "Quinn", "Rosa", "Sam", "Tara",
    "José", "Zoë", "Łukasz", "Anh", "Søren", "Chloé", "Mateo", "Aisha", "Yuki", "Björn",
]  # fmt: skip
LAST_NAMES = [
    "Smith", "Jones", "Garcia", "Miller", "Davis", "Lopez", "Wilson", "Taylor", "Moore", "Lee",
    "Nguyễn", "Müller", "O'Brien", "Kowalski", "Rossi", "Dubois", "Andersson", "Tanaka",
    "Novak", "Silva",
]  # fmt: skip
COMPANIES = [
    "Acme Corp", "Globex", "Initech", "Umbrella Group", "Stark Industries", "Wayne Enterprises",
    "Hooli", "Vandelay Import|Export", "Soylent", "Tyrell Co",
]  # fmt: skip
COUNTRIES = ["US", "CA", "GB", "DE", "FR", "VN", "BR", "JP"]
STATUSES = ["placed", "shipped", "delivered", "cancelled", "returned"]
CHANNELS = ["web", "mobile", "store", "phone", "partner"]
CURRENCIES = ["USD", "EUR", "GBP", "CAD"]
METHODS = ["card", "paypal", "bank_transfer", "gift_card", "apple_pay"]
PAYMENT_STATUSES = ["captured", "authorized", "refunded", "failed"]


def row_counts(settings: Settings) -> dict[str, int]:
    scale = settings.scale_factor
    return {
        table: max(FLOORS[table], round(rows * scale))
        for table, rows in settings.reconcile.rows.items()
    }


def _list(values: list[str]) -> str:
    return "[" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + "]"


def _pick(values: list[str], salt: str) -> str:
    return f"list_extract({_list(values)}, 1 + CAST(h('{salt}') % {len(values)} AS INTEGER))"


def generate(settings: Settings) -> dict[str, pa.Table]:
    """The three legacy tables as Arrow, typed exactly as their DDL says."""
    counts = row_counts(settings)
    year = settings.reconcile.legacy_year
    zone = settings.reconcile.legacy_timezone
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    seed = settings.seed

    def local_time(salt: str) -> str:
        # A day of the year, then 06:00 plus up to 18 hours, with microseconds.
        return (
            f"(TIMESTAMP '{year}-01-01' + to_days(CAST(h('{salt}d') % 365 AS INTEGER)) "
            f"+ to_seconds(CAST(21600 + h('{salt}s') % 64800 AS BIGINT)) "
            f"+ to_microseconds(CAST(h('{salt}u') % 1000000 AS BIGINT)))"
        )

    def decimal(salt: str, whole: str, digits: int, precision: int) -> str:
        fraction = f"lpad(CAST(h('{salt}') % {10**digits} AS VARCHAR), {digits}, '0')"
        return (
            f"CAST(CAST({whole} AS VARCHAR) || '.' || {fraction} AS DECIMAL({precision}, {digits}))"
        )

    def u(salt: str) -> str:
        return f"(h('{salt}') % 10000) / 10000.0"

    def build(table: TableSpec, n: int, select: str) -> pa.Table:
        # `h(salt)` is a stable 64-bit hash of seed, table, salt and row id.
        sql = select.replace("h('", f"hash({seed}, '{table.name}', id, '")
        arrow = con.execute(
            f"SELECT {sql} FROM range(1, {n + 1}) AS r(id) ORDER BY id"
        ).to_arrow_table()
        return arrow.select([c.name for c in table.columns]).cast(arrow_schema(table))

    name = f"""{_pick(FIRST_NAMES, "fn")} || '.' || {_pick(LAST_NAMES, "ln")}"""
    customers = build(
        TABLES[0],
        counts["customers"],
        f"""
        id AS customer_id,
        {_pick(FIRST_NAMES, "fn")} AS first_name,
        {_pick(LAST_NAMES, "ln")} AS last_name,
        CASE WHEN {u("mc")} < 0.1
             THEN {name} || id || '@Example.COM'
             ELSE lower({name}) || id || '@example.com'
        END AS email,
        CASE WHEN {u("co")} < 0.60 THEN NULL
             WHEN {u("co")} < 0.63 THEN ''
             WHEN {u("co")} < 0.68 THEN {_pick(COMPANIES, "cn")} || '   '
             ELSE {_pick(COMPANIES, "cn")}
        END AS company_name,
        {_pick(COUNTRIES, "ct")} AS country,
        CASE WHEN {u("ac")} < 0.02 THEN NULL ELSE {u("av")} < 0.9 END AS is_active,
        CASE WHEN {u("co")} < 0.60 THEN NULL
             ELSE {decimal("clf", "1000 + h('clw') % 500000", 2, 12)} END AS credit_limit,
        {local_time("cr")} AS created_local,
        ({local_time("cr")} AT TIME ZONE '{zone}') + to_seconds(CAST(h('up') % 7776000 AS BIGINT))
            AS updated_at
        """,
    )  # fmt: skip
    n_customers = counts["customers"]
    orders = build(
        TABLES[1],
        counts["orders"],
        f"""
        id AS order_id,
        CASE WHEN {u("gu")} < 0.04 THEN NULL ELSE 1 + h('cu') % {n_customers} END AS customer_id,
        {_pick(STATUSES, "st")} AS status,
        {_pick(CHANNELS, "ch")} AS channel,
        {_pick(CURRENCIES, "cy")} AS currency,
        CASE WHEN {u("en")} < 0.005
             THEN {decimal("af", "10000000000 + h('aw') % 990000000000", 6, 20)}
             ELSE {decimal("af", "5 + h('aw') % 5000", 6, 20)} END AS amount,
        {decimal("fx", "h('fw') % 2", 8, 18)} AS fx_rate,
        CASE WHEN {u("gn")} < 0.01 THEN NULL ELSE {u("gi")} < 0.08 END AS is_gift,
        {local_time("or")} AS ordered_local,
        ({local_time("or")} AT TIME ZONE '{zone}') + to_seconds(CAST(h('cr') % 120 AS BIGINT))
            AS created_at
        """,
    )  # fmt: skip
    n_orders = counts["orders"]
    payments = build(
        TABLES[2],
        counts["payments"],
        f"""
        id AS payment_id,
        CASE WHEN {u("um")} < 0.01 THEN NULL ELSE 1 + h('oi') % {n_orders} END AS order_id,
        {_pick(METHODS, "me")} AS method,
        {_pick(PAYMENT_STATUSES, "ps")} AS status,
        {decimal("pf", "1 + h('pw') % 5000", 2, 12)} AS amount,
        CAST(ROUND({decimal("pf", "1 + h('pw') % 5000", 2, 12)} * 0.029 + 0.30, 4)
             AS DECIMAL(10, 4)) AS fee,
        {_pick(PAYMENT_STATUSES, "ps")} = 'refunded' AS is_refund,
        {local_time("pa")} AS paid_local,
        CASE WHEN {u("se")} < 0.10 THEN NULL
             ELSE ({local_time("pa")} AT TIME ZONE '{zone}')
                  + to_seconds(CAST(h('sd') % 259200 AS BIGINT)) END AS settled_at
        """,
    )  # fmt: skip
    con.close()
    return {"customers": customers, "orders": orders, "payments": payments}
