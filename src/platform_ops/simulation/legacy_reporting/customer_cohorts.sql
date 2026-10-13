-- Customer insights: active customers per sign-up cohort and month since.
CREATE VIEW {reporting}.customer_cohorts AS
WITH cohorts AS (
    SELECT c.customer_id, date_trunc('month', c.signed_up_at)::date AS cohort_month
    FROM {app}.customers c
    JOIN (SELECT DISTINCT canonical_customer_id FROM {reporting}._customer_map) k
      ON k.canonical_customer_id = c.customer_id
),
activity AS (
    SELECT DISTINCT canonical_customer_id AS customer_id,
           date_trunc('month', ordered_at)::date AS order_month
    FROM {reporting}._orders
    WHERE status <> 'cancelled'
)
SELECT
    c.cohort_month,
    (extract(year FROM a.order_month) - extract(year FROM c.cohort_month)) * 12
        + extract(month FROM a.order_month) - extract(month FROM c.cohort_month)
        AS months_since_signup,
    count(*) AS active_customers
FROM cohorts c
JOIN activity a ON a.customer_id = c.customer_id
WHERE a.order_month >= c.cohort_month
GROUP BY 1, 2;
