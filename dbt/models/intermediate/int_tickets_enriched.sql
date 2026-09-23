select
    t.ticket_id,
    t.customer_id,
    m.canonical_customer_id,
    t.order_id,
    o.channel as order_channel,
    t.category,
    t.priority,
    t.ticket_status,
    t.created_at,
    t.resolved_at,
    t.satisfaction_score,
    case
        when t.resolved_at is not null
            then round(date_diff('minute', t.created_at, t.resolved_at) / 60.0, 2)
    end as resolution_hours
from {{ ref('stg_support_tickets') }} t
left join {{ ref('int_customer_id_map') }} m on m.customer_id = t.customer_id
left join {{ ref('stg_orders') }} o on o.order_id = t.order_id
