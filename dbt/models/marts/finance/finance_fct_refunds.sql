select
    order_id,
    ordered_at,
    order_date,
    channel,
    net_amount,
    refunded_amount,
    payment_status = 'missing' as refund_missing
from {{ ref('int_order_payment_status') }}
where order_status = 'returned'
