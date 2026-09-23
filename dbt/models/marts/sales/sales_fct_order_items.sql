select
    order_item_id,
    order_id,
    ordered_at,
    order_status,
    product_id,
    product_name,
    category,
    quantity,
    unit_price,
    line_amount,
    line_margin
from {{ ref('int_order_items_enriched') }}
