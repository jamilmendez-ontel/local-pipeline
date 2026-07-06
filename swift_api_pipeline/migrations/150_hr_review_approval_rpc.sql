-- 150: analytics.hr_review_approval(p_from, p_to) — approval-latency aggregates
-- for the /hr dashboard. Approval lag (submitted -> approved) is a known, separate
-- bottleneck (bi-monthly approval, ~12d average) that the dashboard did not
-- surface at all; the underlying columns already exist on v_hr_report_review.
--
-- Returns: kpis {approved, median_days, p90_days, awaiting} + a per-work-date
-- trend {d, med_days (null when no approvals that day), approved_n, awaiting_n}.
-- Latency is submitted_on_et -> approved_on_et in fractional days (the metric is
-- days-scale). Cancelled tasks are excluded from the approved set, consistent
-- with hr_review_summary (149) and hr_review_offenders (147). "Awaiting" = still
-- in 'submitted' status (not yet approved), the current backlog.
create or replace function analytics.hr_review_approval(p_from date, p_to date)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  with rows_in as (
    select * from analytics.v_hr_report_review
    where work_date between p_from and p_to
  ), appr as (
    select work_date,
           extract(epoch from (approved_on_et - submitted_on_et)) / 86400.0 as appr_days
    from rows_in
    where approved_on_et is not null and submitted_on_et is not null
      and task_status <> 'cancelled'
  ), kpis as (
    select
      (select count(*) from appr)                                                              as approved,
      (select round(percentile_cont(0.5) within group (order by appr_days)::numeric, 1) from appr) as median_days,
      (select round(percentile_cont(0.9) within group (order by appr_days)::numeric, 1) from appr) as p90_days,
      (select count(*) from rows_in where task_status = 'submitted')                            as awaiting
  ), days as (
    select distinct work_date as d from rows_in
  ), trend as (
    select days.d,
           round(
             (percentile_cont(0.5) within group (order by a.appr_days))::numeric, 1
           ) as med_days,
           count(a.appr_days) as approved_n,
           (select count(*) from rows_in ri where ri.work_date = days.d and ri.task_status = 'submitted') as awaiting_n
    from days left join appr a on a.work_date = days.d
    group by days.d
    order by days.d
  )
  select jsonb_build_object(
    'kpis',  (select to_jsonb(k) from kpis k),
    'trend', coalesce((select jsonb_agg(to_jsonb(t) order by t.d) from trend t), '[]'::jsonb)
  );
$$;

revoke all on function analytics.hr_review_approval(date, date) from public, anon, authenticated;
grant execute on function analytics.hr_review_approval(date, date) to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','hr_review_approval',
   'Function(p_from date, p_to date): approval-latency aggregates over analytics.v_hr_report_review. kpis {approved, median_days, p90_days, awaiting} where latency = submitted_on_et -> approved_on_et in fractional days over non-cancelled approved reports, and awaiting = count still in submitted status. trend per work_date {d, med_days (null when no approvals), approved_n, awaiting_n}.',
   'Powers the HR Report Review dashboard approval-latency card (median/p90 days to approve + current awaiting backlog + per-day trend). Approval lag is a known bi-monthly bottleneck.',
   ARRAY['analytics.v_hr_report_review'])
ON CONFLICT DO NOTHING;
