-- 221: position column filter for Ontel DRMC "DR Monitoring" (Jamil 2026-08-05).
-- The app gains an opt-in "Position" column (column picker "More" section) with
-- a multiselect header filter so HR can keep only e.g. the Technical Analyst /
-- Technical Associate / Project Associate variants. v_hr_report_review has
-- carried "position" since before the 207 materialization, and hr_review_page
-- RETURNS SETOF the view, so the DATA already flows; this migration only adds
-- the server-side predicate + the filter's options source.
--
-- hr_review_page / hr_review_count gain one APPENDED, DEFAULTED param:
--   p_positions text[]  -- keep rows whose "position" is in the list
-- NULL = no filter. Rows with a NULL position never match a set filter (=ANY
-- is null-rejecting), same convention as the carrier_groups multiselect. ANDed
-- with the existing base filters.
--
-- Old signatures are DROPped first: appending a defaulted param otherwise
-- creates an overload PostgREST can't disambiguate (same reason as 189/198/219).
--
-- Base definitions: live pg_get_functiondef captured 2026-08-05 (post-219),
-- verbatim except the new param + predicate. "position" is quoted throughout:
-- POSITION is a reserved SQL keyword.
--
-- Also adds analytics.v_position_options, the options source for the header
-- multiselect, mirroring v_division_options (205): the distinct non-blank
-- position values actually present in the roster (v_employee_directory, all
-- employees incl. inactive so historical reports stay filterable).
--
-- Rollback: DROP both new signatures (17+1 / 13+1 args), re-create from the
-- bodies below minus p_positions + its predicate; DROP VIEW
-- analytics.v_position_options; DELETE its agent.schema_metadata row.

DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[], integer, integer, numeric, numeric);
DROP FUNCTION IF EXISTS analytics.hr_review_count(text, text, text, date, date, text[], text[], text[], integer[], integer, integer, numeric, numeric);

CREATE FUNCTION analytics.hr_review_page(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_sort_key text DEFAULT NULL::text,
  p_sort_dir text DEFAULT 'desc'::text,
  p_offset integer DEFAULT 0,
  p_limit integer DEFAULT 100,
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer,
  p_stated_min numeric DEFAULT NULL::numeric,
  p_stated_max numeric DEFAULT NULL::numeric,
  p_positions text[] DEFAULT NULL::text[]
)
 RETURNS SETOF analytics.v_hr_report_review
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select v.*
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_positions is null or v."position" = any(p_positions))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (p_stated_min is null or (v.stated_hours_net is not null and v.stated_hours_net >= p_stated_min))
    and (p_stated_max is null or (v.stated_hours_net is not null and v.stated_hours_net <= p_stated_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'asc'  then v.clock_in_et   end asc  nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'desc' then v.clock_in_et   end desc nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'asc'  then v.first_task_start end asc  nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'desc' then v.first_task_start end desc nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'asc'  then v.last_task_end    end asc  nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'desc' then v.last_task_end    end desc nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'asc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end asc  nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'desc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end desc nulls last,
    case when p_sort_dir = 'asc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end desc nulls last,
    case when p_sort_key = 'work_date' and p_sort_dir = 'asc' then v.work_date end asc,
    case when p_sort_key is distinct from 'work_date' or p_sort_dir <> 'asc' then v.work_date end desc,
    v.task_did asc
  offset greatest(coalesce(p_offset, 0), 0)
  limit  least(greatest(coalesce(p_limit, 100), 0), 1000)
$function$;

CREATE FUNCTION analytics.hr_review_count(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer,
  p_stated_min numeric DEFAULT NULL::numeric,
  p_stated_max numeric DEFAULT NULL::numeric,
  p_positions text[] DEFAULT NULL::text[]
)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select count(*)
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_positions is null or v."position" = any(p_positions))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (p_stated_min is null or (v.stated_hours_net is not null and v.stated_hours_net >= p_stated_min))
    and (p_stated_max is null or (v.stated_hours_net is not null and v.stated_hours_net <= p_stated_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
$function$;

-- Options source for the header multiselect (mirror of v_division_options, 205).
create or replace view analytics.v_position_options as
  select distinct "position"
  from analytics.v_employee_directory
  where "position" is not null
    and length(btrim("position")) > 0
  order by 1;

revoke all on analytics.v_position_options from anon, authenticated;
grant select on analytics.v_position_options to service_role;

-- Semantic-layer metadata for the AI agent. WHERE NOT EXISTS, not ON CONFLICT
-- (NULL column_name never dedupes under the unique key — 2026-06-22 gotcha).
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
select
  'analytics', 'v_position_options',
  'View: the distinct, non-blank position titles present in the roster (v_employee_directory, all employees incl. inactive), sorted. One column: position.',
  'Backs the ontel-people DR Monitoring "Position" column-header filter options (e.g. Technical Analyst I/II/III, Technical Associate, Project Associate), same pattern as v_division_options.',
  array['analytics.v_employee_directory']::text[]
where not exists (
  select 1 from agent.schema_metadata
  where schema_name = 'analytics' and table_name = 'v_position_options'
    and column_name is null
);

notify pgrst, 'reload schema';
