-- 243_weekly_pmi_tracker.sql
-- Weekly PMI Completion Report: tracker tables + analytics views.
-- Replaces Sheena's SharePoint "PMI data" workbook + Power BI loop. Spec:
-- report-automation/docs/superpowers/specs/2026-08-26-weekly-pmi-report-design.md (section 4).
--
-- Populated by report-automation/weekly-pmi-report/src/loader.py (live email loads, one
-- transaction per load: file upsert -> raw rows -> staging replace per source_kind) and
-- src/backfill_tracker.py (one-off tracker backfill, trigger_kind = 'backfill').
-- Loader order: run row -> file rows -> raw rows -> staging rows -> run status.
--
-- Objects:
--   reference.ref_pmi_clusters           market/cluster registry + per-page Power BI filter
--                                        settings; self-registering (ref_qa_forms precedent, 231)
--   pipeline.pmi_report_runs             one row per load/render/send run (dashboard reads it)
--   data_raw.raw_pmi_tracker_files       one row per received xlsx (or tracker backfill group);
--                                        upsert key (market_code, tracker_sent, kind, sha256)
--   data_raw.raw_pmi_tracker_rows        replay source, payload jsonb (header -> cell text)
--   data_staging.stg_pmi_tracker_sites   one row per site per tracker date per cluster per kind
--   analytics.v_pmi_*                    10 views, grain (cluster, tracker_sent)
--
-- Category rule in every view: NULL or '' in status / status_bucket / sent_to_cm /
-- general_contractor / cm_name / cm_company is emitted as the literal '(Blank)' (sort 99).
-- Label views group on lower(trim(label)) and display the most frequent spelling; ties
-- break on the spelling COLLATE "C" (ordinal, same rule as the Python aggregate.merge_labels).
-- Week rule (spec 6.3): Tuesday-start weeks, week 1 = the Tue-Mon week containing Jan 1.
-- Swift join (v_pmi_sites): Verizon scope is the asset path (asset_id ~ '/VZW/'); the FUZE id
-- is the 6-9 digit path segment of asset_id. carrier_group and asset_identifier are NULL on
-- every live stg_assets row (verified 2026-08-26) and are deliberately not used.
-- pmi_report_runs.status also allows 'running' (default): the run row must exist before file
-- rows can FK to it (spec 4.5).
--
-- Rollback:
--   DROP VIEW IF EXISTS analytics.v_pmi_sites, analytics.v_pmi_gc_aging_history,
--     analytics.v_pmi_status_flow, analytics.v_pmi_cx_vs_pmi_weekly, analytics.v_pmi_gc_aging,
--     analytics.v_pmi_cm_company_status, analytics.v_pmi_cm_status, analytics.v_pmi_sent_by_tracker,
--     analytics.v_pmi_status_by_tracker, analytics.v_pmi_tracker_dates;
--   DROP TABLE IF EXISTS data_staging.stg_pmi_tracker_sites, data_raw.raw_pmi_tracker_rows,
--     data_raw.raw_pmi_tracker_files, pipeline.pmi_report_runs, reference.ref_pmi_clusters;
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL AND (
--     (schema_name = 'reference' AND table_name = 'ref_pmi_clusters') OR
--     (schema_name = 'pipeline' AND table_name = 'pmi_report_runs') OR
--     (schema_name = 'data_raw' AND table_name IN ('raw_pmi_tracker_files', 'raw_pmi_tracker_rows')) OR
--     (schema_name = 'data_staging' AND table_name = 'stg_pmi_tracker_sites') OR
--     (schema_name = 'analytics' AND table_name LIKE 'v\_pmi\_%'));
--
-- APPLIED + VERIFIED 2026-08-26 06:17 ET against voqfjfngdpcvevbkikud via psycopg2 over the
-- Supabase session pooler (whole file as one autocommit simple query; the file's own
-- BEGIN/COMMIT made it atomic) (weekly PMI plan Task 6). Pre-flight 2a returned 0 rows,
-- 2b 8 rows (id, asset_did, asset_id, asset_status; asset_did, task_name, task_status,
-- task_approved_on; carrier_group/asset_identifier dropped per amendment 20), 2c 7 rows,
-- 2d both task names (36,078 / 36,077 rows), 2e asset_id ~ '/VZW/' = 25,826 rows of which
-- 25,768 carry a 6-9 digit FUZE segment, 2f service_role USAGE true on all five schemas,
-- 2g 0 pre-existing metadata rows. Post-apply: 5a total=10 active=4 mp=1 cgc_statuses=3
-- sova_tes=TES; 5b svc_select=t svc_insert=t anon_select=f auth_select=f; 5c 5 x true;
-- 5d convalidated=f; 5e ten zeros; 5f Hash Left Join, Hash Cond (s.fuze_project_id =
-- w.fuze_project_id), Unique node over stg_assets (Filter asset_id ~ '/VZW/'), total cost
-- 278294; 5g 15; 5h all ten views svc=t anon=f auth=f; comments present on all 15 objects.
-- Note: stg_asset_tasks.task_approved_on is already date, so the ::date casts in
-- v_pmi_sites are no-ops.

