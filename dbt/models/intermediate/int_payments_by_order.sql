select
    order_id,
    count(*) as payment_count,
    sum(amount) filter (where payment_status = 'succeeded') as paid_amount,
    sum(amount) filter (where payment_status = 'refunded') as refunded_amount,
    min(paid_at) as first_paid_at,
    max(paid_at) as last_paid_at,
    bool_or(is_late_arriving) as has_late_payment
from {{ ref('stg_payments') }}
group by order_id
