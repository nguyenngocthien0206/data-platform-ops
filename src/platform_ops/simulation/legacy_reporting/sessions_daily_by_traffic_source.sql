-- Traffic sources: sessions per day and source.
CREATE VIEW {reporting}.sessions_daily_by_traffic_source AS
SELECT
    started_at::date AS period_start,
    coalesce(utm_source, 'direct') AS traffic_source,
    count(*) AS sessions,
    count(*) FILTER (WHERE converted) AS converted_sessions,
    sum(pageviews) AS pageviews
FROM {app}.web_sessions
GROUP BY 1, 2;