BEGIN;

-- =============================================================================
-- 1. Tables
-- =============================================================================

CREATE TABLE reference.ref_pmi_clusters (
    market_code                text PRIMARY KEY,
    cluster                    text NOT NULL UNIQUE,
    report_title               text NOT NULL DEFAULT 'Weekly PMI Completion Report',
    subtitle                   text NOT NULL,
    page_variant               text NOT NULL CHECK (page_variant IN ('standard', 'mp')),
    show_completion_line       boolean NOT NULL DEFAULT true,
    gc_aging_statuses          text[] NOT NULL DEFAULT ARRAY['No - Pending Items', 'No - Failing PMI']::text[],
    gc_aging_exclude_notes     text[] NOT NULL DEFAULT ARRAY[]::text[],
    gc_aging_exclude_blank_gc  boolean NOT NULL DEFAULT false,
    tes_filter_ma_conducted_by text,
    active                     boolean NOT NULL DEFAULT true,
    recipients                 text[] NOT NULL DEFAULT ARRAY[]::text[],
    registered_by              text NOT NULL DEFAULT 'seed',
    auto_registered            boolean NOT NULL DEFAULT false,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pipeline.pmi_report_runs (
    run_id            uuid PRIMARY KEY,
    market_code       text NOT NULL,
    tracker_sent      date NOT NULL,
    trigger_kind      text NOT NULL CHECK (trigger_kind IN ('dispatch', 'manual', 'backfill')),
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'loaded', 'rendered', 'sent', 'rejected', 'failed')),
    anomalies         jsonb NOT NULL DEFAULT '[]'::jsonb,
    pdf_drive_file_id text,
    email_message_id  text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    error             text
);

CREATE TABLE data_raw.raw_pmi_tracker_files (
    file_id          uuid PRIMARY KEY,
    market_code      text NOT NULL,
    tracker_sent     date NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('pending', 'failing', 'tracker')),
    file_name        text NOT NULL,
    sha256           text NOT NULL,
    drive_file_id    text,
    gmail_message_id text,
    row_count        integer,
    received_at      timestamptz NOT NULL DEFAULT now(),
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    run_id           uuid REFERENCES pipeline.pmi_report_runs(run_id),
    CONSTRAINT uq_raw_pmi_tracker_files_key UNIQUE (market_code, tracker_sent, kind, sha256)
);

CREATE TABLE data_raw.raw_pmi_tracker_rows (
    file_id uuid NOT NULL REFERENCES data_raw.raw_pmi_tracker_files(file_id) ON DELETE CASCADE,
    row_no  integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (file_id, row_no)
);

CREATE TABLE data_staging.stg_pmi_tracker_sites (
    cluster               text NOT NULL,
    tracker_sent          date NOT NULL,
    source_kind           text NOT NULL CHECK (source_kind IN ('pending', 'failing')),
    fuze_project_id       bigint NOT NULL,
    site_name             text NOT NULL DEFAULT '',
    general_contractor    text NOT NULL DEFAULT '',
    status_raw            text,
    status                text,
    status_bucket         text,
    status_order          smallint,
    sent_to_cm            text,
    ontel_notes           text,
    cm_company            text NOT NULL DEFAULT '',
    cm_name               text NOT NULL DEFAULT '',
    antenna_mount_company text,
    verizon_cm            text,
    ma_conducted_by       text,
    days_gc_submits       integer,
    days_ontel_completes  integer,
    cx_date               date,
    days_since_cx         integer,
    duration_bucket       text CHECK (duration_bucket IN ('>=100', '51-99', '0-50')),
    cx_week_no            smallint,
    cx_week_start         date,
    source_file_id        uuid REFERENCES data_raw.raw_pmi_tracker_files(file_id),
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    run_id                uuid REFERENCES pipeline.pmi_report_runs(run_id),
    PRIMARY KEY (cluster, tracker_sent, source_kind, fuze_project_id, site_name, general_contractor)
);

ALTER TABLE data_staging.stg_pmi_tracker_sites
    ADD CONSTRAINT fk_stg_pmi_tracker_sites_cluster
    FOREIGN KEY (cluster) REFERENCES reference.ref_pmi_clusters(cluster) NOT VALID;

CREATE INDEX idx_stg_pmi_tracker_sites_fuze
    ON data_staging.stg_pmi_tracker_sites (fuze_project_id);

-- RLS + grants (pipeline schema has no default ACL: service_role grant is explicit everywhere)
ALTER TABLE reference.ref_pmi_clusters          ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline.pmi_report_runs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_pmi_tracker_files      ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_pmi_tracker_rows       ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_pmi_tracker_sites  ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON reference.ref_pmi_clusters          FROM anon, authenticated;
REVOKE ALL ON pipeline.pmi_report_runs            FROM anon, authenticated;
REVOKE ALL ON data_raw.raw_pmi_tracker_files      FROM anon, authenticated;
REVOKE ALL ON data_raw.raw_pmi_tracker_rows       FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_pmi_tracker_sites  FROM anon, authenticated;
GRANT ALL ON reference.ref_pmi_clusters          TO service_role;
GRANT ALL ON pipeline.pmi_report_runs            TO service_role;
GRANT ALL ON data_raw.raw_pmi_tracker_files      TO service_role;
GRANT ALL ON data_raw.raw_pmi_tracker_rows       TO service_role;
GRANT ALL ON data_staging.stg_pmi_tracker_sites  TO service_role;

