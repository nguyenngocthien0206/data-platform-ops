"""Raw data for the simulated company, generated inside DuckDB.

Two rules make the content deterministic:

1. Every "random" value is a pure hash of ``(seed, table, row id, column)``.
   DuckDB's ``random()`` depends on how work is split across threads, so it is
   never used. A hash depends only on its inputs.
2. Rows are inserted in primary-key order, so the physical row order and the
   row groups are the same on every run.

What is *not* deterministic is the on-disk encoding. DuckDB picks a compression
codec per segment when it writes, and that choice can differ between two runs
of identical data (seen as FSST versus Dictionary on the same column). Anything
that needs a stable size, such as Phase 2's scan estimate, must be computed from
the logical data, not from compressed storage. See ADR 0004.

Each table is defined over a fixed id space that covers the history *and* the
whole simulated window. A load inserts only the rows whose ``_loaded_at`` falls
in ``(after, until]``. The initial seed is one load ending at the window start;
Phase 2's daily append is the same call over one simulated day. Nothing is ever
regenerated, so content only grows, the way a real append-only source does.

Event times grow with the square root of the row id, so the business gets
busier over time. That shape is also what makes "whose parent existed yet"
cheap to compute: an order at fraction ``u`` of its id space can only reference
customers from the first ``u`` of theirs, because both map to the same instant.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

from platform_ops.common.config import Settings
from platform_ops.common.db import RAW_SCHEMA

# Rows that have arrived by the window start, at scale 1.0. The id space is then
# extended along the same growth curve to cover the simulated window, so the
# window length (a simulation setting) never changes how big the company's
# history is. Floors keep tiny scales usable in tests.
HISTORY_COUNTS: dict[str, int] = {
    "customers": 48_000,
    "products": 2_000,
    "orders": 400_000,
    "web_sessions": 1_450_000,
    "marketing_campaigns": 80,
    "support_tickets": 28_000,
}
# Event time grows with sqrt(id) for these, linearly for campaigns, and the
# product catalogue is a fixed snapshot.
_SQRT_GROWTH = frozenset({"customers", "orders", "web_sessions", "support_tickets"})
_LINEAR_GROWTH = frozenset({"marketing_campaigns"})
FLOORS: dict[str, int] = {
    "customers": 200,
    "products": 50,
    "orders": 1_000,
    "web_sessions": 2_000,
    "marketing_campaigns": 12,
    "support_tickets": 100,
}

# Load order matters: children read their parents from the raw tables.
TABLES: tuple[str, ...] = (
    "customers",
    "products",
    "orders",
    "order_items",
    "payments",
    "web_sessions",
    "marketing_campaigns",
    "marketing_spend",
    "support_tickets",
)

_DDL: dict[str, str] = {
    "customers": """
        customer_id BIGINT, first_name VARCHAR, last_name VARCHAR, email VARCHAR,
        phone VARCHAR, country VARCHAR, referral_source VARCHAR,
        signed_up_at TIMESTAMP, _loaded_at TIMESTAMP""",
    "products": """
        product_id BIGINT, product_name VARCHAR, category VARCHAR,
        unit_price DECIMAL(10, 2), unit_cost DECIMAL(10, 2), is_active BOOLEAN,
        created_at TIMESTAMP, _loaded_at TIMESTAMP""",
    "orders": """
        order_id BIGINT, customer_id BIGINT, ordered_at TIMESTAMP, status VARCHAR,
        channel VARCHAR, discount_code VARCHAR, currency VARCHAR, _loaded_at TIMESTAMP""",
    "order_items": """
        order_item_id BIGINT, order_id BIGINT, line_number INTEGER, product_id BIGINT,
        quantity INTEGER, unit_price DECIMAL(10, 2), _loaded_at TIMESTAMP""",
    "payments": """
        payment_id BIGINT, order_id BIGINT, payment_method VARCHAR, status VARCHAR,
        amount DECIMAL(12, 2), created_at TIMESTAMP, _loaded_at TIMESTAMP""",
    "web_sessions": """
        session_id BIGINT, customer_id BIGINT, started_at TIMESTAMP, device VARCHAR,
        landing_page VARCHAR, utm_source VARCHAR, campaign_id BIGINT,
        pageviews INTEGER, duration_seconds INTEGER, converted BOOLEAN,
        _loaded_at TIMESTAMP""",
    "marketing_campaigns": """
        campaign_id BIGINT, campaign_name VARCHAR, channel VARCHAR, start_date DATE,
        end_date DATE, budget DECIMAL(12, 2), _loaded_at TIMESTAMP""",
    "marketing_spend": """
        spend_id BIGINT, campaign_id BIGINT, spend_date DATE, spend DECIMAL(12, 2),
        impressions BIGINT, clicks BIGINT, _loaded_at TIMESTAMP""",
    "support_tickets": """
        ticket_id BIGINT, customer_id BIGINT, order_id BIGINT, category VARCHAR,
        priority VARCHAR, status VARCHAR, created_at TIMESTAMP, resolved_at TIMESTAMP,
        satisfaction_score INTEGER, _loaded_at TIMESTAMP""",
}

_PRIMARY_KEYS: dict[str, str] = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
    "web_sessions": "session_id",
    "marketing_campaigns": "campaign_id",
    "marketing_spend": "spend_id",
    "support_tickets": "ticket_id",
}

_FIRST_NAMES = [
    "Anna", "Ben", "Chloe", "David", "Emma", "Felix", "Grace", "Hugo", "Ines", "Jack",
    "Kai", "Lina", "Minh", "Nora", "Omar", "Paula", "Quinn", "Rosa", "Sven", "Thao",
]  # fmt: skip
_LAST_NAMES = [
    "Nguyen", "Smith", "Muller", "Garcia", "Tanaka", "Silva", "Kowalski", "Brown", "Rossi",
    "Dubois", "Kim", "Patel", "Jensen", "Novak", "Tran", "Lopez", "Weber", "Moreau",
]  # fmt: skip
_DOMAINS = ["example.com", "mail.test", "inbox.test", "post.test"]
_COUNTRIES = ["US", "GB", "DE", "FR", "VN", "JP", "CA", "AU", "BR", "IN"]
_CATEGORIES = ["apparel", "electronics", "home", "beauty", "sports", "toys", "books", "garden"]
_ADJECTIVES = ["Classic", "Pro", "Eco", "Mini", "Deluxe", "Smart", "Urban", "Nordic"]
_CHANNELS = ["paid_search", "social", "email", "display", "affiliate"]
_THEMES = ["spring_sale", "back_to_school", "black_friday", "holiday", "brand", "retention"]
_LANDING = ["/", "/products", "/sale", "/blog", "/search", "/cart"]
_TICKET_CATEGORIES = ["shipping", "refund", "product_question", "account", "payment", "other"]
# Repeated values weight the draw: half of all payments are by card.
_PAYMENT_METHODS = ["card", "card", "card", "paypal", "bank_transfer", "gift_card"]
_PRIORITIES = ["low", "normal", "normal", "high", "urgent"]


def _sql_list(values: list[str]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def _ts(value: datetime) -> str:
    return f"TIMESTAMP '{value:%Y-%m-%d %H:%M:%S}'"


@dataclass(frozen=True)
class _Sql:
    """Builds the hash-based SQL fragments for one seed."""

    seed: int

    def h(self, table: str, column: str, row: str) -> str:
        """A 64-bit unsigned hash, fully determined by its inputs."""
        return f"hash({self.seed}, '{table}', {row}, '{column}')"

    def u(self, table: str, column: str, row: str) -> str:
        """A uniform double in [0, 1)."""
        return f"(({self.h(table, column, row)} % 1000000)::DOUBLE / 1000000.0)"

    def pick(self, table: str, column: str, row: str, values: list[str]) -> str:
        """One element of ``values``, chosen by hash."""
        n = len(values)
        return f"({_sql_list(values)})[1 + ({self.h(table, column, row)} % {n})::INTEGER]"

    def between(self, table: str, column: str, row: str, low: int, high: int) -> str:
        """An integer in [low, high]."""
        return f"({low} + ({self.h(table, column, row)} % {high - low + 1})::BIGINT)"


@dataclass(frozen=True)
class RawDataPlan:
    """Everything a load needs, resolved from settings once."""

    seed: int
    history_start: datetime
    window_end: datetime
    counts: dict[str, int]

    @classmethod
    def from_settings(cls, settings: Settings) -> RawDataPlan:
        window = settings.simulation
        history_share = (window.start - window.history_start) / (window.end - window.history_start)
        counts: dict[str, int] = {}
        for table, base in HISTORY_COUNTS.items():
            by_start = max(FLOORS[table], round(base * settings.scale_factor))
            # Invert the growth curve: the full id space whose first part lands
            # exactly `by_start` rows before the window opens.
            if table in _SQRT_GROWTH:
                counts[table] = round(by_start / history_share**2)
            elif table in _LINEAR_GROWTH:
                counts[table] = round(by_start / history_share)
            else:
                counts[table] = by_start
        return cls(
            seed=settings.seed,
            history_start=settings.simulation.history_start,
            window_end=settings.simulation.end,
            counts=counts,
        )

    @property
    def span_seconds(self) -> int:
        return int((self.window_end - self.history_start).total_seconds())

    @property
    def span_days(self) -> int:
        return (self.window_end - self.history_start).days


def create_raw_tables(connection: duckdb.DuckDBPyConnection) -> None:
    """Drop and recreate every raw table, empty. The seed starts from here."""
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
    for table in TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {RAW_SCHEMA}.{table}")
        connection.execute(f"CREATE TABLE {RAW_SCHEMA}.{table} ({_DDL[table]})")


@dataclass(frozen=True)
class _Window:
    """The load window, and the id ranges that can possibly land in it.

    Event time rises with the id (square-root growth), so the rows loaded in a
    window come from a narrow range of ids. Generating only that range, instead
    of the whole id space and filtering, is what makes a one-day append cheap:
    about 2,400 new orders out of 620,000 possible ids. The range is widened by
    the table's longest load lag and a small margin, so pruning never drops a
    row; ``test_appending_a_day_equals_seeding_through_that_day`` checks it.
    """

    after: datetime | None
    until: datetime

    def ids(self, plan: RawDataPlan, n: int, max_lag: timedelta) -> tuple[int, int]:
        def share(moment: datetime) -> float:
            x = (moment - plan.history_start).total_seconds() / plan.span_seconds
            return min(1.0, max(0.0, x)) ** 2

        high = min(n, math.ceil(n * share(self.until)) + 2)
        if self.after is None:
            return 1, max(1, high)
        low = max(1, math.floor(n * share(self.after - max_lag - timedelta(seconds=2))) - 1)
        return low, max(low, high)

    def loaded(self, column: str = "_loaded_at") -> str:
        upper = f"{column} <= {_ts(self.until)}"
        return upper if self.after is None else f"{column} > {_ts(self.after)} AND {upper}"


_BUILDERS: dict[str, Callable[[RawDataPlan, _Sql, _Window], str]] = {}


def generated_rows(plan: RawDataPlan, table: str, until: datetime) -> str:
    """SQL for every row of ``table`` the generator produces with ``_loaded_at <= until``.

    This is the ground truth of what the table should hold. Fault repair uses it
    to put back rows or values a fault removed.
    """
    window = _Window(None, until)
    select = _BUILDERS[table](plan, _Sql(plan.seed), window)
    return f"SELECT * FROM ({select}) WHERE {window.loaded()}"


def load_window(
    connection: duckdb.DuckDBPyConnection,
    plan: RawDataPlan,
    after: datetime | None,
    until: datetime,
    tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Insert every row whose ``_loaded_at`` is in ``(after, until]``.

    ``tables`` limits the load to some tables, which is how a stale source is
    simulated. Rows are inserted by column name into whatever columns the table
    has now, so loading keeps working while a schema-change fault has dropped
    or renamed a column, the way an upstream feed would.

    Returns the number of rows inserted per table.
    """
    bounds = _Window(after, until)
    window = bounds.loaded()
    inserted: dict[str, int] = {}
    for table in TABLES:
        if tables is not None and table not in tables:
            continue
        select = _BUILDERS[table](plan, _Sql(plan.seed), bounds)
        columns = ", ".join(_shared_columns(connection, table))
        before = _count(connection, table)
        connection.execute(
            f"INSERT INTO {RAW_SCHEMA}.{table} ({columns}) "
            f"SELECT {columns} FROM ({select}) WHERE {window} ORDER BY {_PRIMARY_KEYS[table]}"
        )
        inserted[table] = _count(connection, table) - before
    return inserted


