-- 144: Aggregate the awaiting-approval queue for the /approvals overview so the
-- app stops fetching ~1,250 rows just to compute 5 KPIs + the group backlog.
-- Reproduces lib/hr/domain/approval-metrics.ts summarizeQueue + groupBacklog.
-- Same filters + is_awaiting_approval source as getApprovalQueue.
create or replace function analytics.approval_queue_summary(
  p_carrier_group text default null,
  p_division text default null,
  p_search text default null
) returns jsonb
language sql
stable
security invoker
set search_path = analytics, public
as $$
  with q as (
    select assigned_approver, no_approver_flag, pending_wait_days
    from analytics.v_daily_report_approvals
    where is_awaiting_approval = true
      and (p_carrier_group is null or carrier_group = p_carrier_group)
      and (p_division is null or division = p_division)
      and (p_search is null or employee_name ilike '%'||p_search||'%' or emp_id ilike '%'||p_search||'%')
  ),
  tagged as (
    select coalesce(assigned_approver, '(unassigned)') as grp,
           no_approver_flag,
           coalesce(pending_wait_days, 0) as w,
           case when coalesce(pending_wait_days,0) between 4 and 7 then 'amber'
                when coalesce(pending_wait_days,0) > 7 then 'red' else 'ok' end as bucket
    from q
  ),
  backlog as (
    select grp as "group",
           count(*) as waiting,
           count(*) filter (where bucket='amber') as amber,
           count(*) filter (where bucket='red') as red
    from tagged group by grp order by count(*) desc
  )
  select jsonb_build_object(
    'kpis', jsonb_build_object(
      'awaiting', (select count(*) from tagged),
      'amber',    (select count(*) from tagged where bucket='amber'),
      'red',      (select count(*) from tagged where bucket='red'),
      'oldest_wait_days', (select coalesce(max(w),0) from tagged),
      'no_approver', (select count(*) from tagged where no_approver_flag)
    ),
    'backlog', (select coalesce(jsonb_agg(jsonb_build_object(
                  'group', "group", 'waiting', waiting, 'amber', amber, 'red', red)
                  order by waiting desc), '[]'::jsonb) from backlog)
  );
$$;

revoke all on function analytics.approval_queue_summary(text, text, text) from public, anon, authenticated;
grant execute on function analytics.approval_queue_summary(text, text, text) to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approval_queue_summary',
   'Function(p_carrier_group,p_division,p_search): KPIs (awaiting, amber, red, oldest_wait_days, no_approver) + per-approver-group backlog counts for the awaiting-approval queue, backlog ordered waiting DESC. Same filters and is_awaiting_approval source as the app''s getApprovalQueue.',
   'Powers the HR /approvals overview so the app computes 5 KPIs + group backlog without fetching the full ~1,250-row awaiting-approval queue client-side.',
   ARRAY['analytics.v_daily_report_approvals'])
ON CONFLICT DO NOTHING;
