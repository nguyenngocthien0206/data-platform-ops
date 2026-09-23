-- Payment method mix, built for the 2025 payment provider negotiation.
-- History: the negotiation finished in 2025-09. The analysis is in a slide deck
-- somewhere; the model that fed it still builds every night.
select
    payment_method,
    count(*) as payments,
    sum(amount) as amount
from {{ ref('stg_payments') }}
group by payment_method
