-- Sales operations monitor: orders per day and channel.
CREATE VIEW {reporting}.orders_daily_by_channel AS
SELECT
    order_date AS period_start,
    channel,
    count(*) AS orders,
    count(DISTINCT canonical_customer_id) AS customers,
    sum(net_amount) AS net_revenue,
    round(avg(net_amount), 2) AS avg_order_value
FROM {reporting}._orders
GROUP BY 1, 2;
