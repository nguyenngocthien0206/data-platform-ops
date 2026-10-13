-- Product engagement: sessions and conversions per month and device.
CREATE VIEW {reporting}.engagement_monthly_by_device AS
SELECT
    date_trunc('month', started_at)::date AS period_start,
    device,
    count(*) AS sessions,
    count(*) FILTER (WHERE converted) AS converted_sessions
FROM {app}.web_sessions
GROUP BY 1, 2;
