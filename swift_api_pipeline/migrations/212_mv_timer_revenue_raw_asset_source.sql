-- 212: mv_timer_revenue — source asset_identifier from data_raw.raw_assets, not stg_assets
-- Found during premerge review of PRs #29/#30: stg_assets is fully refreshed nightly by
-- transform_assets() BEFORE the assets-extract phase re-enriches asset_identifier (and the
-- GHA nightly runs main, which wiped the 209 backfill within hours). A refresh of this MV
-- landing in that window (refresh_analytics in the nightly) would rebuild with zero
-- identifiers and null out ALL revenue until the next post-enrich refresh.
-- Fix: asset_market reads data_raw.raw_assets directly (identifier always present there —
-- the assets extract TRUNCATE+reloads it in one phase), making the MV refresh-order-
-- independent. stg_assets.asset_identifier stays as the convenience column (enrich keeps
-- it fresh once this PR merges). Also re-runs the 209 backfill wiped by tonight's refresh.
-- Applied to prod 2026-08-03; verified: 122,310 rows / $7.55M / 112,294 priced,
-- no_market fraction unchanged (1.16%) with the MV independent of stg_assets state.
DROP MATERIALIZED VIEW analytics.mv_timer_revenue;

CREATE MATERIALIZED VIEW analytics.mv_timer_revenue AS
WITH asset_market AS (
    SELECT DISTINCT ON (r.asset_did)
           r.asset_did, r.asset_identifier, cw.market_bucket
    FROM data_raw.raw_assets r
    LEFT JOIN reference.ref_market_bucket_crosswalk cw
      ON cw.market_signature = reference.market_signature(r.asset_identifier)
    WHERE r.asset_identifier IS NOT NULL
    ORDER BY r.asset_did, r.loaded_at DESC
),
lr_approved AS (
    SELECT DISTINCT asset_did
    FROM data_staging.stg_asset_tasks
    WHERE task_name_clean = 'Live Review Complete' AND task_status = 'approved'
),
tech_task AS (
    SELECT t.asset_did, t.task_clean, t.user_email,
           max(t.user_name) AS user_name,
           sum(t.duration_min) AS tech_minutes,
           count(*) AS timer_entries,
           min(t.start_date) AS first_work_date,
           max(t.end_date) AS last_work_date
    FROM data_staging.stg_timer_activities_clean t
    WHERE t.asset_did IS NOT NULL
      AND t.task_clean IS NOT NULL
      AND t.duration_min > 0
    GROUP BY 1, 2, 3
),
task_total AS (
    SELECT asset_did, task_clean, sum(tech_minutes) AS task_minutes
    FROM tech_task GROUP BY 1, 2
),
base AS (
    SELECT
        tt.asset_did,
        am.asset_identifier,
        am.market_bucket,
        tt.task_clean,
        tt.user_email,
        tt.user_name,
        tt.timer_entries,
        tt.first_work_date,
        tt.last_work_date,
        tt.tech_minutes,
        tot.task_minutes,
        tt.tech_minutes / tot.task_minutes AS tech_share,
        (la.asset_did IS NOT NULL) AS lr_approved,
        r.amount_usd  AS task_rate_usd,
        r.duration_hrs AS task_rate_duration_hrs,
        CASE
            WHEN am.market_bucket IS NULL OR am.market_bucket = 'EXCLUDED'
                 OR r.id IS NULL THEN NULL
            WHEN tt.task_clean = 'Final COP Complete' AND la.asset_did IS NULL
                THEN r.amount_usd + COALESCE(rlr.amount_usd, 0)
            WHEN tt.task_clean = 'Live Review Complete' AND la.asset_did IS NULL
                THEN 0
            ELSE r.amount_usd
        END AS bundled_rate_usd,
        (tt.task_clean = 'Final COP Complete' AND la.asset_did IS NULL
         AND r.id IS NOT NULL) AS lr_bundled_into_fcop,
        CASE
            WHEN am.market_bucket IS NULL THEN 'no_market'
            WHEN am.market_bucket = 'EXCLUDED' THEN 'excluded_market'
            WHEN r.id IS NULL THEN 'no_rate'
            WHEN tt.task_clean = 'Live Review Complete' AND la.asset_did IS NULL
                THEN 'lr_unapproved_zero'
            ELSE 'priced'
        END AS pricing_status
    FROM tech_task tt
    JOIN task_total tot
      ON tot.asset_did = tt.asset_did AND tot.task_clean = tt.task_clean
    LEFT JOIN asset_market am ON am.asset_did = tt.asset_did
    LEFT JOIN lr_approved la ON la.asset_did = tt.asset_did
    LEFT JOIN reference.ref_task_revenue_rates r
      ON r.market_bucket = am.market_bucket AND r.task_name_norm = tt.task_clean
    LEFT JOIN reference.ref_task_revenue_rates rlr
      ON rlr.market_bucket = am.market_bucket
     AND rlr.task_name_norm = 'Live Review Complete'
)
SELECT
    asset_did, asset_identifier, market_bucket, task_clean,
    user_email, user_name, timer_entries, first_work_date, last_work_date,
    round(tech_minutes, 2) AS tech_minutes,
    round(task_minutes, 2) AS task_minutes,
    round(tech_share, 4)   AS tech_share,
    lr_approved, lr_bundled_into_fcop, pricing_status,
    task_rate_usd, task_rate_duration_hrs, bundled_rate_usd,
    round(bundled_rate_usd * tech_share, 2) AS amount_usd
FROM base;

CREATE UNIQUE INDEX mv_timer_revenue_pk
    ON analytics.mv_timer_revenue (asset_did, task_clean, user_email);
CREATE INDEX mv_timer_revenue_email_idx ON analytics.mv_timer_revenue (user_email);
CREATE INDEX mv_timer_revenue_bucket_idx ON analytics.mv_timer_revenue (market_bucket);

GRANT SELECT ON analytics.mv_timer_revenue TO service_role;

-- Restore stg_assets.asset_identifier (wiped by tonight's full refresh; the enrich
-- code change ships in the same PR and keeps it populated from the next assets phase)
WITH src AS (
    SELECT DISTINCT ON (project_did, asset_did)
           project_did, asset_did, asset_identifier
    FROM data_raw.raw_assets
    WHERE asset_identifier IS NOT NULL
    ORDER BY project_did, asset_did, loaded_at DESC
)
UPDATE data_staging.stg_assets s
SET asset_identifier = src.asset_identifier
FROM src
WHERE s.project_did = src.project_did
  AND s.asset_did = src.asset_did
  AND s.asset_identifier IS DISTINCT FROM src.asset_identifier;
