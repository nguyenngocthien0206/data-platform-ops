-- Recognized revenue per order: the number the business reports externally.
-- A returned order recognizes nothing and its value moves to returned_revenue.
select
    order_id,
    ordered_at as recognized_at,
    order_date,
    status,
    channel,
    country,
    gross_amount,
    discount_amount,
    case when status = 'returned' then 0 else net_amount end as recognized_revenue,
    case when status = 'returned' then net_amount else 0 end as returned_revenue
from {{ ref('int_orders_enriched') }}
where status in ('completed', 'shipped', 'returned')