COMMENT ON TABLE reference.ref_pmi_clusters IS
    'Weekly PMI report market registry: market_code (email subject) -> tracker cluster + per-page Power BI filter settings (gc_aging_*, tes_filter, page_variant). Self-registering: unknown codes are inserted by loader.py with registered_by = auto:<run_id>.';
COMMENT ON TABLE pipeline.pmi_report_runs IS
    'One row per weekly PMI report run (dispatch / manual / backfill): status (running -> loaded -> rendered -> sent, or rejected / failed), anomalies jsonb, PDF + email ids. Read by the PMI dashboard through REST (service_role).';
COMMENT ON TABLE data_raw.raw_pmi_tracker_files IS
    'Received PMI tracker xlsx files (pending / failing) or backfill groups (tracker). Upsert key (market_code, tracker_sent, kind, sha256): an identical resend updates received_at / gmail_message_id / run_id; a new sha256 is a new file row.';
COMMENT ON TABLE data_raw.raw_pmi_tracker_rows IS
    'Raw cells of every received tracker file row as jsonb text (header -> value). Replay source only; never read by views.';
COMMENT ON TABLE data_staging.stg_pmi_tracker_sites IS
    'Weekly PMI tracker sites: one row per site per tracker date per cluster per source kind (pending / failing). Replaced per (cluster, tracker_sent, source_kind) on every load. Statuses canonicalised (spec 6.1), buckets per spec 6.2, Tuesday-start cx weeks per spec 6.3.';

-- =============================================================================
-- 2. Views
-- =============================================================================

CREATE VIEW analytics.v_pmi_tracker_dates AS
SELECT s.cluster,
       s.tracker_sent,
       count(*)::int AS total_sites,
       count(*) FILTER (WHERE s.status = 'Yes - Completed')::int AS completed,
       round(100.0 * count(*) FILTER (WHERE s.status = 'Yes - Completed') / count(*), 2) AS completion_pct,
       count(*) FILTER (WHERE s.source_kind = 'pending')::int AS pending_n,
       count(*) FILTER (WHERE s.source_kind = 'failing')::int AS failing_n
FROM data_staging.stg_pmi_tracker_sites s
GROUP BY s.cluster, s.tracker_sent;

CREATE VIEW analytics.v_pmi_status_by_tracker AS
SELECT s.cluster,
       s.tracker_sent,
       COALESCE(NULLIF(btrim(s.status), ''), '(Blank)')        AS status,
       COALESCE(NULLIF(btrim(s.status_bucket), ''), '(Blank)') AS status_bucket,
       COALESCE(s.status_order, 99)::smallint                  AS status_order,
       count(*)::int AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.cluster, s.tracker_sent), 2) AS pct
FROM data_staging.stg_pmi_tracker_sites s
GROUP BY s.cluster, s.tracker_sent,
         COALESCE(NULLIF(btrim(s.status), ''), '(Blank)'),
         COALESCE(NULLIF(btrim(s.status_bucket), ''), '(Blank)'),
         COALESCE(s.status_order, 99);

CREATE VIEW analytics.v_pmi_sent_by_tracker AS
SELECT s.cluster,
       s.tracker_sent,
       COALESCE(NULLIF(btrim(s.sent_to_cm), ''), '(Blank)') AS sent_to_cm,
       count(*)::int AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.cluster, s.tracker_sent), 2) AS pct
FROM data_staging.stg_pmi_tracker_sites s
LEFT JOIN reference.ref_pmi_clusters c ON c.cluster = s.cluster
WHERE c.tes_filter_ma_conducted_by IS NULL
   OR lower(btrim(COALESCE(s.ma_conducted_by, ''))) = lower(btrim(c.tes_filter_ma_conducted_by))
GROUP BY s.cluster, s.tracker_sent, COALESCE(NULLIF(btrim(s.sent_to_cm), ''), '(Blank)');

CREATE VIEW analytics.v_pmi_cm_status AS
WITH base AS (
    SELECT s.cluster, s.tracker_sent,
           COALESCE(NULLIF(lower(btrim(s.cm_name)), ''), '(blank)') AS cm_key,
           COALESCE(NULLIF(btrim(s.cm_name), ''), '(Blank)')        AS cm_spelling,
           COALESCE(NULLIF(btrim(s.status), ''), '(Blank)')         AS status,
           COALESCE(NULLIF(btrim(s.status_bucket), ''), '(Blank)')  AS status_bucket,
           COALESCE(s.status_order, 99)::smallint                   AS status_order
    FROM data_staging.stg_pmi_tracker_sites s
), spelling AS (
    SELECT cluster, tracker_sent, cm_key, cm_spelling,
           row_number() OVER (PARTITION BY cluster, tracker_sent, cm_key
                              ORDER BY count(*) DESC, cm_spelling COLLATE "C") AS rn
    FROM base
    GROUP BY cluster, tracker_sent, cm_key, cm_spelling
)
SELECT b.cluster, b.tracker_sent,
       sp.cm_spelling AS cm_name,
       b.status, b.status_bucket, b.status_order,
       count(*)::int AS n
