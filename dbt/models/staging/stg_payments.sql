-- A payment is "late arriving" when the record reached the warehouse more than
-- a day after the payment happened. Finance reconciliation has to tolerate it.
select
    payment_id,
    order_id,
    payment_method,
    status as payment_status,
    amount,
    created_at as paid_at,
    _loaded_at,
    _loaded_at > created_at + interval 1 day as is_late_arriving
from {{ source('raw', 'payments') }}
