select
    order_id,
    ordered_at,
    order_date,
    channel,
    country,
    net_amount,
    cost_amount,
    margin_amount,
    round(margin_amount / nullif(net_amount, 0), 4) as margin_pct
from {{ ref('int_orders_enriched') }}
where status in ('completed', 'shipped')
