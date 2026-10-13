-- Margin report: net revenue, cost and margin per month and channel.
CREATE VIEW {reporting}.margin_monthly_by_channel AS
SELECT
    date_trunc('month', ordered_at)::date AS period_start,
    channel,
    sum(net_amount) AS net_revenue,
    sum(cost_amount) AS cost,
    sum(margin_amount) AS margin
FROM {reporting}._orders
WHERE status IN ('completed', 'shipped')
GROUP BY 1, 2;