FROM base b
JOIN spelling sp ON sp.cluster = b.cluster AND sp.tracker_sent = b.tracker_sent
                AND sp.cm_key = b.cm_key AND sp.rn = 1
GROUP BY b.cluster, b.tracker_sent, sp.cm_spelling, b.status, b.status_bucket, b.status_order;

CREATE VIEW analytics.v_pmi_cm_company_status AS
WITH base AS (
    SELECT s.cluster, s.tracker_sent,
           COALESCE(NULLIF(lower(btrim(s.cm_company)), ''), '(blank)') AS co_key,
           COALESCE(NULLIF(btrim(s.cm_company), ''), '(Blank)')        AS co_spelling,
           COALESCE(NULLIF(btrim(s.status), ''), '(Blank)')            AS status,
           COALESCE(NULLIF(btrim(s.status_bucket), ''), '(Blank)')     AS status_bucket,
           COALESCE(s.status_order, 99)::smallint                      AS status_order
    FROM data_staging.stg_pmi_tracker_sites s
), spelling AS (
    SELECT cluster, tracker_sent, co_key, co_spelling,
           row_number() OVER (PARTITION BY cluster, tracker_sent, co_key
                              ORDER BY count(*) DESC, co_spelling COLLATE "C") AS rn
    FROM base
    GROUP BY cluster, tracker_sent, co_key, co_spelling
)
SELECT b.cluster, b.tracker_sent,
       sp.co_spelling AS cm_company,
       b.status, b.status_bucket, b.status_order,
       count(*)::int AS n
FROM base b
JOIN spelling sp ON sp.cluster = b.cluster AND sp.tracker_sent = b.tracker_sent
                AND sp.co_key = b.co_key AND sp.rn = 1
GROUP BY b.cluster, b.tracker_sent, sp.co_spelling, b.status, b.status_bucket, b.status_order;

CREATE VIEW analytics.v_pmi_gc_aging AS
WITH base AS (
    SELECT s.cluster, s.tracker_sent,
           COALESCE(NULLIF(lower(btrim(s.general_contractor)), ''), '(blank)') AS gc_key,
           COALESCE(NULLIF(btrim(s.general_contractor), ''), '(Blank)')        AS gc_spelling,
           COALESCE(s.duration_bucket, 'No CX date')                           AS duration_bucket
    FROM data_staging.stg_pmi_tracker_sites s
    JOIN reference.ref_pmi_clusters c ON c.cluster = s.cluster
    WHERE s.status = ANY (c.gc_aging_statuses)
      AND lower(btrim(COALESCE(s.ontel_notes, ''))) <> ALL (
            ARRAY(SELECT lower(btrim(x)) FROM unnest(c.gc_aging_exclude_notes) AS x))
      AND (NOT c.gc_aging_exclude_blank_gc OR s.general_contractor <> '')
), spelling AS (
    SELECT cluster, tracker_sent, gc_key, gc_spelling,
           row_number() OVER (PARTITION BY cluster, tracker_sent, gc_key
                              ORDER BY count(*) DESC, gc_spelling COLLATE "C") AS rn
    FROM base
    GROUP BY cluster, tracker_sent, gc_key, gc_spelling
)
SELECT b.cluster, b.tracker_sent,
       sp.gc_spelling AS general_contractor,
       b.duration_bucket,
       count(*)::int AS n
FROM base b
JOIN spelling sp ON sp.cluster = b.cluster AND sp.tracker_sent = b.tracker_sent
                AND sp.gc_key = b.gc_key AND sp.rn = 1
GROUP BY b.cluster, b.tracker_sent, sp.gc_spelling, b.duration_bucket;

CREATE VIEW analytics.v_pmi_cx_vs_pmi_weekly AS
WITH snap AS (
    SELECT cluster, tracker_sent,
           make_date(extract(year FROM tracker_sent)::int, 1, 1) AS jan1
    FROM data_staging.stg_pmi_tracker_sites
    GROUP BY cluster, tracker_sent
), wk AS (
    -- Tuesday-start week 1 = the Tue-Mon week containing Jan 1 (spec 6.3)
    SELECT cluster, tracker_sent,
           (jan1 - ((extract(isodow FROM jan1)::int - 2 + 7) % 7))::date AS week1_start
    FROM snap
), spine AS (
    SELECT w.cluster, w.tracker_sent, g.week_no,
           (w.week1_start + 7 * (g.week_no - 1))::date     AS week_start,
           (w.week1_start + 7 * (g.week_no - 1) + 6)::date AS week_end
    FROM wk w
    CROSS JOIN LATERAL generate_series(1, 1 + ((w.tracker_sent - w.week1_start) / 7)) AS g(week_no)
), counts AS (
    SELECT cluster, tracker_sent, cx_week_no,
           count(*)::int AS cx_completed_sites,
           count(*) FILTER (WHERE status = 'Yes - Completed')::int AS pmi_completed_sites
    FROM data_staging.stg_pmi_tracker_sites
    WHERE cx_date IS NOT NULL
      AND extract(year FROM cx_date) = extract(year FROM tracker_sent)
    GROUP BY cluster, tracker_sent, cx_week_no
)
SELECT sp.cluster, sp.tracker_sent,
       sp.week_no::smallint AS cx_week_no,
       sp.week_start, sp.week_end,
       COALESCE(c.cx_completed_sites, 0)  AS cx_completed_sites,
       COALESCE(c.pmi_completed_sites, 0) AS pmi_completed_sites
