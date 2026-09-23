-- The order fact every sales number is built from. Customer is the canonical
-- person, so revenue per customer is not split across duplicate accounts.
select
    order_id,
    canonical_customer_id as customer_id,
    ordered_at,
    order_date,
    status,
    channel,
    country,
    discount_code,
    has_discount,
    item_lines,
    units,
    gross_amount,
    discount_amount,
    net_amount
from {{ ref('int_orders_enriched') }}
