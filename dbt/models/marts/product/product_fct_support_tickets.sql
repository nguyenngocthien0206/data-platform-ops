select
    ticket_id,
    created_at,
    category,
    priority,
    ticket_status,
    order_channel,
    resolution_hours,
    sla_hours,
    breached_sla,
    satisfaction_score
from {{ ref('int_ticket_resolution') }}
