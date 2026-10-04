select o.order_id, o.customer_id, o.status, o.amount, o.ordered_at,
       i.invoice_id, i.paid_at, i.paid_at is not null as is_paid
from {{ ref('stg_shop__orders') }} as o
left join {{ ref('stg_billing__invoices') }} as i on i.order_id = o.order_id