FROM spine sp
LEFT JOIN counts c ON c.cluster = sp.cluster AND c.tracker_sent = sp.tracker_sent
                  AND c.cx_week_no = sp.week_no;

CREATE VIEW analytics.v_pmi_status_flow AS
WITH dates AS (
    SELECT cluster, tracker_sent,
           lag(tracker_sent) OVER (PARTITION BY cluster ORDER BY tracker_sent) AS prev_tracker_sent
    FROM (SELECT DISTINCT cluster, tracker_sent FROM data_staging.stg_pmi_tracker_sites) d
), snap AS (
    SELECT cluster, tracker_sent, fuze_project_id,
           bool_or(status = 'Yes - Completed') AS completed
    FROM data_staging.stg_pmi_tracker_sites
    GROUP BY cluster, tracker_sent, fuze_project_id
), joined AS (
    SELECT d.cluster, d.tracker_sent, d.prev_tracker_sent,
           c.completed                   AS cur_completed,
           (p.fuze_project_id IS NOT NULL) AS in_prev,
           p.completed                   AS prev_completed
    FROM dates d
    JOIN snap c ON c.cluster = d.cluster AND c.tracker_sent = d.tracker_sent
    LEFT JOIN snap p ON p.cluster = d.cluster AND p.tracker_sent = d.prev_tracker_sent
                    AND p.fuze_project_id = c.fuze_project_id
), resolved AS (
    SELECT d.cluster, d.tracker_sent, count(*)::int AS resolved_n
    FROM dates d
    JOIN snap p ON p.cluster = d.cluster AND p.tracker_sent = d.prev_tracker_sent
    LEFT JOIN snap c ON c.cluster = d.cluster AND c.tracker_sent = d.tracker_sent
                    AND c.fuze_project_id = p.fuze_project_id
    WHERE c.fuze_project_id IS NULL
    GROUP BY d.cluster, d.tracker_sent
)
SELECT j.cluster, j.tracker_sent, j.prev_tracker_sent,
       count(*) FILTER (WHERE NOT j.in_prev)::int AS new_n,
       COALESCE(max(r.resolved_n), 0)             AS resolved_n,
       count(*) FILTER (WHERE j.in_prev)::int     AS carried_n,
       count(*) FILTER (WHERE j.cur_completed AND (NOT j.in_prev OR NOT j.prev_completed))::int AS completed_new_n
FROM joined j
LEFT JOIN resolved r ON r.cluster = j.cluster AND r.tracker_sent = j.tracker_sent
GROUP BY j.cluster, j.tracker_sent, j.prev_tracker_sent;

CREATE VIEW analytics.v_pmi_gc_aging_history AS
WITH base AS (
    SELECT s.cluster, s.tracker_sent, s.days_since_cx,
           COALESCE(NULLIF(lower(btrim(s.general_contractor)), ''), '(blank)') AS gc_key,
           COALESCE(NULLIF(btrim(s.general_contractor), ''), '(Blank)')        AS gc_spelling
    FROM data_staging.stg_pmi_tracker_sites s
    JOIN reference.ref_pmi_clusters c ON c.cluster = s.cluster
    WHERE s.status = ANY (c.gc_aging_statuses)
      AND lower(btrim(COALESCE(s.ontel_notes, ''))) <> ALL (
            ARRAY(SELECT lower(btrim(x)) FROM unnest(c.gc_aging_exclude_notes) AS x))
      AND (NOT c.gc_aging_exclude_blank_gc OR s.general_contractor <> '')
), spelling AS (
    SELECT cluster, tracker_sent, gc_key, gc_spelling,
           row_number() OVER (PARTITION BY cluster, tracker_sent, gc_key
                              ORDER BY count(*) DESC, gc_spelling COLLATE "C") AS rn
    FROM base
    GROUP BY cluster, tracker_sent, gc_key, gc_spelling
)
SELECT b.cluster, b.tracker_sent,
       sp.gc_spelling AS general_contractor,
       count(*)::int AS pending_n,
       round(avg(b.days_since_cx)::numeric, 1) AS avg_days,
       max(b.days_since_cx) AS max_days
FROM base b
JOIN spelling sp ON sp.cluster = b.cluster AND sp.tracker_sent = b.tracker_sent
                AND sp.gc_key = b.gc_key AND sp.rn = 1
GROUP BY b.cluster, b.tracker_sent, sp.gc_spelling;

