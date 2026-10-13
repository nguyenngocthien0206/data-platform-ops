-- Sales executive dashboard: one row per order day.
CREATE VIEW {reporting}.sales_daily AS
SELECT
    order_date,
    count(*) AS orders,
    count(DISTINCT canonical_customer_id) AS customers,
    sum(net_amount) FILTER (WHERE status IN ('completed', 'shipped')) AS net_revenue,
    round(avg(net_amount), 2) AS avg_order_value,
    count(*) FILTER (WHERE status = 'cancelled') AS cancelled_orders
FROM {reporting}._orders
GROUP BY order_date;
