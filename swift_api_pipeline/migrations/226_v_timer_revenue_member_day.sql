-- 226: analytics.v_timer_revenue_member_day — member-day rollup of the revenue MV
--
-- WHY: the ontel-people variance dashboard needs scope revenue, per-member
-- totals, and per-(member, day) heatmap cells. It was reading the per-TASK
-- grain (analytics.mv_timer_revenue_daily) and aggregating in TypeScript, which
-- meant paging tens of thousands of rows through PostgREST. The app's paging
-- budget (20 pages x 1000) silently truncated long ranges, and because the read
-- is ordered by work_date ASC the rows it dropped were the most RECENT ones:
-- the heatmap rendered filled for the early months and empty for everything
-- after the cut, which reads as "no work happened" rather than "we stopped
-- fetching". Measured on live data 2026-08-07 for a real filter (49 active
-- Technical Analysts, 2026-01-01..2026-07-31): 40,092 task-grain rows, of which
-- 18,971 fall on or before 2026-04-20, so the 20,000-row cap landed in the
-- 04-20..04-27 week and every later week read as empty. Scope KPIs were
-- undercounted by the same cut.
--
-- This view moves that rollup into Postgres. Same window aggregates to 10,831
-- member-day rows instead of 50,872 task rows (4.7x fewer), which fits the
-- app's existing paging budget with roughly 1.5 years of headroom; the app
-- still surfaces a visible warning if it ever caps, rather than logging.
--
-- Grain: one row per (user_email, work_date).
--   attributed_usd  — summed ONLY over pricing_status='priced'. NULL amounts are
--                     unpriced work and 0 is the lr_unapproved_zero case, so
--                     both stay out of the money while their MINUTES still
--                     count in total_minutes below.
--   priced_minutes / total_minutes — coverage numerator/denominator. Minutes are
--                     the honest denominator; a dollar share would be circular.
--
-- Plain view, not materialized: measured 277ms over the 7-month window on an
-- index scan of mv_timer_revenue_daily_email_date_idx, well inside the 8s
-- PostgREST timeout, and it needs no refresh wiring so it can never go stale
-- against its parent MV. Migration 227 also LEFT JOINs it into
-- analytics.hr_review_page/hr_review_count so DR Monitoring can filter and sort
-- on revenue server-side.
--
-- SECURITY: analytics views are reached through the service role only; the app
-- gates revenue at REVENUE_MIN_ROLE before it ever queries. No anon or
-- authenticated grant is issued here.

CREATE OR REPLACE VIEW analytics.v_timer_revenue_member_day AS
SELECT
    lower(d.user_email) AS user_email,
    d.work_date,
    sum(d.amount_usd) FILTER (WHERE d.pricing_status = 'priced') AS attributed_usd,
    sum(d.day_minutes) FILTER (WHERE d.pricing_status = 'priced') AS priced_minutes,
    sum(d.day_minutes) AS total_minutes
FROM analytics.mv_timer_revenue_daily d
WHERE d.user_email IS NOT NULL
GROUP BY lower(d.user_email), d.work_date;

COMMENT ON VIEW analytics.v_timer_revenue_member_day IS
'Member-day rollup of analytics.mv_timer_revenue_daily for the ontel-people variance dashboard and the DR Monitoring revenue column. attributed_usd sums only priced rows; priced_minutes/total_minutes are the coverage ratio. Emails are lowercased so app-side joins need no per-row normalization. Migration 226.';

GRANT SELECT ON analytics.v_timer_revenue_member_day TO service_role;
