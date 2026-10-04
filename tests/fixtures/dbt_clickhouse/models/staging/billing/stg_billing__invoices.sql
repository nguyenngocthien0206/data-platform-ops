select invoice_id, order_id, issued_at, paid_at, {{ acme_shared.cents_to_amount('amount_cents') }} as amount
from {{ source('billing', 'invoices') }}
