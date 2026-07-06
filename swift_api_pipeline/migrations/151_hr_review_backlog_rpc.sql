-- 151: analytics.hr_review_backlog() — the CURRENT unfiled-report backlog,
-- aged, independent of any date range. The dashboard's "missing" count is
-- range-scoped and undifferentiated; an HR lead also needs "what is overdue
-- right now and how stale is it." A report is overdue once it matures unfiled
-- (evidence_at + 48h in the past and still pending/in_progress = is_missing_report).
--
-- Returns: total, oldest_days, and age buckets by days-past-the-48h-due-point:
-- <=2d, 2-7d, 7-14d, 14-30d, 30d+. No date args — always "as of now()".
create or replace function analytics.hr_review_backlog()
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  with m as (
    select extract(epoch from (now() - (evidence_at + interval '48 hours'))) / 86400.0 as overdue_days
    from analytics.v_hr_report_review
    where is_missing_report
  ), b as (
    select
      count(*)                                                        as total,
      round(max(overdue_days)::numeric, 1)                            as oldest_days,
      count(*) filter (where overdue_days < 2)                        as d_le2,
      count(*) filter (where overdue_days >= 2  and overdue_days < 7) as d_2_7,
      count(*) filter (where overdue_days >= 7  and overdue_days < 14) as d_7_14,
      count(*) filter (where overdue_days >= 14 and overdue_days < 30) as d_14_30,
      count(*) filter (where overdue_days >= 30)                      as d_30p
    from m
  )
  select jsonb_build_object(
    'total', (select total from b),
    'oldest_days', (select oldest_days from b),
    'buckets', jsonb_build_array(
      jsonb_build_object('label', '≤2d',    'n', (select d_le2 from b)),
      jsonb_build_object('label', '2–7d',   'n', (select d_2_7 from b)),
      jsonb_build_object('label', '7–14d',  'n', (select d_7_14 from b)),
      jsonb_build_object('label', '14–30d', 'n', (select d_14_30 from b)),
      jsonb_build_object('label', '30d+',   'n', (select d_30p from b))
    )
  );
$$;

revoke all on function analytics.hr_review_backlog() from public, anon, authenticated;
grant execute on function analytics.hr_review_backlog() to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','hr_review_backlog',
   'Function(): current unfiled-report backlog as of now(), independent of any date range. Over analytics.v_hr_report_review where is_missing_report (matured, has work evidence, still unfiled). Returns total, oldest_days (max days past the 48h due point), and age buckets by days overdue: <=2d, 2-7d, 7-14d, 14-30d, 30d+.',
   'Powers the HR Report Review dashboard "unfiled backlog (now)" card — how many reports are overdue right now and how stale.',
   ARRAY['analytics.v_hr_report_review'])
ON CONFLICT DO NOTHING;
