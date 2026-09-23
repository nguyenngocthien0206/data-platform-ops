-- Order-to-cash reconciliation. "missing" means an order that should have been
-- paid has no payment record yet: sometimes a late-arriving payment, sometimes
-- a real gap that someone in finance has to chase.
select
    order_id,
    ordered_at,
    order_date,
    order_status,
    channel,
    gross_amount,
    paid_amount,
    refunded_amount,
    payment_count,
    has_late_payment,
    payment_status,
    case when payment_status in ('missing', 'underpaid')
         then gross_amount - paid_amount else 0 end as outstanding_amount
from {{ ref('int_order_payment_status') }}
