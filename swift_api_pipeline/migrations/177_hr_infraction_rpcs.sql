-- 177: HR Infraction Monitor RPCs. Per-employee monthly unexcused infraction
-- counts for two tracks (late DR filing; tardiness past the 30-min grace) plus
-- per-infraction detail rows, joining the latest-wins excusal state in
-- app_hr.hr_infraction_excusal (ontel-people migration 019). Spec: ontel-people
-- docs/superpowers/specs/2026-07-09-hr-infraction-monitor-design.md.
--
-- Infraction definitions (cancelled tasks never count, mirroring the
-- migration-147 offenders convention):
--   filing    = is_late_filing AND task_status <> 'cancelled'
--               (report actually filed more than 48h after first clock-in;
--                never-filed reports deliberately do NOT count, per Jamil
--                2026-07-09: reminder emails own that workflow)
--   tardiness = clock_in_late_minutes > 30 AND task_status <> 'cancelled'
--               (30 = ontel-people LATE_GRACE_MINUTES; a sql-sync unit test in
--                ontel-people pins this constant to this migration)
--
-- The habitual thresholds (5+/month; 8 or 10 across any 3 consecutive months)
-- are applied app-side by the ontel-people rules engine; these RPCs only
-- return counts and rows, so threshold tweaks never need a warehouse change.
-- Hardening block per migration 147; search_path adds app_hr for the excusal
-- join (service_role executes these, so it can read the deny-all table).
create or replace function analytics.hr_infraction_months(p_from date, p_to date)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference, app_hr
as $$
  with base as (
    select emp_id, employee_name, carrier_group, task_did,
           date_trunc('month', work_date)::date as month,
           (is_late_filing and task_status <> 'cancelled')             as filing_inf,
           (clock_in_late_minutes > 30 and task_status <> 'cancelled') as tardy_inf
    from analytics.v_hr_report_review
    where work_date between p_from and p_to
  ), inf as (
    select 'filing'::text as track, emp_id, employee_name, carrier_group, task_did, month
    from base where filing_inf
    union all
    select 'tardiness', emp_id, employee_name, carrier_group, task_did, month
    from base where tardy_inf
  ), exc as (
    select distinct on (track, task_did) track, task_did, action
    from app_hr.hr_infraction_excusal
    order by track, task_did, created_at desc, id desc
  ), agg as (
    select i.emp_id, max(i.employee_name) as employee_name,
           max(i.carrier_group) as carrier_group, i.month, i.track,
           count(*) filter (where e.action is distinct from 'excused') as unexcused_count,
           count(*) filter (where e.action = 'excused')                as excused_count
    from inf i
    left join exc e on e.track = i.track and e.task_did = i.task_did
    group by i.emp_id, i.month, i.track
  )
  select jsonb_build_object(
    'months', coalesce((select jsonb_agg(to_jsonb(a) order by a.emp_id, a.month) from agg a), '[]'::jsonb)
  );
$$;

create or replace function analytics.hr_infraction_detail(p_emp_id text, p_track text, p_from date, p_to date)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference, app_hr
as $$
  with rows_in as (
    select v.work_date, v.task_did, v.submitted_on_et, v.clock_in_et,
           v.shift_time_in_pht, v.filing_lag_hours, v.clock_in_late_minutes
    from analytics.v_hr_report_review v
    where v.emp_id = p_emp_id
      and v.work_date between p_from and p_to
      and v.task_status <> 'cancelled'
      and case when p_track = 'filing'    then v.is_late_filing
               when p_track = 'tardiness' then v.clock_in_late_minutes > 30
               else false end
  ), exc as (
    select distinct on (task_did) task_did, action, reason_category, note, created_by, created_at
    from app_hr.hr_infraction_excusal
    where track = p_track and emp_id = p_emp_id
    order by task_did, created_at desc, id desc
  ), items as (
    select r.work_date, r.task_did, r.submitted_on_et, r.clock_in_et,
           r.shift_time_in_pht, r.filing_lag_hours, r.clock_in_late_minutes,
           coalesce(e.action = 'excused', false)                       as excused,
           case when e.action = 'excused' then e.reason_category end   as excusal_reason_category,
           case when e.action = 'excused' then e.note end              as excusal_note,
           case when e.action = 'excused' then e.created_by end        as excusal_actor,
           case when e.action = 'excused' then e.created_at end        as excusal_at
    from rows_in r left join exc e using (task_did)
  )
  select jsonb_build_object(
    'items', coalesce((select jsonb_agg(to_jsonb(i) order by i.work_date) from items i), '[]'::jsonb)
  );
$$;

revoke all on function analytics.hr_infraction_months(date, date) from public, anon, authenticated;
grant execute on function analytics.hr_infraction_months(date, date) to service_role;
revoke all on function analytics.hr_infraction_detail(text, text, date, date) from public, anon, authenticated;
grant execute on function analytics.hr_infraction_detail(text, text, date, date) to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','hr_infraction_months',
   'Function(p_from date, p_to date): per (emp_id, month, track) unexcused_count + excused_count of habitual-clause infractions. track = filing (report filed >48h after first clock-in, is_late_filing, cancelled excluded; never-filed reports do not count) or tardiness (clock_in_late_minutes > 30, i.e. past the 30-minute grace, cancelled excluded). Excusal state = latest app_hr.hr_infraction_excusal action per (track, task_did).',
   'Feeds the Ontel People HR Infraction Monitor (/hr/infractions) and its dashboard card; the app-side rules engine applies the habitual thresholds (5+ per calendar month, or 8 (filing) / 10 (tardiness) across any 3 consecutive calendar months).',
   ARRAY['analytics.v_hr_report_review','app_hr.hr_infraction_excusal']),
  ('analytics','hr_infraction_detail',
   'Function(p_emp_id, p_track, p_from, p_to): individual infraction rows (work_date, task_did, submitted_on_et, clock_in_et, shift_time_in_pht, filing_lag_hours, clock_in_late_minutes) with latest-wins excusal state (excused, excusal_reason_category, excusal_note, excusal_actor, excusal_at) for the Infraction Monitor drill-down drawer.',
   'Drill-down for one employee + track in the Ontel People Infraction Monitor; excuse/reinstate actions write app_hr.hr_infraction_excusal.',
   ARRAY['analytics.v_hr_report_review','app_hr.hr_infraction_excusal'])
ON CONFLICT DO NOTHING;