CREATE VIEW analytics.v_pmi_sites AS
WITH swift_assets AS (             -- one row per asset_did, keyed by FUZE id
    SELECT DISTINCT ON (asset_did) id, asset_did, asset_id, asset_status,
           ((regexp_match(asset_id, '/([0-9]{6,9})/'))[1])::bigint AS fuze_project_id
    FROM data_staging.stg_assets
    WHERE asset_id ~ '/VZW/' AND asset_id ~ '/[0-9]{6,9}/'
    ORDER BY asset_did, id DESC
), swift_tasks AS (                -- aggregate first: (asset_did, task_name) is not unique
    SELECT asset_did,
           MIN(task_approved_on) FILTER (WHERE task_name = '1. PMI COP Complete'        AND task_status = 'approved') AS pmi_cop_complete_on,
           MIN(task_approved_on) FILTER (WHERE task_name = '2. PMI COP Upload Complete' AND task_status = 'approved') AS pmi_cop_upload_on
    FROM data_staging.stg_asset_tasks
    WHERE task_name IN ('1. PMI COP Complete', '2. PMI COP Upload Complete')
    GROUP BY asset_did
), swift_by_fuze AS (              -- one asset per FUZE id, explicit preference
    SELECT DISTINCT ON (a.fuze_project_id) a.*, t.pmi_cop_complete_on, t.pmi_cop_upload_on
    FROM swift_assets a LEFT JOIN swift_tasks t USING (asset_did)
    ORDER BY a.fuze_project_id, (t.pmi_cop_complete_on IS NOT NULL) DESC,
             (a.asset_status <> 'cancelled') DESC, a.asset_did COLLATE "C" DESC, a.id DESC
)
SELECT s.*,
       w.asset_did                 AS swift_asset_did,
       w.asset_id                  AS swift_asset_id,
       w.asset_status              AS swift_asset_status,
       w.pmi_cop_complete_on::date AS pmi_cop_complete_on,
       w.pmi_cop_upload_on::date   AS pmi_cop_upload_on,
       CASE WHEN w.asset_did IS NULL THEN NULL
            ELSE (COALESCE(s.status, '') = 'Yes - Completed')
                 = (w.pmi_cop_complete_on IS NOT NULL AND w.pmi_cop_complete_on <= s.tracker_sent)
       END                         AS swift_agrees
FROM data_staging.stg_pmi_tracker_sites s
LEFT JOIN swift_by_fuze w ON w.fuze_project_id = s.fuze_project_id;

-- View ACLs: the dashboard reads these through REST as service_role (spec 4.6)
REVOKE ALL ON analytics.v_pmi_tracker_dates      FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_status_by_tracker  FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_sent_by_tracker    FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_cm_status          FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_cm_company_status  FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_gc_aging           FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_cx_vs_pmi_weekly   FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_status_flow        FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_gc_aging_history   FROM anon, authenticated;
REVOKE ALL ON analytics.v_pmi_sites              FROM anon, authenticated;
GRANT SELECT ON analytics.v_pmi_tracker_dates      TO service_role;
GRANT SELECT ON analytics.v_pmi_status_by_tracker  TO service_role;
GRANT SELECT ON analytics.v_pmi_sent_by_tracker    TO service_role;
GRANT SELECT ON analytics.v_pmi_cm_status          TO service_role;
GRANT SELECT ON analytics.v_pmi_cm_company_status  TO service_role;
GRANT SELECT ON analytics.v_pmi_gc_aging           TO service_role;
GRANT SELECT ON analytics.v_pmi_cx_vs_pmi_weekly   TO service_role;
GRANT SELECT ON analytics.v_pmi_status_flow        TO service_role;
GRANT SELECT ON analytics.v_pmi_gc_aging_history   TO service_role;
GRANT SELECT ON analytics.v_pmi_sites              TO service_role;

COMMENT ON VIEW analytics.v_pmi_tracker_dates IS
    'One row per (cluster, tracker_sent): total_sites, completed, completion_pct (Yes - Completed / total, blanks in the denominator), pending_n, failing_n. Feeds date pickers and the last-6 trend selection.';
COMMENT ON VIEW analytics.v_pmi_status_by_tracker IS
    'Site counts per (cluster, tracker_sent, status, status_bucket, status_order); NULL/blank -> (Blank) order 99. Sum n over status (standard page) or status_bucket (MP page). Feeds donut, trend bars, completion line.';
COMMENT ON VIEW analytics.v_pmi_sent_by_tracker IS
    'Site counts per (cluster, tracker_sent, sent_to_cm) with (Blank); honours ref_pmi_clusters.tes_filter_ma_conducted_by (SOVA: MA Conducted By = TES). Feeds the TES pie and TES trend.';
COMMENT ON VIEW analytics.v_pmi_cm_status IS
    'Per construction manager status stacks. cm_name merged on lower(trim()), most frequent spelling displayed (ties: COLLATE "C" ordinal), (Blank) for empty. Grain (cluster, tracker_sent, cm_name, status, status_bucket).';
COMMENT ON VIEW analytics.v_pmi_cm_company_status IS
    'Per CM company status stacks. cm_company merged on lower(trim()), most frequent spelling displayed (ties: COLLATE "C" ordinal), (Blank) for empty.';
