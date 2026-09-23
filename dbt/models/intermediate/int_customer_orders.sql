select
    canonical_customer_id,
    min(ordered_at) as first_order_at,
    max(ordered_at) as last_order_at,
    count(*) as order_count,
    sum(net_amount) filter (where status in ('completed', 'shipped')) as lifetime_revenue
from {{ ref('int_orders_enriched') }}
where status <> 'cancelled'
  and canonical_customer_id is not null
group by canonical_customer_id
