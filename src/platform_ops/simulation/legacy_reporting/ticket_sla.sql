-- Support quality: tickets, SLA breaches and satisfaction per month, category
-- and priority. Resolution time counts the minute marks crossed, the same way
-- the new platform's date_diff does.
CREATE VIEW {reporting}.ticket_sla AS
WITH tickets AS (
    SELECT
        created_at,
        category,
        priority,
        satisfaction_score,
        CASE WHEN resolved_at IS NOT NULL THEN round(
            (extract(epoch FROM date_trunc('minute', resolved_at))
             - extract(epoch FROM date_trunc('minute', created_at))) / 60 / 60.0, 2)
        END AS resolution_hours,
        CASE priority WHEN 'urgent' THEN 4 WHEN 'high' THEN 24 WHEN 'normal' THEN 72 ELSE 168 END
            AS sla_hours
    FROM {app}.support_tickets
)
SELECT
    date_trunc('month', created_at)::date AS ticket_month,
    category,
    priority,
    count(*) AS tickets,
    count(resolution_hours) AS resolved_tickets,
    count(*) FILTER (WHERE resolution_hours > sla_hours) AS breached_tickets,
    round(avg(resolution_hours), 2) AS avg_resolution_hours,
    round(avg(satisfaction_score), 2) AS avg_satisfaction
FROM tickets
GROUP BY 1, 2, 3;
