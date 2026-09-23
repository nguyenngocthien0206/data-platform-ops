-- Service level targets by priority. An open ticket has no verdict yet rather
-- than counting as a breach, so the numbers do not shift with the clock.
with targets as (
    select
        *,
        case priority
            when 'urgent' then 4
            when 'high' then 24
            when 'normal' then 72
            else 168
        end as sla_hours
    from {{ ref('int_tickets_enriched') }}
)

select
    ticket_id,
    created_at,
    category,
    priority,
    ticket_status,
    order_channel,
    resolution_hours,
    satisfaction_score,
    sla_hours,
    case when resolution_hours is not null then resolution_hours > sla_hours end
        as breached_sla
from targets
