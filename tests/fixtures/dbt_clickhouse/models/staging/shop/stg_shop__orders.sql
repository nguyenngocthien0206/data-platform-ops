select order_id, customer_id, status, {{ acme_shared.cents_to_amount('amount_cents') }} as amount, ordered_at
from {{ source('shop', 'orders') }}
