select
    ticket_id,
    customer_id,
    order_id,
    category,
    priority,
    status as ticket_status,
    created_at,
    resolved_at,
    satisfaction_score,
    _loaded_at
from {{ source('raw', 'support_tickets') }}
