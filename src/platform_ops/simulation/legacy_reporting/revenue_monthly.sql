-- Finance revenue dashboard: recognized revenue per month. A returned order
-- recognizes nothing; its value is reported as returned revenue.
CREATE VIEW {reporting}.revenue_monthly AS
SELECT
    date_trunc('month', ordered_at)::date AS revenue_month,
    count(*) AS orders,
    sum(gross_amount) AS gross_revenue,
    sum(discount_amount) AS discounts,
    sum(CASE WHEN status = 'returned' THEN 0 ELSE net_amount END) AS recognized_revenue,
    sum(CASE WHEN status = 'returned' THEN net_amount ELSE 0 END) AS returned_revenue
FROM {reporting}._orders
WHERE status IN ('completed', 'shipped', 'returned')
GROUP BY 1;
