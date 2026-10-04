select order_id, customer_id, status, amount, ordered_at, toDate(ordered_at) as order_date, is_paid
from {{ ref('int_order_payments') }}
