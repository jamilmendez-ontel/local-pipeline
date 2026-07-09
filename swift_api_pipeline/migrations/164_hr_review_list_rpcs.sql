-- 164: Report Review list/count/chip RPCs. The app's multi-flag Review path
-- (lib/hr/queries/review-queries.ts) previously fetched EVERY row per selected
-- flag through PostgREST 1000-row pages, then merged/deduped/sorted in Node
-- just to serve one 100-row scroll page or one count. The view is one row per
-- task_did, so the flag union is a plain OR here; these three functions make
-- each list page, exact count, and the 5-chip count strip a single round trip.
--
-- Hardening block per migration 147 (language sql stable, security invoker,
-- explicit search_path, revoke-then-grant-service_role).
--
-- Variance thresholds are SQL copies of lib/hr/domain/review-signals.ts
-- (VARIANCE_AMBER gap 1 / coverage 90, VARIANCE_RED gap 2 / coverage 75),
-- same precedent as 147's red_variance KPI. Keep in sync with that file.
--
-- Sort contract mirrors the app's applyOrder/compareReviewRows exactly:
-- whitelisted sort column first (NULLS LAST in both directions), then
-- work_date DESC, then task_did ASC as the stable tiebreaker. A plain
-- work_date sort honors p_sort_dir; unknown p_sort_key = default ordering.

create or replace function analytics.hr_review_page(
  p_search        text    default null,
  p_carrier_group text    default null,
  p_status        text    default null,
  p_date_from     date    default null,
  p_date_to       date    default null,
  p_flags         text[]  default '{}',
  p_sort_key      text    default null,
  p_sort_dir      text    default 'desc',
  p_offset        integer default 0,
  p_limit         integer default 100
)
returns setof analytics.v_hr_report_review
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  select v.*
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_status is null or v.task_status = p_status)
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.variance_hours > 1 and v.coverage_pct < 90)
      or ('variance_red' = any(p_flags) and v.variance_hours > 2 and v.coverage_pct < 75)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_dir = 'asc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
      end
    end desc nulls last,
    case when p_sort_key = 'work_date' and p_sort_dir = 'asc' then v.work_date end asc,
    case when p_sort_key is distinct from 'work_date' or p_sort_dir <> 'asc' then v.work_date end desc,
    v.task_did asc
  offset greatest(coalesce(p_offset, 0), 0)
  limit  least(greatest(coalesce(p_limit, 100), 0), 1000)
$$;

create or replace function analytics.hr_review_count(
  p_search        text   default null,
  p_carrier_group text   default null,
  p_status        text   default null,
  p_date_from     date   default null,
  p_date_to       date   default null,
  p_flags         text[] default '{}'
)
returns bigint
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  select count(*)
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_status is null or v.task_status = p_status)
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.variance_hours > 1 and v.coverage_pct < 90)
      or ('variance_red' = any(p_flags) and v.variance_hours > 2 and v.coverage_pct < 75)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
    )
$$;

-- Chip counts: the 5 visible FlagChips under the BASE filters only (chips show
-- what is available to add/remove, not the currently selected flag set).
-- variance_red is link-only and intentionally absent.
create or replace function analytics.hr_review_chip_counts(
  p_search        text default null,
  p_carrier_group text default null,
  p_status        text default null,
  p_date_from     date default null,
  p_date_to       date default null
)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  select jsonb_build_object(
    'late',       count(*) filter (where v.is_late_filing),
    'missing',    count(*) filter (where v.is_missing_report),
    'variance',   count(*) filter (where v.variance_hours > 1 and v.coverage_pct < 90),
    'open_timer', count(*) filter (where v.open_timer_count > 0),
    'no_time_in', count(*) filter (where not v.has_time_in and v.submitted_on_et is not null)
  )
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_status is null or v.task_status = p_status)
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
$$;

revoke all on function analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer) from public, anon, authenticated;
grant execute on function analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer) to service_role;

revoke all on function analytics.hr_review_count(text, text, text, date, date, text[]) from public, anon, authenticated;
grant execute on function analytics.hr_review_count(text, text, text, date, date, text[]) to service_role;

revoke all on function analytics.hr_review_chip_counts(text, text, text, date, date) from public, anon, authenticated;
grant execute on function analytics.hr_review_chip_counts(text, text, text, date, date) to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','hr_review_page',
   'Function(p_search, p_carrier_group, p_status, p_date_from, p_date_to, p_flags text[], p_sort_key, p_sort_dir, p_offset, p_limit): one page of analytics.v_hr_report_review rows with base filters ANDed and flag predicates (late, missing, variance amber, variance_red, open_timer, no_time_in) OR-combined; whitelisted sort column first (NULLS LAST), then work_date DESC, task_did ASC.',
   'Powers the Ontel People Report Review table (initial load, infinite scroll, and CSV export pagination) as one round trip; replaced the app-side per-flag fetch/merge/sort union.',
   ARRAY['analytics.v_hr_report_review']),
  ('analytics','hr_review_count',
   'Function(p_search, p_carrier_group, p_status, p_date_from, p_date_to, p_flags text[]): exact count of v_hr_report_review rows matching the base filters with flag predicates OR-combined (the view is one row per task_did, so OR equals the union count).',
   'Powers the Report Review result count and the home page late/missing tiles as one round trip.',
   ARRAY['analytics.v_hr_report_review']),
  ('analytics','hr_review_chip_counts',
   'Function(p_search, p_carrier_group, p_status, p_date_from, p_date_to): jsonb {late, missing, variance, open_timer, no_time_in} counts under the base filters only, one scan with count FILTER per flag. variance thresholds are SQL copies of lib/hr/domain/review-signals.ts (amber gap 1 / coverage 90).',
   'Powers the Report Review FlagChips count strip; replaced 6 parallel exact-count queries with one.',
   ARRAY['analytics.v_hr_report_review'])
ON CONFLICT DO NOTHING;
