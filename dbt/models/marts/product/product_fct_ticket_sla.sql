select
    cast(date_trunc('month', created_at) as date) as ticket_month,
    category,
    priority,
    count(*) as tickets,
    count(resolution_hours) as resolved_tickets,
    count(*) filter (where breached_sla) as breached_tickets,
    round(avg(resolution_hours), 2) as avg_resolution_hours,
    round(avg(satisfaction_score), 2) as avg_satisfaction
from {{ ref('product_fct_support_tickets') }}
group by 1, 2, 3
