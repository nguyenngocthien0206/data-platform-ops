select
    product_id,
    product_name,
    category,
    unit_price,
    unit_cost,
    is_active,
    created_at,
    _loaded_at
from {{ source('raw', 'products') }}
