select
    order_date,
    count(*) as orders,
    count(distinct customer_id) as customers,
    sum(net_amount) filter (where status in ('completed', 'shipped')) as net_revenue,
    round(avg(net_amount), 2) as avg_order_value,
    count(*) filter (where status = 'cancelled') as cancelled_orders
from {{ ref('sales_fct_orders') }}
group by order_date
