-- Does every order that should have been paid have its payment? Payments cover
-- the goods total, so they are compared with the gross amount, not the net.
select
    o.order_id,
    o.ordered_at,
    o.order_date,
    o.status as order_status,
    o.channel,
    o.gross_amount,
    o.net_amount,
    coalesce(p.paid_amount, 0) as paid_amount,
    coalesce(p.refunded_amount, 0) as refunded_amount,
    coalesce(p.payment_count, 0) as payment_count,
    coalesce(p.has_late_payment, false) as has_late_payment,
    case
        when o.status in ('pending', 'cancelled') then 'not_due'
        when o.status = 'returned' and p.refunded_amount is not null then 'refunded'
        when p.order_id is null then 'missing'
        when coalesce(p.paid_amount, 0) >= o.gross_amount - 0.01 then 'paid'
        else 'underpaid'
    end as payment_status
from {{ ref('int_orders_enriched') }} o
left join {{ ref('int_payments_by_order') }} p using (order_id)
