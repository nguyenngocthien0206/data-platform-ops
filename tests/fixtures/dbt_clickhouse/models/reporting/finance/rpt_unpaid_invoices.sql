select invoice_id, order_id, issued_at, amount
from {{ ref('fct_invoices') }}
where paid_at is null
