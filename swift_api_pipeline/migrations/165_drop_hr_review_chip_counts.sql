-- 165: drop analytics.hr_review_chip_counts (added in 164, unused as of the
-- same day). Measured on prod data 2026-07-08: the single-scan jsonb count
-- (~940ms cold, no filters) lost to the app's original 5 parallel PostgREST
-- per-flag counts (~400ms wall), because a literal per-flag WHERE lets the
-- planner push the predicate into the view's joins while the parameterized
-- single scan computes the full view; a 5-subquery SQL function was worse
-- still (~1.9s, sequential). The app reverted chip counts to the parallel
-- client path; hr_review_page and hr_review_count (also 164) stay.

drop function if exists analytics.hr_review_chip_counts(text, text, text, date, date);

delete from agent.schema_metadata
where schema_name = 'analytics' and table_name = 'hr_review_chip_counts';
