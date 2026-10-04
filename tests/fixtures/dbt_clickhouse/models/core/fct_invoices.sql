select invoice_id, order_id, issued_at, paid_at, amount
from {{ ref('stg_billing__invoices') }}
