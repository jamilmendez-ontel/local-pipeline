-- 236_schedule_feed_audit.sql
-- Registry of Swift schedule disagreements: task-record value vs the task's own
-- activity feed (Firebase). Root cause (investigated 2026-08-14): Swift's
-- server-side calendar scheduling path applies its 12h noon->midnight
-- normalization (meant for date-only schedules) to timed schedules too, so the
-- task record lands 12h early while the activity feed keeps the correct
-- instant. The feed is therefore the source of truth when the two disagree.
--
-- Populated by swift_api_pipeline/schedule_feed_audit.py (full + incremental).
-- Serving layer: analytics.v_user_priorities_effective exposes
-- scheduled_effective = feed value while a timed_mismatch is open, NULL for an
-- open ghost_schedule (feed says the schedule was removed), else the raw
-- stored value. data_staging.stg_user_priorities is NEVER mutated: it stays a
-- faithful mirror of Swift's task records, and the 5-minute full-refresh
-- transform would wipe in-table corrections anyway. Corrections auto-resolve
-- when task and feed agree again (e.g. after Swift fixes/backfills), so the
-- overlay cannot double-shift.

BEGIN;

CREATE TABLE pipeline.schedule_audit_runs (
    run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mode              text NOT NULL CHECK (mode IN ('full', 'incremental')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'ok', 'failed')),
    tasks_checked     integer,
    feeds_fetched     integer,
    coverage_pct      numeric,
    new_anomalies     integer,
    resolved_anomalies integer,
    open_anomalies    integer,
    error             text
);

CREATE TABLE pipeline.schedule_audit_anomalies (
    task_did        text PRIMARY KEY,
    class           text NOT NULL
                    CHECK (class IN ('timed_mismatch', 'ghost_schedule', 'no_feed_schedule')),
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    stored_scheduled timestamptz,
    feed_scheduled  timestamptz,
    offset_hours    numeric,
    last_feed_event text,
    last_event_at   timestamptz,
    last_event_by   text,
    task_name       text,
    asset_name      text,
    project         text,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
);

CREATE INDEX idx_schedule_audit_anomalies_open
    ON pipeline.schedule_audit_anomalies (status)
    WHERE status = 'open';

ALTER TABLE pipeline.schedule_audit_runs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline.schedule_audit_anomalies ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON pipeline.schedule_audit_runs      FROM anon, authenticated;
REVOKE ALL ON pipeline.schedule_audit_anomalies FROM anon, authenticated;

CREATE OR REPLACE VIEW analytics.v_user_priorities_effective AS
SELECT
    p.*,
    a.class                          AS schedule_anomaly_class,
    a.feed_scheduled                 AS schedule_feed_value,
    (a.task_did IS NOT NULL)         AS schedule_anomaly_open,
    CASE
        WHEN a.class = 'ghost_schedule' THEN NULL
        WHEN a.class = 'timed_mismatch' AND a.feed_scheduled IS NOT NULL
            THEN a.feed_scheduled
        ELSE p.scheduled
    END                              AS scheduled_effective
FROM data_staging.stg_user_priorities p
LEFT JOIN pipeline.schedule_audit_anomalies a
       ON a.task_did = p.task_did AND a.status = 'open';

COMMENT ON TABLE pipeline.schedule_audit_anomalies IS
    'Open/resolved disagreements between Swift task-record schedules and the task''s activity feed. Feed wins. Written only by schedule_feed_audit.py.';
COMMENT ON VIEW analytics.v_user_priorities_effective IS
    'stg_user_priorities plus scheduled_effective: feed-corrected schedule while a Swift 12h-flip anomaly is open. Consumers should read this instead of stg when schedule accuracy matters.';

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, data_notes)
VALUES
    ('pipeline', 'schedule_audit_runs',
     'Run log of the Swift schedule-vs-activity-feed audit job.',
     'Operational: one row per full/incremental audit run with coverage and anomaly counts.',
     'status=failed rows indicate the audit could not verify schedules; treat data as unverified for that window.'),
    ('pipeline', 'schedule_audit_anomalies',
     'Registry of Swift schedules whose task record disagrees with the task''s own activity feed (Swift 12h calendar-path bug, found 2026-08-14).',
     'Feed is source of truth: timed_mismatch = task stored 12h early; ghost_schedule = feed says schedule removed but task still scheduled; no_feed_schedule = schedule with no feed record.',
     'PK task_did. Auto-resolves when task and feed agree again. Never hand-edit; schedule_feed_audit.py owns it.'),
    ('analytics', 'v_user_priorities_effective',
     'User priorities with scheduled_effective: the activity-feed-corrected schedule when a Swift schedule anomaly is open.',
     'Use scheduled_effective instead of scheduled for due-time logic; schedule_anomaly_open flags corrected rows.',
     'Ghost schedules surface scheduled_effective = NULL while raw scheduled stays visible.');

COMMIT;
