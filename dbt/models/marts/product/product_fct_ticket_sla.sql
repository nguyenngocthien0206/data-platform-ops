{{ config(order_by='(ticket_month, category, priority)') }}
select
    cast(date_trunc('month', created_at) as date) as ticket_month,
    category,
    priority,
    count(*) as tickets,
    count(resolution_hours) as resolved_tickets,
    count(*) filter (where breached_sla) as breached_tickets,
    {{ avg_half_up('resolution_hours') }} as avg_resolution_hours,
    {{ avg_half_up('satisfaction_score') }} as avg_satisfaction
from {{ ref('product_fct_support_tickets') }}
group by 1, 2, 3