COMMENT ON VIEW analytics.v_pmi_gc_aging IS
    'Pending PMI COPs per general contractor by days-since-CX bucket (>=100 / 51-99 / 0-50 / No CX date). Filter per ref_pmi_clusters: status = ANY(gc_aging_statuses), notes not in gc_aging_exclude_notes (lower/trim), blank GC excluded when gc_aging_exclude_blank_gc. Bar total = filtered row count.';
COMMENT ON VIEW analytics.v_pmi_cx_vs_pmi_weekly IS
    'MP chart feed: Tuesday-start week spine 1..week_of(tracker_sent) of the tracker year, zero-filled; cx_completed_sites = snapshot rows with cx_date in that week (tracker year), pmi_completed_sites = those with status Yes - Completed. week_start Tuesday, week_end Monday.';
COMMENT ON VIEW analytics.v_pmi_status_flow IS
    'Site movement between consecutive tracker dates of a cluster keyed on fuze_project_id: new_n (not in previous), resolved_n (in previous, gone now), carried_n, completed_new_n (completed now, not completed or absent before).';
COMMENT ON VIEW analytics.v_pmi_gc_aging_history IS
    'Per (cluster, tracker_sent, general_contractor) history of the GC-aging population: pending_n, avg_days, max_days of days_since_cx. Same filter as v_pmi_gc_aging.';
COMMENT ON VIEW analytics.v_pmi_sites IS
    'Every stg_pmi_tracker_sites column plus Swift: swift_asset_did / swift_asset_id / swift_asset_status, pmi_cop_complete_on, pmi_cop_upload_on (approved task dates), swift_agrees (NULL when no Swift asset). Joined on the FUZE id embedded in the stg_assets asset_id path (Verizon scope = asset_id ~ /VZW/); one asset per FUZE id (approved PMI first, non-cancelled first, latest asset_did).';

-- =============================================================================
-- 3. agent.schema_metadata (5 tables + 10 views)
-- =============================================================================

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, NULL, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('reference', 'ref_pmi_clusters',
     'Weekly PMI report market registry: market_code -> tracker cluster + per-page filter settings.',
     'One row per Verizon market (FL, WBV, CGC, MP, ...). Drives the report page variant, GC-aging filters and the TES filter that Power BI used to hold as page filters.',
     'Self-registering: unknown market codes are inserted at load time (registered_by = auto:<run_id>). active=false for the six markets stopped in March 2026. recipients empty in v1 (runtime default PMI_REPORT_RECIPIENTS).',
     ARRAY['data_staging.stg_pmi_tracker_sites (via cluster)']),
    ('pipeline', 'pmi_report_runs',
     'Run log of the weekly PMI report pipeline (load, render, send).',
     'Operational: one row per dispatch / manual / backfill run with status, anomalies jsonb (blank_status, row_count_jump, ...), PDF Drive id and email id.',
     'status running -> loaded -> rendered -> sent, or rejected / failed. Read by the PMI dashboard via REST as service_role.',
     ARRAY['data_raw.raw_pmi_tracker_files (via run_id)', 'data_staging.stg_pmi_tracker_sites (via run_id)']),
    ('data_raw', 'raw_pmi_tracker_files',
     'Received PMI tracker xlsx files (pending / failing) and backfill groups (tracker).',
     'Landing registry: sha256, Drive and Gmail ids, row_count.',
     'Upsert key (market_code, tracker_sent, kind, sha256). Rows of an earlier sha256 are kept for replay.',
     ARRAY['data_raw.raw_pmi_tracker_rows (via file_id)', 'pipeline.pmi_report_runs (via run_id)']),
    ('data_raw', 'raw_pmi_tracker_rows',
     'Raw cells of each received tracker file row as jsonb text.',
     'Replay source for stg_pmi_tracker_sites; never read by views.',
     'PK (file_id, row_no). ON DELETE CASCADE from raw_pmi_tracker_files.',
     ARRAY['data_raw.raw_pmi_tracker_files (via file_id)']),
    ('data_staging', 'stg_pmi_tracker_sites',
     'Weekly PMI tracker: one row per site per tracker date per cluster per source kind.',
     'The cleaned SharePoint PMI tracker. All analytics.v_pmi_* views read it. Statuses canonicalised, status_bucket = tracker Ontel Notes 2 rule, Tuesday-start cx weeks.',
     'Replaced per (cluster, tracker_sent, source_kind) on every load. Join to Swift on fuze_project_id (prefer analytics.v_pmi_sites, which already resolves one asset per FUZE id from the asset_id path). PK includes site_name and general_contractor (the tracker dedupe key).',
     ARRAY['reference.ref_pmi_clusters (via cluster)', 'data_raw.raw_pmi_tracker_files (via source_file_id)', 'data_staging.stg_assets (via FUZE id in asset_id)']),
    ('analytics', 'v_pmi_tracker_dates',
     'Totals and completion % per (cluster, tracker_sent).',
     'Date pickers and the last-6 trend window of the weekly PMI report.',
     'completion_pct = Yes - Completed / total_sites, blanks in the denominator.',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_status_by_tracker',
     'Site counts per tracker date by status and status_bucket.',
     'Donut, trend bars and completion line of the weekly PMI report.',
     'Sum n over status (standard page) or status_bucket (MP page). (Blank) = NULL/empty, status_order 99.',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_sent_by_tracker',
     'Site counts per tracker date by sent_to_cm (Uploaded to TES Portal).',
     'TES pie and TES trend.',
     'Honours ref_pmi_clusters.tes_filter_ma_conducted_by (SOVA: TES only).',
     ARRAY['data_staging.stg_pmi_tracker_sites', 'reference.ref_pmi_clusters']),
    ('analytics', 'v_pmi_cm_status',
     'Status stacks per construction manager.',
     'Per-CM chart of the weekly PMI report.',
     'cm_name merged on lower(trim()); most frequent spelling shown, ties broken COLLATE "C" (ordinal, same as the Python mirror).',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_cm_company_status',
     'Status stacks per construction manager company.',
     'Per-CM-company chart of the weekly PMI report.',
     'cm_company merged on lower(trim()); most frequent spelling shown, ties broken COLLATE "C".',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_gc_aging',
     'Pending PMI COPs per general contractor by days-since-CX bucket.',
     'GC aging bar of the weekly PMI report; filter settings per market in ref_pmi_clusters.',
     'duration_bucket No CX date for rows without cx_date so the bar total equals the filtered count.',
     ARRAY['data_staging.stg_pmi_tracker_sites', 'reference.ref_pmi_clusters']),
    ('analytics', 'v_pmi_cx_vs_pmi_weekly',
     'CX-complete vs PMI-complete sites per Tuesday-start week, zero-filled spine.',
     'MP (Zeta) page chart, weeks [w-6, w-1] of the tracker week.',
     'Only cx_date in the tracker year counts (the helper sheet rule).',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_status_flow',
     'New / resolved / carried / newly completed sites between consecutive tracker dates.',
     'Dashboard Analytics tab.',
     'Keyed on (cluster, fuze_project_id).',
     ARRAY['data_staging.stg_pmi_tracker_sites']),
    ('analytics', 'v_pmi_gc_aging_history',
     'GC-aging population history per general contractor: pending_n, avg_days, max_days.',
     'Dashboard Analytics tab.',
     'Same filter as v_pmi_gc_aging.',
     ARRAY['data_staging.stg_pmi_tracker_sites', 'reference.ref_pmi_clusters']),
    ('analytics', 'v_pmi_sites',
     'Tracker sites with the matching Swift asset and PMI COP task dates.',
     'Drill-down, Sites tab and the Swift-vs-tracker agreement check.',
     'Verizon scope = stg_assets.asset_id ~ /VZW/ (carrier_group and asset_identifier are NULL on every row); FUZE id = the 6-9 digit segment of asset_id. One Swift asset per FUZE id (approved PMI first, non-cancelled first, latest asset_did). swift_agrees NULL when no asset. Promote swift_by_fuze to a materialized view if too slow.',
     ARRAY['data_staging.stg_pmi_tracker_sites', 'data_staging.stg_assets', 'data_staging.stg_asset_tasks'])
) AS v(schema_name, table_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name AND m.column_name IS NULL
);

