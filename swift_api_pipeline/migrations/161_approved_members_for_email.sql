-- 161: analytics.approved_members_for_email(p_email) -- the distinct people an
-- app user has PERSONALLY approved, keyed off approval history.
--
-- Why this exists: the Home "Team members" tile used to scope on the carrier
-- group(s) an approver's scorecard rows map to. That undercounts a person-named
-- approver whose reportees span multiple carrier groups: the approver_scorecard
-- RPC returns carrier_group = NULL for a person-named bucket (e.g. "Merjien
-- Lara" covers people in both DA and HR), so the tile silently dropped them and
-- showed only the approver's carrier-backed queues. Merjien read 10 (QPI 7 +
-- Accounting 3) while she has actually approved 11 distinct people -- Orville
-- (HR) and Hajie (DA) among them -- were invisible.
--
-- The fix: define "Team members" for an approver as the DISTINCT employees whose
-- daily reports they have approved, regardless of queue/carrier group. This
-- mirrors analytics.approver_groups_for_email (migration 160) exactly for
-- identity resolution:
--   * match approved_by (a Swift display name) to the directory on
--     nickname/first + last name (approved_by carries no id and is "First Last",
--     while the directory full_name may include a middle name);
--   * refuse to attribute anything if the name is shared by another ACTIVE
--     employee (collision guard) -- under-attribute rather than mis-attribute;
--   * window approved_by to the trailing 180 days (PHT) so rotated-out ownership
--     drops off, consistent with owned-group resolution.
-- It additionally JOINs v_employee_directory so it only returns emp_ids that
-- resolve to a directory row -- this keeps the tile's count in lockstep with the
-- /directory?approver= list it links to.
CREATE OR REPLACE FUNCTION analytics.approved_members_for_email(p_email text)
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
  -- ambiguous and we cannot safely attribute approvals from approved_by strings.
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
  SELECT DISTINCT a.emp_id
  FROM me, ambiguous, analytics.v_daily_report_approvals a
  JOIN analytics.v_employee_directory d2 ON d2.emp_id = a.emp_id
  WHERE ambiguous.collisions = 0
    AND a.approved_by IS NOT NULL
    AND a.emp_id IS NOT NULL
    AND a.work_date >= ((now() AT TIME ZONE 'Asia/Manila')::date - 180)
    AND (lower(a.approved_by) = me.nick_key OR lower(a.approved_by) = me.first_key);
$$;

REVOKE ALL ON FUNCTION analytics.approved_members_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approved_members_for_email(text) TO service_role;

-- Semantic-layer metadata for the AI agent.
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'approved_members_for_email' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approved_members_for_email',
   'Function(p_email text) -> setof text: the distinct emp_ids of employees a given app user has PERSONALLY approved daily reports for in the trailing 180 days, derived from approval history (v_daily_report_approvals.approved_by matched to v_employee_directory by nickname/first + last name, joined back to the directory so only resolvable emp_ids return). Returns nothing when the name is shared by another active employee (collision guard). STABLE.',
   'Backs the ontel-people Home page "Team members" tile and its /directory?approver= drill-down: an approver''s team = the distinct people they actually approve, not the carrier-group roster (which undercounts person-named approvers whose reportees span multiple carrier groups). Windowed to 180 days and refused when ambiguous by name, mirroring approver_groups_for_email.',
   ARRAY['analytics.v_daily_report_approvals','analytics.v_employee_directory']::text[]);

NOTIFY pgrst, 'reload schema';
