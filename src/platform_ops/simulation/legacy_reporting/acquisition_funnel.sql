-- Acquisition overview: sign-ups and first orders per month and referral source.
CREATE VIEW {reporting}.acquisition_funnel AS
WITH people AS (
    SELECT c.customer_id, c.signed_up_at, coalesce(c.referral_source, 'unknown') AS referral_source
    FROM {app}.customers c
    JOIN (SELECT DISTINCT canonical_customer_id FROM {reporting}._customer_map) k
      ON k.canonical_customer_id = c.customer_id
),
signups AS (
    SELECT date_trunc('month', signed_up_at)::date AS month, referral_source, count(*) AS signups
    FROM people GROUP BY 1, 2
),
firsts AS (
    SELECT canonical_customer_id, min(ordered_at) AS first_order_at
    FROM {reporting}._orders
    WHERE status <> 'cancelled' AND canonical_customer_id IS NOT NULL
    GROUP BY canonical_customer_id
),
first_orders AS (
    SELECT date_trunc('month', f.first_order_at)::date AS month, p.referral_source,
           count(*) AS first_orders
    FROM firsts f
    JOIN people p ON p.customer_id = f.canonical_customer_id
    GROUP BY 1, 2
)
SELECT
    coalesce(s.month, f.month) AS month,
    coalesce(s.referral_source, f.referral_source) AS referral_source,
    coalesce(s.signups, 0) AS signups,
    coalesce(f.first_orders, 0) AS first_orders
FROM signups s
FULL OUTER JOIN first_orders f ON f.month = s.month AND f.referral_source = s.referral_source;
