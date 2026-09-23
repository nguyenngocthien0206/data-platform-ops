-- Segments are computed as of the latest order in the data, never as of the
-- wall clock, so a rebuild on another day gives the same answer.
with reference as (
    select max(last_order_at) as as_of from {{ ref('int_customer_orders') }}
)

select
    o.canonical_customer_id,
    coalesce(o.lifetime_revenue, 0) as lifetime_revenue,
    o.order_count,
    case
        when coalesce(o.lifetime_revenue, 0) >= 2000 then 'vip'
        when coalesce(o.lifetime_revenue, 0) >= 500 then 'regular'
        else 'occasional'
    end as value_segment,
    o.last_order_at < r.as_of - interval 90 day as is_lapsed
from {{ ref('int_customer_orders') }} o
cross join reference r
