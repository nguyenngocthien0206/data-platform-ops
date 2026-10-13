-- Campaign ROI: spend, traffic and last-touch attributed orders per campaign.
-- An order is credited to the campaign of the customer's latest campaign
-- session in the seven days before it.
CREATE VIEW {reporting}.campaign_performance AS
WITH sessions AS (
    SELECT s.session_id, s.started_at, s.campaign_id, m.canonical_customer_id
    FROM {app}.web_sessions s
    LEFT JOIN {reporting}._customer_map m ON m.customer_id = s.customer_id
    WHERE s.campaign_id IS NOT NULL
),
touches AS (
    SELECT
        o.order_id,
        o.net_amount,
        s.campaign_id,
        row_number() OVER (
            PARTITION BY o.order_id ORDER BY s.started_at DESC, s.session_id DESC
        ) AS touch_rank
    FROM {reporting}._orders o
    JOIN sessions s
      ON s.canonical_customer_id = o.canonical_customer_id
     AND s.started_at <= o.ordered_at
     AND s.started_at > o.ordered_at - interval '7 days'
    WHERE o.status IN ('completed', 'shipped')
),
attributed AS (
    SELECT campaign_id, count(*) AS orders, sum(net_amount) AS revenue
    FROM touches WHERE touch_rank = 1 GROUP BY campaign_id
),
spend AS (
    SELECT campaign_id, sum(spend) AS spend, sum(impressions) AS impressions,
           sum(clicks) AS clicks
    FROM {app}.marketing_spend GROUP BY campaign_id
),
traffic AS (
    SELECT campaign_id, count(*) AS sessions FROM sessions GROUP BY campaign_id
)
SELECT
    c.campaign_id,
    c.campaign_name,
    c.channel,
    c.start_date,
    c.end_date,
    c.budget,
    coalesce(sp.spend, 0) AS spend,
    coalesce(sp.impressions, 0) AS impressions,
    coalesce(sp.clicks, 0) AS clicks,
    coalesce(t.sessions, 0) AS sessions,
    coalesce(a.orders, 0) AS attributed_orders,
    coalesce(a.revenue, 0) AS attributed_revenue,
    round(a.revenue / nullif(sp.spend, 0), 4) AS return_on_ad_spend
FROM {app}.marketing_campaigns c
LEFT JOIN spend sp ON sp.campaign_id = c.campaign_id
LEFT JOIN traffic t ON t.campaign_id = c.campaign_id
LEFT JOIN attributed a ON a.campaign_id = c.campaign_id;
