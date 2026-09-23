select
    order_item_id,
    order_id,
    line_number,
    product_id,
    quantity,
    unit_price,
    quantity * unit_price as line_amount,
    _loaded_at
from {{ source('raw', 'order_items') }}
