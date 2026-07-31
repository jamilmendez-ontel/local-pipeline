-- 206: analytics.v_approver_options -- the DR Approval "Approver" filter options.
--
-- The Approver filter listed only curated GROUP queues from
-- reference.ref_approver_group (include=true). But an assigned_approver on a
-- report can be an individual lead's NAME, not a group (see migration 159): e.g.
-- Hjalmark's entry is assigned to "Akira Nakaegawa". Those person-name queues
-- had no ref_approver_group row, so they never appeared as filter options even
-- though reports are assigned to them. Reported by Jamil.
--
-- Fix: source the options from the assigned_approver values actually in the data
-- (analytics.v_daily_report_approvals), while still honoring the deliberate junk
-- curation: ref_approver_group.include=false labels (duplicate/variant queues
-- like "CG2 - AT&T/DISH Team", "Quality and Process Improvement (QPI)", cluster
-- labels) stay hidden. Curated groups keep their friendly display_label; a
-- person-name queue with no ref row shows its raw name. A future stray label can
-- be hidden by adding an include=false row, and a future person-name approver
-- appears automatically.
--
-- Label-collision rule: when a non-curated raw value shares a label with a
-- curated group (observed: a stray bare "Accounting", 1 report, vs the real
-- "Daily Report Approvers - Accounting" whose display_label is "Accounting"),
-- keep only the curated entry so the dropdown has no confusing duplicate.
create or replace view analytics.v_approver_options as
  with data_vals as (
    select distinct assigned_approver as value
    from analytics.v_daily_report_approvals
    where assigned_approver is not null
      and length(btrim(assigned_approver)) > 0
      and assigned_approver <> '(no approver assigned)'
  ),
  joined as (
    select d.value,
           coalesce(g.display_label, d.value) as label,
           (g.group_label is not null) as curated
    from data_vals d
    left join reference.ref_approver_group g on g.group_label = d.value
    where coalesce(g.include, true) = true   -- drop only the explicitly-excluded junk
  )
  select j.value, j.label
  from joined j
  where j.curated
     or not exists (select 1 from joined c where c.curated and c.label = j.label)
  order by j.label;

revoke all on analytics.v_approver_options from anon, authenticated;
grant select on analytics.v_approver_options to service_role;

-- Semantic-layer metadata for the AI agent.
delete from agent.schema_metadata
 where schema_name = 'analytics' and table_name = 'v_approver_options' and column_name is null;
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
values
  ('analytics', 'v_approver_options',
   'View: the DR Approval "Approver" filter options. Distinct assigned_approver values present in v_daily_report_approvals, minus reference.ref_approver_group.include=false junk, minus "(no approver assigned)". Columns: value (the assigned_approver to filter on), label (ref_approver_group.display_label when curated, else the raw name). Non-curated values whose label collides with a curated group are dropped. Includes individual-lead name queues (e.g. "Akira Nakaegawa") that have no ref_approver_group row.',
   'Backs the ontel-people DR Approval Approver filter (top bar + column header, ag= param), replacing the ref_approver_group-only source that hid person-name approver queues.',
   array['analytics.v_daily_report_approvals', 'reference.ref_approver_group']::text[]);

notify pgrst, 'reload schema';
