{{ config(order_by='(order_month, channel)') }}
select
    cast(date_trunc('month', ordered_at) as date) as order_month,
    channel,
    count(*) as orders,
    sum(net_amount) filter (where status in ('completed', 'shipped')) as net_revenue,
    {{ avg_half_up('net_amount') }} as avg_order_value,
    round(count(*) filter (where has_discount) / count(*), 4) as discount_share,
    round(count(*) filter (where status = 'returned') / count(*), 4) as return_rate
from {{ ref('sales_fct_orders') }}
group by 1, 2
