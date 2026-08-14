-- 237_schedule_effective_reschedule_guard.sql
-- Guard v_user_priorities_effective against reschedules that happen between
-- audit runs: a correction only applies while stg.scheduled still equals the
-- exact value recorded when the anomaly was detected (stored_scheduled). The
-- moment the task's schedule changes (e.g. someone fixes it via the
-- Reschedule dialog), the view immediately falls back to the live stored
-- value instead of overriding with a stale feed value; the next audit run
-- then resolves or re-opens the anomaly. schedule_anomaly_stale exposes this
-- in-between state.

-- Adds schedule_anomaly_stale mid-column-list, so the view must be dropped
-- and recreated (CREATE OR REPLACE cannot reorder/insert columns). Safe:
-- view introduced minutes earlier in 236, no dependents yet.
DROP VIEW analytics.v_user_priorities_effective;

CREATE VIEW analytics.v_user_priorities_effective AS
SELECT
    p.*,
    a.class                          AS schedule_anomaly_class,
    a.feed_scheduled                 AS schedule_feed_value,
    (a.task_did IS NOT NULL)         AS schedule_anomaly_open,
    (a.task_did IS NOT NULL
     AND p.scheduled IS DISTINCT FROM a.stored_scheduled)
                                     AS schedule_anomaly_stale,
    CASE
        WHEN a.task_did IS NULL THEN p.scheduled
        -- schedule changed since detection (reschedule in Swift):
        -- trust the live value until the audit re-checks the feed
        WHEN p.scheduled IS DISTINCT FROM a.stored_scheduled THEN p.scheduled
        WHEN a.class = 'ghost_schedule' THEN NULL
        WHEN a.class = 'timed_mismatch' AND a.feed_scheduled IS NOT NULL
            THEN a.feed_scheduled
        ELSE p.scheduled
    END                              AS scheduled_effective
FROM data_staging.stg_user_priorities p
LEFT JOIN pipeline.schedule_audit_anomalies a
       ON a.task_did = p.task_did AND a.status = 'open';

COMMENT ON VIEW analytics.v_user_priorities_effective IS
    'stg_user_priorities plus scheduled_effective: feed-corrected schedule while a Swift 12h-flip anomaly is open AND the stored value is unchanged since detection. schedule_anomaly_stale = schedule touched since detection, awaiting audit re-check.';
