-- The single shared definition of the money on an order. Any discount code
-- takes a flat 10% off the goods total.
with priced as (
    select
        o.order_id,
        o.customer_id,
        m.canonical_customer_id,
        o.ordered_at,
        o.order_date,
        o.status,
        o.channel,
        o.discount_code,
        o.has_discount,
        t.item_lines,
        t.units,
        t.gross_amount,
        t.cost_amount,
        case when o.has_discount then round(t.gross_amount * 0.10, 2) else 0 end
            as discount_amount
    from {{ ref('stg_orders') }} o
    join {{ ref('int_order_totals') }} t using (order_id)
    left join {{ ref('int_customer_id_map') }} m on m.customer_id = o.customer_id
)

select
    p.*,
    c.country,
    p.gross_amount - p.discount_amount as net_amount,
    p.gross_amount - p.discount_amount - p.cost_amount as margin_amount
from priced p
left join {{ ref('stg_customers') }} c on c.customer_id = p.canonical_customer_id
