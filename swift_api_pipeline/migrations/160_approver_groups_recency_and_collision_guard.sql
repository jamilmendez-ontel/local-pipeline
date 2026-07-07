-- 160: Harden analytics.approver_groups_for_email (from migration 159) with a
-- recency window and a name-collision guard. Follow-up to an expert data review.
--
-- Two risks in 159's history-derived mapping:
--
-- (1) NO RECENCY WINDOW. 159 scanned ALL approval history, so a lead who moved
--     off a queue months ago "owned" it forever. Window approved_by to the
--     trailing 180 days (PHT). Verified on prod 2026-07-07: 96% of the 12,949
--     approvals fall inside 180 days and all four current approvers
--     (tan->CG3, orville->3, hajie->DA&R, merjien->6) map identically at 180d
--     as all-time, so this only drops genuinely-rotated-out ownership.
--
-- (2) NAME-COLLISION MIS-ATTRIBUTION. 159 matched approved_by (a Swift display
--     name) to the directory purely on nickname/first + last name, with no
--     identity key. Two active employees sharing a name (plausible in a large
--     PH roster) would attribute one person's queues to the other -- one
--     employee seeing another group's operational metrics. Guard: if ANY other
--     ACTIVE employee shares this person's name key, the approved_by string is
--     ambiguous, so we refuse to attribute anything (under-attribute rather than
--     mis-attribute). Verified 2026-07-07: all 8 current app users resolve to a
--     unique active name, so the guard changes nothing for them today.
--
-- Also drops `public` from the search_path (all refs are analytics-qualified).
CREATE OR REPLACE FUNCTION analytics.approver_groups_for_email(p_email text)
RETURNS SETOF text
LANGUAGE sql
STABLE
SET search_path = analytics
AS $$
  WITH me AS (
    SELECT
      lower(coalesce(nickname, '')   || ' ' || coalesce(last_name, '')) AS nick_key,
      lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) AS first_key
    FROM analytics.v_employee_directory
    WHERE lower(email) = lower(p_email)
    LIMIT 1
  ),
  -- Count OTHER active employees sharing either name key. >0 means the name is
  -- ambiguous and we cannot safely attribute queues from approved_by strings.
  ambiguous AS (
    SELECT count(*) AS collisions
    FROM analytics.v_employee_directory d, me
    WHERE d.is_active
      AND lower(d.email) <> lower(p_email)
      AND (
           lower(coalesce(d.nickname, '')   || ' ' || coalesce(d.last_name, '')) = me.nick_key
        OR lower(coalesce(d.first_name, '') || ' ' || coalesce(d.last_name, '')) = me.first_key
      )
  )
  SELECT DISTINCT a.assigned_approver
  FROM me, ambiguous, analytics.v_daily_report_approvals a
  WHERE ambiguous.collisions = 0
    AND a.approved_by IS NOT NULL
    AND a.assigned_approver IS NOT NULL
    AND a.work_date >= ((now() AT TIME ZONE 'Asia/Manila')::date - 180)
    AND (lower(a.approved_by) = me.nick_key OR lower(a.approved_by) = me.first_key);
$$;

REVOKE ALL ON FUNCTION analytics.approver_groups_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_groups_for_email(text) TO service_role;

-- Refresh the semantic-layer metadata description for the hardened function.
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'approver_groups_for_email' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approver_groups_for_email',
   'Function(p_email text) -> setof text: the distinct assigned_approver queue labels a given app user has approved in the TRAILING 180 DAYS, derived from approval history (v_daily_report_approvals.approved_by matched to v_employee_directory by nickname/first + last name). Returns nothing when the name is shared by another active employee (collision guard) to avoid mis-attribution. STABLE.',
   'Backs the ontel-people Home page: scopes Pending-approvals and On-time-rate KPIs to the queues a user personally works recently. Ownership comes from who approved (not the queue label), is windowed to 180 days so rotated-out leads drop off, and is refused when ambiguous by name.',
   ARRAY['analytics.v_daily_report_approvals','analytics.v_employee_directory']::text[]);

NOTIFY pgrst, 'reload schema';