def _ddl_columns(table: str) -> list[tuple[str, str]]:
    """(name, type) pairs from ``_DDL``, splitting on commas outside parentheses."""
    parts = re.split(r",(?![^(]*\))", _DDL[table])
    return [(name, data_type.strip()) for name, _, data_type in
            (part.strip().partition(" ") for part in parts)]  # fmt: skip


def generated_columns(table: str) -> list[str]:
    """The columns the generator produces for ``table``, in table order."""
    return [name for name, _ in _ddl_columns(table)]


def column_type(table: str, column: str) -> str:
    """The generator's type for ``table.column``, used to re-add a dropped column."""
    for name, data_type in _ddl_columns(table):
        if name == column:
            return data_type
    raise KeyError(f"{table}.{column}")


def _shared_columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    present = {
        str(r[0])
        for r in connection.execute(
            "SELECT column_name FROM duckdb_columns() WHERE schema_name = ? AND table_name = ?",
            [RAW_SCHEMA, table],
        ).fetchall()
    }
    return [c for c in generated_columns(table) if c in present]


def seed(connection: duckdb.DuckDBPyConnection, settings: Settings) -> dict[str, int]:
    """Recreate the raw schema and load everything up to the window start."""
    plan = RawDataPlan.from_settings(settings)
    create_raw_tables(connection)
    return load_window(connection, plan, after=None, until=settings.simulation.start)