-- =============================================================================
-- 4. Seed: the ten tracker clusters (spec 4.1)
-- =============================================================================

INSERT INTO reference.ref_pmi_clusters
    (market_code, cluster, subtitle, page_variant, show_completion_line,
     gc_aging_statuses, gc_aging_exclude_notes, gc_aging_exclude_blank_gc,
     tes_filter_ma_conducted_by, active, registered_by)
VALUES
    ('FL',     'Alpha_2', 'VZW/FL - Embedded',            'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, true,  'seed'),
    ('WBV',    'Alpha_3', 'VZW/WBV - Embedded',           'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, true,  'seed'),
    ('CGC',    'Epsilon', 'VZW/CGC - Embedded',           'standard', false,
     ARRAY['No - Pending Items', 'No - Failing PMI', 'NO'],
     ARRAY['Site is not listed in the VZW tracker.', 'The MA is not performed by TES.'], false, NULL, true, 'seed'),
    ('MP',     'Zeta',    'VZW/MP - Embedded, NSB Macro', 'mp',       false,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], true,  NULL, true,  'seed'),
    ('BAWA',   'Alpha',   'VZW/BAWA',                     'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, false, 'seed'),
    ('SOVA',   'Gamma',   'VZW/SOVA - Embedded',          'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, 'TES', false, 'seed'),
    ('DELTA',  'Delta',   'VZW/DELTA',                    'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, false, 'seed'),
    ('DELTA2', 'Delta_2', 'VZW/DELTA2',                   'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, false, 'seed'),
    ('DELTA3', 'Delta_3', 'VZW/DELTA3',                   'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, false, 'seed'),
    ('DELTA4', 'Delta_4', 'VZW/DELTA4',                   'standard', true,
     ARRAY['No - Pending Items', 'No - Failing PMI'], ARRAY[]::text[], false, NULL, false, 'seed')
ON CONFLICT (market_code) DO NOTHING;

COMMIT;
