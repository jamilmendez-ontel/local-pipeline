-- 181: fast attachment counts for the Ontel DRMC "Attachments (ZIP)" extract
-- (Jamil 2026-07-14). The confirm modal and the batch route's over-cap
-- pre-check need {entries with files, total files} for the CURRENT browse
-- filter. The app previously resolved the whole filtered set through
-- PostgREST (paged rows + 200-did requirement chunks) just to add numbers,
-- which took many seconds on wide filters; this RPC answers in one
-- aggregate. Filter predicates mirror applyBrowseFilters in
-- ontel-people lib/hr/queries/approval-queries.ts EXACTLY (single-value
-- legacy params AND array params, same as the 180 RPCs; the app sends one
-- style per call). p_search arrives pre-sanitized by the app (sanitizeSearch
-- keeps [\w '-] only), same contract as approval_queue_summary.

CREATE FUNCTION analytics.dr_attachment_counts(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_division text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_statuses text[] DEFAULT NULL::text[],
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_approvers text[] DEFAULT NULL::text[]
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
    and (p_approvers is null or v.assigned_approver = any(p_approvers));
$function$;

REVOKE ALL ON FUNCTION analytics.dr_attachment_counts(text, text, text[], text, text, text[], date, date, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.dr_attachment_counts(text, text, text[], text, text, text[], date, date, text[]) TO service_role;
