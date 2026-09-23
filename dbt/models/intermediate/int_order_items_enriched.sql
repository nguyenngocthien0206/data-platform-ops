select
    i.order_item_id,
    i.order_id,
    o.ordered_at,
    o.status as order_status,
    i.line_number,
    i.product_id,
    p.product_name,
    p.category,
    i.quantity,
    i.unit_price,
    i.line_amount,
    i.quantity * p.unit_cost as line_cost,
    i.line_amount - i.quantity * p.unit_cost as line_margin
from {{ ref('stg_order_items') }} i
join {{ ref('stg_orders') }} o using (order_id)
join {{ ref('stg_products') }} p using (product_id)
