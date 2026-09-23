-- Weekly orders for the old Monday leadership email.
-- History: the email moved to the sales executive dashboard in 2025-05, which
-- reads the generated weekly rollups instead. The email job was switched off;
-- the model was not.
select
    cast(date_trunc('week', ordered_at) as date) as order_week,
    count(*) as orders,
    sum(net_amount) as net_revenue
from {{ ref('int_orders_enriched') }}
where status <> 'cancelled'
group by 1
