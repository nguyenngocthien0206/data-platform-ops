-- Open ticket backlog by category, from a support tooling experiment.
-- History: the experiment ended in 2025-08 without a decision. The tracking
-- ticket was closed; the model was left running.
select
    category,
    priority,
    count(*) as open_tickets
from {{ ref('stg_support_tickets') }}
where ticket_status in ('open', 'pending')
group by 1, 2
