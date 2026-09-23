-- Payment reconciliation snapshot for the Q3 2025 quarter close.
-- History: built by a contractor during the close. The contract ended and the
-- close process moved to finance_fct_payments_reconciliation.
select
    order_status,
    payment_status,
    count(*) as orders,
    sum(gross_amount) as gross_amount,
    sum(paid_amount) as paid_amount
from {{ ref('int_order_payment_status') }}
where ordered_at >= timestamp '2025-07-01'
  and ordered_at < timestamp '2025-10-01'
group by 1, 2
