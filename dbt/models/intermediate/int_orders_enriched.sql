-- The single shared definition of the money on an order. Any discount code
-- takes a flat 10% off the goods total.
{{ config(order_by='order_id') }}
with priced as (
    select
        o.order_id as order_id,
        o.customer_id as customer_id,
        m.canonical_customer_id as canonical_customer_id,
        o.ordered_at as ordered_at,
        o.order_date as order_date,
        o.status as status,
        o.channel as channel,
        o.discount_code as discount_code,
        o.has_discount as has_discount,
        t.item_lines as item_lines,
        t.units as units,
        t.gross_amount as gross_amount,
        t.cost_amount as cost_amount,
        -- A decimal rate keeps money in Decimal: a float here would make every
        -- sum downstream depend on the order ClickHouse adds the rows in.
        case when o.has_discount then round(t.gross_amount * cast(0.10 as decimal(4, 2)), 2) else 0 end
            as discount_amount
    from {{ ref('stg_orders') }} o
    join {{ ref('int_order_totals') }} t using (order_id)
    left join {{ ref('int_customer_id_map') }} m on m.customer_id = o.customer_id
)

select
    p.*,
    c.country as country,
    p.gross_amount - p.discount_amount as net_amount,
    p.gross_amount - p.discount_amount - p.cost_amount as margin_amount
from priced p
left join {{ ref('stg_customers') }} c on c.customer_id = p.canonical_customer_id
