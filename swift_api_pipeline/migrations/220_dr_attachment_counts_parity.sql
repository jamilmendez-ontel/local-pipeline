-- 220: dr_attachment_counts parity with applyBrowseFilters (2026-08-04).
-- Pre-merge review of the stated-hours filter (ontel-people PR #57) caught that
-- the DR Approval "Attachments (ZIP)" confirm-modal counts run through this
-- aggregate RPC (migration 181) with hand-built args, NOT applyBrowseFilters,
-- so two browse filters were silently ignored by the counts while the actual
-- ZIP download honored them:
--   * the NEW stated-hours range (ontel-people shMin/shMax, this feature), and
--   * the day-of-week filter (p_dows), missed ever since work_dow landed in
--     migration 200 -- a pre-existing gap of the same parity class.
-- Mismatch made the modal's file/entry counts (and the ZIP_MAX_FILES gate)
-- describe a different set than the download collects.
--
-- dr_attachment_counts gains four APPENDED, DEFAULTED params:
--   p_dows int[]           -- keep rows with work_dow = any(...) (0=Sun..6=Sat)
--   p_hours_min numeric    -- keep rows with RAW total_hours >= min
--   p_hours_max numeric    -- keep rows with RAW total_hours <= max
--   p_require_hours bool   -- keep rows with total_hours is not null
-- Hours params are RAW total_hours bounds: the app maps the user's NET range
-- (net of the 1h break) through the unit-tested statedNetToRawBounds helper
-- (lib/hr/domain/stated-hours-filter.ts) so the net->raw mapping lives in ONE
-- place; SQL stays a dumb range. All default to no-op, so existing callers are
-- unaffected.
--
-- Old signature DROPped first: appending a defaulted param otherwise creates
-- an overload PostgREST can't disambiguate (same reason as 189/198/219).
-- Base definition: live pg_get_functiondef captured 2026-08-04, verbatim
-- except the four params + three predicates.
-- Rollback: DROP the 13-arg signature and re-create the 9-arg body from 181.

DROP FUNCTION IF EXISTS analytics.dr_attachment_counts(text, text, text[], text, text, text[], date, date, text[]);

CREATE FUNCTION analytics.dr_attachment_counts(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_division text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_statuses text[] DEFAULT NULL::text[],
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_approvers text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_hours_min numeric DEFAULT NULL::numeric,
  p_hours_max numeric DEFAULT NULL::numeric,
  p_require_hours boolean DEFAULT false
)
 RETURNS TABLE(entries bigint, files bigint)
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging'
AS $function$
  select count(distinct h.task_did)                       as entries,
         coalesce(sum(h.file_uploaded_count), 0)::bigint  as files
  from analytics.v_daily_report_approvals v
  join data_staging.stg_daily_report_hours h
    on h.task_did = v.task_did
   and h.file_uploaded_count > 0
  where (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_division is null or v.division = p_division)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_status is null or v.task_status = p_status)
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_approvers is null or v.assigned_approver = any(p_approvers))
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_hours_min is null or v.total_hours >= p_hours_min)
    and (p_hours_max is null or v.total_hours <= p_hours_max)
    and (not p_require_hours or v.total_hours is not null);
$function$;