PRIMARY_KEYS: dict[str, str] = _PRIMARY_KEYS
# Sources that change in place during the simulated window. Everything else
# only ever receives new rows.
MUTABLE_TABLES: frozenset[str] = frozenset({"products", "customers"})


def apply_daily_changes(
    connection: duckdb.DuckDBPyConnection,
    plan: RawDataPlan,
    day: int,
    at: datetime,
    price_changes: int,
    profile_changes: int,
) -> dict[str, int]:
    """Change some existing rows in place, the way a real catalogue or CRM does.

    A handful of products get a new price and a handful of customers a new phone
    number or referral source, each chosen by hash of the row and the day, so the
    same rows change on every run. Changed rows get ``_loaded_at = at``, as a
    change-data-capture feed would stamp them. This is what makes these two
    sources not append-only, so the incremental-model recommendation has
    something to rule out.
    """
    s = _Sql(plan.seed)
    changed: dict[str, int] = {}
    n_products = plan.counts["products"]
    product_day = f"product_id * 1000 + {day}"
    customer_day = f"customer_id * 1000 + {day}"
    new_price = f"round(unit_price * (0.9 + 0.2 * {s.u('price_change', 'pct', product_day)}), 2)"
    new_phone = f"({s.h('profile_change', 'phone', customer_day)} % 10000000)::VARCHAR"
    connection.execute(
        f"""UPDATE {RAW_SCHEMA}.products
            SET unit_price = {new_price},
                _loaded_at = {_ts(at)}
            WHERE ({s.h("price_change", "pick", product_day)} % {n_products})
                  < {price_changes}"""
    )
    changed["products"] = _stamped_at(connection, "products", at)
    n_customers = plan.counts["customers"]
    connection.execute(
        f"""UPDATE {RAW_SCHEMA}.customers
            SET phone = '+1-555-' || lpad({new_phone}, 7, '0'),
                referral_source = coalesce(referral_source, 'organic'),
                _loaded_at = {_ts(at)}
            WHERE ({s.h("profile_change", "pick", customer_day)} % {n_customers})
                  < {profile_changes}
              AND _loaded_at < {_ts(at)}"""
    )
    changed["customers"] = _stamped_at(connection, "customers", at)
    return changed


