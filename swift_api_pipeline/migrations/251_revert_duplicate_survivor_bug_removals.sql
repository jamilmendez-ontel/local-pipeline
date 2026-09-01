-- 251: restore the timer entries the duplicate-survivor bug auto-removed.
--
-- _resolve_duplicate_for_action(action="remove") picked the survivor of a
-- 3+ duplicate group by LATEST end_time (= the longest runaway snapshot) and
-- wrote 'auto_resolved_sibling' removals for the member's real session. The
-- member then removed the runaway survivor and the whole group went to zero.
-- Code fix ships in the same branch (survivor = shortest alive snapshot).
--
-- Restore rule (2026-08-31, approved by Jamil): in groups that are (a) fully
-- absent from stg_timer_activities_clean, (b) resolved_by='correction'
-- (the bug's path) since 2026-07-30, and (c) have NO correction on the same
-- natural key (a corrected replacement means the zero was intended), revert
-- EXACTLY the removals the bug wrote (reason='auto_resolved_sibling') and
-- re-point the review's selected_entry at the restored snapshot. Member-made
-- removals (reason NULL / free text) are never touched. 14 rows, 14 groups,
-- 10 members, 2,455.29 min (~40.9h). Restored entries resurface in the next
-- daily email with Edit/Remove buttons, so a member who truly wanted the
-- session gone can remove it for real.
--
-- Worklist (group_id -> restored label · member · minutes):
--   809fe6078b07 A mark.manalac 22.77    | 57a2b9defa08 A earl.tolentino 63.83
--   1628b3d57a0f A mark.manalac 35.04    | 8c0827fb4278 A madel 138.39
--   a320ce575496 C ernie.catalan 51.28   | 24f30c158a46 B lenard 152.74
--   b0699e72d484 A czarina 124.02        | 03230e7817f5 A mark.manalac 72.32
--   a29480e582c5 H mark.manalac 74.50    | 34e1f8d04544 A nadine.fortin 358.12
--   7d4332c19aaf A nadine.fortin 612.55  | 6ecb29a7341b A nadine.fortin 631.10
--   b007f81ea085 A mark.manalac 53.40    | e75a662e5c95 B erika.ramirez 65.23
--
-- After applying, run SELECT data_staging.rebuild_timer_clean(); and verify
-- each restored key has a clean row (erika's group ends with 2: her separate
-- 16-min session was never excluded thanks to the anchor-start join).

-- ---------------------------------------------------------------------------
-- 0) Preflight: the 14 target removals must exist exactly as expected.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int;
BEGIN
  SELECT COUNT(*) INTO n
  FROM (VALUES
    ('809fe6078b07','A'), ('57a2b9defa08','A'), ('1628b3d57a0f','A'),
    ('8c0827fb4278','A'), ('a320ce575496','C'), ('24f30c158a46','B'),
    ('b0699e72d484','A'), ('03230e7817f5','A'), ('a29480e582c5','H'),
    ('34e1f8d04544','A'), ('7d4332c19aaf','A'), ('6ecb29a7341b','A'),
    ('b007f81ea085','A'), ('e75a662e5c95','B')
  ) m(group_id, keep_label)
  JOIN app_timer.duplicate_reviews r
    ON r.group_id = m.group_id
   AND r.status = 'resolved'
   AND r.resolved_by = 'correction'
  JOIN LATERAL jsonb_array_elements(r.entries) e
    ON e->>'label' = m.keep_label
  JOIN app_timer.entry_removals rm
    ON rm.project_did = r.project_did
   AND rm.user_email  = r.user_email
   AND rm.start_time  = COALESCE((e->>'start_time')::timestamptz, r.start_time)
   AND rm.site_name IS NOT DISTINCT FROM r.site_name
   AND rm.site_id   IS NOT DISTINCT FROM r.site_id
   AND rm.task      IS NOT DISTINCT FROM r.task
   AND rm.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
   AND rm.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
   AND rm.reason = 'auto_resolved_sibling';
  IF n <> 14 THEN
    RAISE EXCEPTION '251: expected 14 auto_resolved_sibling target removals, found % — re-verify before applying', n;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) Revert the bug's removals (REVERTED rows are skipped by every
--    rebuild_timer_clean anti-join; precedent: migration 042).
-- ---------------------------------------------------------------------------
UPDATE app_timer.entry_removals rm
SET reason = 'REVERTED',
    updated_at = NOW()
FROM (VALUES
    ('809fe6078b07','A'), ('57a2b9defa08','A'), ('1628b3d57a0f','A'),
    ('8c0827fb4278','A'), ('a320ce575496','C'), ('24f30c158a46','B'),
    ('b0699e72d484','A'), ('03230e7817f5','A'), ('a29480e582c5','H'),
    ('34e1f8d04544','A'), ('7d4332c19aaf','A'), ('6ecb29a7341b','A'),
    ('b007f81ea085','A'), ('e75a662e5c95','B')
) m(group_id, keep_label)
JOIN app_timer.duplicate_reviews r ON r.group_id = m.group_id
JOIN LATERAL jsonb_array_elements(r.entries) e ON e->>'label' = m.keep_label
WHERE rm.project_did = r.project_did
  AND rm.user_email  = r.user_email
  AND rm.start_time  = COALESCE((e->>'start_time')::timestamptz, r.start_time)
  AND rm.site_name IS NOT DISTINCT FROM r.site_name
  AND rm.site_id   IS NOT DISTINCT FROM r.site_id
  AND rm.task      IS NOT DISTINCT FROM r.task
  AND rm.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
  AND rm.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
  AND rm.reason = 'auto_resolved_sibling';

-- ---------------------------------------------------------------------------
-- 2) Re-point each review at the restored snapshot so rejected_entries no
--    longer excludes it (rejected exclusion is independent of entry_removals).
-- ---------------------------------------------------------------------------
UPDATE app_timer.duplicate_reviews r
SET selected_entry = m.keep_label,
    rejected_entries = (
      SELECT jsonb_agg(jsonb_build_object(
               'end_time', e->'end_time',
               'duration_min', e->'duration_min'))
      FROM jsonb_array_elements(r.entries) e
      WHERE e->>'label' <> m.keep_label
    ),
    resolved_by = 'survivor_bug_restore',
    updated_at = NOW()
FROM (VALUES
    ('809fe6078b07','A'), ('57a2b9defa08','A'), ('1628b3d57a0f','A'),
    ('8c0827fb4278','A'), ('a320ce575496','C'), ('24f30c158a46','B'),
    ('b0699e72d484','A'), ('03230e7817f5','A'), ('a29480e582c5','H'),
    ('34e1f8d04544','A'), ('7d4332c19aaf','A'), ('6ecb29a7341b','A'),
    ('b007f81ea085','A'), ('e75a662e5c95','B')
) m(group_id, keep_label)
WHERE r.group_id = m.group_id;

-- ---------------------------------------------------------------------------
-- ROLLBACK: set reason back to 'auto_resolved_sibling' on the 14 rows and
-- restore each review's previous selected_entry/rejected_entries (values are
-- reconstructable from entries minus the pre-251 selected label; resolved_by
-- was 'correction'). Then rebuild_timer_clean().
-- ---------------------------------------------------------------------------
