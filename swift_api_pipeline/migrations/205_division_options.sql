-- 205: analytics.v_division_options -- the canonical list of divisions
-- (carrier_group) for the DR Approval / DR Monitoring "Division" filter.
--
-- The filter options were sourced from app_hr.hr_group (a curated table that
-- drifted from reality): it was missing real divisions that appear on reports
-- (IT, Admin-Ops Mgmt, CG3 - TMO/FTTH, R&D Mgmt, TS-PM, and "Creative" vs the
-- stale "Creatives") and still listed dead ones (Admin and Operations,
-- Creatives, PHDSM). Reported by Jamil: "IT" was missing.
--
-- carrier_group is a roster/directory attribute -- every daily report inherits
-- it from the filing employee -- so v_employee_directory is its authoritative
-- source. The distinct set here matches the distinct carrier_group present in
-- both analytics.v_daily_report_approvals and analytics.v_hr_report_review
-- exactly (verified on prod 2026-07-31), so the options now mirror what is
-- actually in the data. No is_active filter: an inactive/former employee's
-- division must still be selectable while their historical reports exist.
create or replace view analytics.v_division_options as
  select distinct carrier_group as division
  from analytics.v_employee_directory
  where carrier_group is not null
    and length(btrim(carrier_group)) > 0
  order by 1;

revoke all on analytics.v_division_options from anon, authenticated;
grant select on analytics.v_division_options to service_role;

-- Semantic-layer metadata for the AI agent.
delete from agent.schema_metadata
 where schema_name = 'analytics' and table_name = 'v_division_options' and column_name is null;
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
values
  ('analytics', 'v_division_options',
   'View: the distinct, non-blank division labels (carrier_group) present in the roster (v_employee_directory, all employees incl. inactive), sorted. One column: division.',
   'Backs the ontel-people DR Approval and DR Monitoring "Division" column-header filter options, replacing the drifted app_hr.hr_group list so the choices mirror the carrier_group values actually in the report data (e.g. IT was missing before).',
   array['analytics.v_employee_directory']::text[]);

notify pgrst, 'reload schema';