def _stamped_at(connection: duckdb.DuckDBPyConnection, table: str, at: datetime) -> int:
    row = connection.execute(
        f"SELECT count(*) FROM {RAW_SCHEMA}.{table} WHERE _loaded_at = {_ts(at)}"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _count(connection: duckdb.DuckDBPyConnection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {RAW_SCHEMA}.{table}").fetchone()
    return int(row[0]) if row is not None else 0


def _event_time(plan: RawDataPlan, s: _Sql, table: str, row: str, n: int) -> str:
    """Event time for row ``row`` of ``n``: square-root growth over the full span."""
    frac = f"(({row} - 1 + {s.u(table, 't', row)}) / {n})"
    offset = f"CAST(floor({plan.span_seconds} * sqrt({frac})) AS BIGINT)"
    return f"({_ts(plan.history_start)} + to_seconds({offset}))"


def _parent_id(s: _Sql, table: str, column: str, row: str, n_self: int, n_parent: int) -> str:
    """A parent id that already existed at this row's event time (see module docstring)."""
    eligible = f"greatest(1, CAST(floor({n_parent} * (({row} - 1) / {n_self})) AS BIGINT))"
    return f"(1 + ({s.h(table, column, row)} % {eligible})::BIGINT)"


def _customers_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["customers"]
    dups = max(1, round(n * 0.02))
    t = "customers"
    first = s.pick(t, "first", "i", _FIRST_NAMES)
    last = s.pick(t, "last", "i", _LAST_NAMES)
    domain = s.pick(t, "dom", "i", _DOMAINS)
    base = f"""
        SELECT
            i AS customer_id,
            {first} AS first_name,
            {last} AS last_name,
            lower({first} || '.' || {last} || i::VARCHAR || '@' || {domain}) AS email,
            CASE WHEN {s.u(t, "phone_null", "i")} < 0.3 THEN NULL
                 ELSE '+1-555-' || lpad(({s.h(t, "phone", "i")} % 10000000)::VARCHAR, 7, '0')
            END AS phone,
            {s.pick(t, "country", "i", _COUNTRIES)} AS country,
            CASE WHEN {s.u(t, "ref_null", "i")} < 0.4 THEN NULL
                 ELSE {s.pick(t, "ref", "i", ["organic", *_CHANNELS])}
            END AS referral_source,
            {_event_time(plan, s, t, "i", n)} AS signed_up_at
        FROM range(1, {n + 1}) r(i)
    """
    # Roughly 2% of customers register twice: same person, same email apart from
    # case or stray whitespace, new id, a little later. Staging has to fold it.
    duplicates = f"""
        SELECT
            {n} + k AS customer_id,
            b.first_name,
            b.last_name,
            CASE ({s.h(t, "dup_variant", "k")} % 3)
                WHEN 0 THEN upper(b.email)
                WHEN 1 THEN '  ' || b.email
                ELSE b.email || ' '
            END AS email,
            NULL AS phone,
            b.country,
            b.referral_source,
            b.signed_up_at + to_days({s.between(t, "dup_days", "k", 1, 60)}::INTEGER)
                AS signed_up_at
        FROM range(1, {dups + 1}) d(k)
        JOIN ({base}) b ON b.customer_id = 1 + ({s.h(t, "dup_of", "k")} % {n})::BIGINT
    """
    return f"""
        SELECT *, signed_up_at + to_minutes({s.between(t, "lag", "customer_id", 1, 30)})
            AS _loaded_at
        FROM ({base} UNION ALL {duplicates})
    """


def _products_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["products"]
    t = "products"
    catalog_start = plan.history_start - timedelta(days=730)
    price = f"round(5 + {s.u(t, 'price', 'i')} * 495, 2)"
    return f"""
        SELECT
            i AS product_id,
            {s.pick(t, "adj", "i", _ADJECTIVES)} || ' ' || upper(category[1]) || category[2:]
                || ' ' || i::VARCHAR AS product_name,
            category,
            unit_price::DECIMAL(10, 2) AS unit_price,
            round(unit_price * (0.40 + 0.35 * {s.u(t, "margin", "i")}), 2)::DECIMAL(10, 2)
                AS unit_cost,
            {s.u(t, "active", "i")} >= 0.08 AS is_active,
            created_at,
            created_at + INTERVAL 1 HOUR AS _loaded_at
        FROM (
            SELECT
                i,
                {s.pick(t, "category", "i", _CATEGORIES)} AS category,
                {price} AS unit_price,
                {_ts(catalog_start)} + to_days(({s.h(t, "created", "i")} % 730)::INTEGER)
                    AS created_at
            FROM range(1, {n + 1}) r(i)
        )
    """


def _orders_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["orders"]
    lo, hi = w.ids(plan, n, max_lag=timedelta(minutes=30))
    t = "orders"
    status = s.u(t, "status", "i")
    channel = s.u(t, "channel", "i")
    return f"""
        SELECT
            i AS order_id,
            {_parent_id(s, t, "customer", "i", n, plan.counts["customers"])} AS customer_id,
            ordered_at,
            CASE WHEN {status} < 0.84 THEN 'completed'
                 WHEN {status} < 0.88 THEN 'shipped'
                 WHEN {status} < 0.91 THEN 'pending'
                 WHEN {status} < 0.96 THEN 'cancelled'
                 ELSE 'returned' END AS status,
            CASE WHEN {channel} < 0.50 THEN 'web'
                 WHEN {channel} < 0.88 THEN 'mobile'
                 ELSE 'marketplace' END AS channel,
            CASE WHEN {s.u(t, "discount", "i")} < 0.15
                 THEN {s.pick(t, "code", "i", ["WELCOME10", "SPRING15", "VIP20", "FREESHIP"])}
            END AS discount_code,
            'USD' AS currency,
            ordered_at + to_minutes({s.between(t, "lag", "i", 1, 30)}) AS _loaded_at
        FROM (
            SELECT i, {_event_time(plan, s, t, "i", n)} AS ordered_at
            FROM range({lo}, {hi + 1}) r(i)
        )
    """


def _order_items_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    t = "order_items"
    line_id = "(o.order_id * 10 + line_number)"
    return f"""
        SELECT
            {line_id} AS order_item_id,
            o.order_id,
            line_number::INTEGER AS line_number,
            p.product_id,
            CASE WHEN {s.u(t, "qty", line_id)} < 0.70 THEN 1
                 WHEN {s.u(t, "qty", line_id)} < 0.90 THEN 2
                 ELSE 3 END AS quantity,
            p.unit_price,
            o._loaded_at
        FROM (
            SELECT order_id, _loaded_at,
                   unnest(range(1, 2 + ({s.h(t, "lines", "order_id")} % 5)::BIGINT)) AS line_number
            FROM {RAW_SCHEMA}.orders
            WHERE {w.loaded()}
        ) o
        JOIN {RAW_SCHEMA}.products p
          ON p.product_id = 1 + ({s.h(t, "product", line_id)} % {plan.counts["products"]})::BIGINT
    """


def _payments_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    t = "payments"
    pid = "order_id * 10 + part"
    method = s.pick(t, "method", "order_id", _PAYMENT_METHODS)
    late_days = s.between(t, "late_days", pid, 1, 5)
    late_minutes = s.between(t, "late_min", pid, 0, 600)
    lag_minutes = s.between(t, "lag", pid, 1, 20)
    delay_minutes = s.between(t, "delay", pid, 1, 120)
    # A payment lands at most about six days after its order (two hours to pay,
    # up to five days and ten hours late), so only the last week of orders can
    # have a payment arriving in this window.
    lo, hi = w.ids(plan, plan.counts["orders"], max_lag=timedelta(days=7))
    return f"""
        WITH totals AS (
            SELECT order_id, sum(quantity * unit_price) AS total
            FROM {RAW_SCHEMA}.order_items
            WHERE order_id BETWEEN {lo} AND {hi}
            GROUP BY order_id
        ),
        eligible AS (
            SELECT o.order_id, o.ordered_at, o.status, t.total,
                   o._loaded_at AS order_loaded_at,
                   CASE WHEN {s.u(t, "split", "o.order_id")} < 0.03 THEN 2 ELSE 1 END AS parts
            FROM {RAW_SCHEMA}.orders o
            JOIN totals t USING (order_id)
            WHERE o.order_id BETWEEN {lo} AND {hi}
              AND o.status NOT IN ('pending', 'cancelled')
              -- About 2% of completed orders never get a payment record at all.
              AND NOT (o.status = 'completed' AND {s.u(t, "missing", "o.order_id")} < 0.02)
        ),
        split AS (
            SELECT e.*, unnest(range(1, parts + 1)) AS part FROM eligible e
        )
        SELECT
            order_id * 10 + part AS payment_id,
            order_id,
            {method} AS payment_method,
            CASE WHEN status = 'returned' THEN 'refunded' ELSE 'succeeded' END AS status,
            CASE WHEN parts = 1 THEN total
                 WHEN part = 1 THEN round(total * 0.6, 2)
                 ELSE total - round(total * 0.6, 2) END::DECIMAL(12, 2) AS amount,
            created_at,
            -- About 5% of payments arrive one to five days late: the payment
            -- happened, the record reached the warehouse much later.
            -- A payment record never reaches the warehouse before its order does, so
            -- whenever a payment lands, the order it belongs to is already loaded.
            greatest(
                CASE WHEN {s.u(t, "late", pid)} < 0.05
                     THEN created_at + to_days({late_days}::INTEGER) + to_minutes({late_minutes})
                     ELSE created_at + to_minutes({lag_minutes})
                END,
                order_loaded_at + INTERVAL 1 MINUTE
            ) AS _loaded_at
        FROM (
            SELECT *, ordered_at + to_minutes({delay_minutes}) AS created_at
            FROM split
        )
    """


def _sessions_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["web_sessions"]
    lo, hi = w.ids(plan, n, max_lag=timedelta(minutes=60))
    t = "web_sessions"
    n_campaigns = plan.counts["marketing_campaigns"]
    # Campaigns start evenly across the span, so only the first sqrt(u) of them
    # had launched by the time of a session at id fraction u.
    launched = f"greatest(1, CAST(floor({n_campaigns} * sqrt((i - 1) / {n})) AS BIGINT))"
    return f"""
        SELECT
            i AS session_id,
            CASE WHEN {s.u(t, "anon", "i")} < 0.55 THEN NULL
                 ELSE {_parent_id(s, t, "customer", "i", n, plan.counts["customers"])}
            END AS customer_id,
            started_at,
            {s.pick(t, "device", "i", ["desktop", "mobile", "mobile", "tablet"])} AS device,
            {s.pick(t, "landing", "i", _LANDING)} AS landing_page,
            utm_source,
            CASE WHEN utm_source IS NOT NULL AND {s.u(t, "campaign_null", "i")} < 0.5
                 THEN 1 + ({s.h(t, "campaign", "i")} % {launched})::BIGINT
            END AS campaign_id,
            {s.between(t, "pages", "i", 1, 12)}::INTEGER AS pageviews,
            {s.between(t, "duration", "i", 5, 1800)}::INTEGER AS duration_seconds,
            {s.u(t, "converted", "i")} < 0.03 AS converted,
            started_at + to_minutes({s.between(t, "lag", "i", 5, 60)}) AS _loaded_at
        FROM (
            SELECT i, {_event_time(plan, s, t, "i", n)} AS started_at,
                   CASE WHEN {s.u(t, "utm_null", "i")} < 0.6 THEN NULL
                        ELSE {s.pick(t, "utm", "i", _CHANNELS)} END AS utm_source
            FROM range({lo}, {hi + 1}) r(i)
        )
    """


def _campaigns_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["marketing_campaigns"]
    t = "marketing_campaigns"
    start_offset = (
        f"CAST(floor({plan.span_days} * ((k - 1 + {s.u(t, 'start', 'k')}) / {n})) AS INTEGER)"
    )
    history_day = f"DATE '{plan.history_start:%Y-%m-%d}'"
    theme = s.pick(t, "theme", "k", _THEMES)
    duration = s.between(t, "duration", "k", 30, 400)
    return f"""
        SELECT
            k AS campaign_id,
            channel || '_' || {theme} || '_' || k::VARCHAR AS campaign_name,
            channel,
            start_date,
            (start_date + to_days({duration}::INTEGER))::DATE AS end_date,
            round(5000 + {s.u(t, "budget", "k")} * 95000, 2)::DECIMAL(12, 2) AS budget,
            -- Campaigns are set up a few days before launch.
            start_date::TIMESTAMP - INTERVAL 3 DAY + INTERVAL 9 HOUR AS _loaded_at
        FROM (
            SELECT k, {s.pick(t, "channel", "k", _CHANNELS)} AS channel,
                   ({history_day} + to_days({start_offset}))::DATE AS start_date
            FROM range(1, {n + 1}) r(k)
        )
    """


def _spend_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    t = "marketing_spend"
    spend_id = "(c.campaign_id * 1000 + d)"
    return f"""
        SELECT
            {spend_id} AS spend_id,
            c.campaign_id,
            (c.start_date + to_days(d::INTEGER))::DATE AS spend_date,
            round(c.budget / greatest(1, c.end_date - c.start_date)
                  * (0.6 + 0.8 * {s.u(t, "spend", spend_id)}), 2)::DECIMAL(12, 2) AS spend,
            {s.between(t, "impressions", spend_id, 500, 60000)} AS impressions,
            {s.between(t, "clicks", spend_id, 5, 1500)} AS clicks,
            -- Ad platforms deliver yesterday's spend in a morning batch.
            (c.start_date + to_days(d::INTEGER))::TIMESTAMP + INTERVAL 30 HOUR AS _loaded_at
        FROM (
            SELECT campaign_id, start_date, end_date, budget,
                   unnest(range(0, end_date - start_date)) AS d
            FROM {RAW_SCHEMA}.marketing_campaigns
        ) c
        WHERE (c.start_date + to_days(d::INTEGER))::DATE < DATE '{plan.window_end:%Y-%m-%d}'
    """


def _tickets_sql(plan: RawDataPlan, s: _Sql, w: _Window) -> str:
    n = plan.counts["support_tickets"]
    t = "support_tickets"
    status = s.u(t, "status", "i")
    return f"""
        SELECT
            i AS ticket_id,
            customer_id, order_id, category, priority, status, created_at, resolved_at,
            satisfaction_score,
            -- Closed tickets are exported when they close, open ones when opened.
            coalesce(resolved_at, created_at) + to_minutes({s.between(t, "lag", "i", 1, 45)})
                AS _loaded_at
        FROM (
            SELECT
                i,
                CASE WHEN {s.u(t, "guest", "i")} < 0.05 THEN NULL
                     ELSE {_parent_id(s, t, "customer", "i", n, plan.counts["customers"])}
                END AS customer_id,
                CASE WHEN {s.u(t, "has_order", "i")} < 0.6
                     THEN {_parent_id(s, t, "order", "i", n, plan.counts["orders"])}
                END AS order_id,
                {s.pick(t, "category", "i", _TICKET_CATEGORIES)} AS category,
                {s.pick(t, "priority", "i", _PRIORITIES)} AS priority,
                CASE WHEN {status} < 0.55 THEN 'solved'
                     WHEN {status} < 0.85 THEN 'closed'
                     WHEN {status} < 0.93 THEN 'pending'
                     ELSE 'open' END AS status,
                created_at,
                CASE WHEN {status} < 0.85
                     THEN created_at + to_hours({s.between(t, "resolve", "i", 1, 240)})
                END AS resolved_at,
                CASE WHEN {status} < 0.85 AND {s.u(t, "csat_null", "i")} < 0.6
                     THEN {s.between(t, "csat", "i", 1, 5)}::INTEGER
                END AS satisfaction_score
            FROM (
                SELECT i, {_event_time(plan, s, t, "i", n)} AS created_at
                FROM range(1, {n + 1}) r(i)
            )
        )
    """


_BUILDERS.update(
    {
        "customers": _customers_sql,
        "products": _products_sql,
        "orders": _orders_sql,
        "order_items": _order_items_sql,
        "payments": _payments_sql,
        "web_sessions": _sessions_sql,
        "marketing_campaigns": _campaigns_sql,
        "marketing_spend": _spend_sql,
        "support_tickets": _tickets_sql,
    }
)
