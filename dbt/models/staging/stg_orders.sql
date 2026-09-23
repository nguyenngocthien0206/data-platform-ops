select
    order_id,
    customer_id,
    ordered_at,
    cast(ordered_at as date) as order_date,
    status,
    channel,
    discount_code,
    discount_code is not null as has_discount,
    currency,
    _loaded_at
from {{ source('raw', 'orders') }}
