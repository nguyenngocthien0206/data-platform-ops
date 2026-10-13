-- Cash reconciliation: what each order was paid, refunded, and still owes.
-- A payment is late when it was written more than a day after it happened.
CREATE VIEW {reporting}.payments_reconciliation AS
WITH payments AS (
    SELECT
        order_id,
        count(*) AS payment_count,
        sum(amount) FILTER (WHERE status = 'succeeded') AS paid_amount,
        sum(amount) FILTER (WHERE status = 'refunded') AS refunded_amount,
        bool_or(updated_at > created_at + interval '1 day') AS has_late_payment
    FROM {app}.payments
    GROUP BY order_id
),
statused AS (
    SELECT
        o.order_id,
        o.ordered_at,
        o.order_date,
        o.status AS order_status,
        o.channel,
        o.gross_amount,
        coalesce(p.paid_amount, 0) AS paid_amount,
        coalesce(p.refunded_amount, 0) AS refunded_amount,
        coalesce(p.payment_count, 0) AS payment_count,
        coalesce(p.has_late_payment, false) AS has_late_payment,
        CASE
            WHEN o.status IN ('pending', 'cancelled') THEN 'not_due'
            WHEN o.status = 'returned' AND p.refunded_amount IS NOT NULL THEN 'refunded'
            WHEN p.order_id IS NULL THEN 'missing'
            WHEN coalesce(p.paid_amount, 0) >= o.gross_amount - 0.01 THEN 'paid'
            ELSE 'underpaid'
        END AS payment_status
    FROM {reporting}._orders o
    LEFT JOIN payments p ON p.order_id = o.order_id
)
SELECT
    *,
    CASE WHEN payment_status IN ('missing', 'underpaid') THEN gross_amount - paid_amount ELSE 0 END
        AS outstanding_amount
FROM statused;
