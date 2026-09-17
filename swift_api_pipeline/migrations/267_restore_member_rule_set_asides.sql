-- 267: restore the real timer sessions the duplicate auto-collapse set aside.
--
-- Rule since 2026-09-17 (Jamil): only the member removes timer entries. The
-- resolver in timer_correction_review.py used to collapse a duplicate group to
-- ONE survivor after any member removal and write 'auto_resolved_sibling'
-- removals for every other copy. Overlap clustering is transitive, so two
-- runaway snapshots (e.g. 08:05-16:12) bridged two real, non-overlapping
-- sessions (08:05-09:32 and 14:43-15:13) into one group; when the member
-- removed the runaways the collapse then set aside a real session as a
-- "duplicate" of one it never overlapped (nath 2026-09-15).
--
-- Audit (2026-09-17, 32 non-REVERTED auto_resolved_sibling rows in total):
--   * 26 overlap a surviving clean row -> genuine copies of a counted timer,
--     left alone.
--   * 4 overlap nothing and their raw row still exists -> real sessions lost,
--     restored here (280.6 min, ~4.7h):
--       5385741b4aea lenard        2026-08-10 09:02-10:53  111.49 min  group 0a046b14b218
--       aba0fbae248d madel         2026-08-24 09:26-10:42   75.13 min  group 583cd993e78f
--       9cea3a79cfeb steph.familara 2026-09-09 17:49-17:57    7.18 min  group 467d6ea98bba
--       43fff6642fcf nath.parajas  2026-09-15 08:05-09:32   86.80 min  group b614c1665bf9
--   * 2 overlap nothing but their raw row is gone (inert), left alone.
--
-- Member-made removals (reason NULL / free text) are never touched. After
-- applying, run SELECT data_staging.rebuild_timer_clean(); and verify each
-- restored key has a clean row.

-- ---------------------------------------------------------------------------
-- 0) Preflight: the 4 target removals and their 4 groups must exist as expected.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int;
BEGIN
  SELECT COUNT(*) INTO n
  FROM app_timer.entry_removals
  WHERE entry_id IN ('5385741b4aea', 'aba0fbae248d', '9cea3a79cfeb', '43fff6642fcf')
    AND reason = 'auto_resolved_sibling';
  IF n <> 4 THEN
    RAISE EXCEPTION '267: expected 4 auto_resolved_sibling target removals, found % - re-verify before applying', n;
  END IF;

  SELECT COUNT(*) INTO n
  FROM app_timer.duplicate_reviews
  WHERE group_id IN ('0a046b14b218', '583cd993e78f', '467d6ea98bba', 'b614c1665bf9')
    AND status = 'resolved'
    AND resolved_by = 'correction';
  IF n <> 4 THEN
    RAISE EXCEPTION '267: expected 4 resolved-by-correction groups, found % - re-verify before applying', n;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) Revert the system set-asides (REVERTED rows are skipped by every
--    rebuild_timer_clean anti-join; precedent: migrations 042 and 251).
-- ---------------------------------------------------------------------------
UPDATE app_timer.entry_removals
SET reason = 'REVERTED',
    updated_at = NOW()
WHERE entry_id IN ('5385741b4aea', 'aba0fbae248d', '9cea3a79cfeb', '43fff6642fcf')
  AND reason = 'auto_resolved_sibling';

-- ---------------------------------------------------------------------------
-- 2) The system rejects nothing in these groups any more: rejected_entries is
--    an exclusion path independent of entry_removals, so clear it. The
--    member's own removals stay in entry_removals and keep excluding the
--    runaway copies.
-- ---------------------------------------------------------------------------
UPDATE app_timer.duplicate_reviews
SET rejected_entries = '[]'::jsonb,
    resolved_by = 'member_rule_restore',
    updated_at = NOW()
WHERE group_id IN ('0a046b14b218', '583cd993e78f', '467d6ea98bba', 'b614c1665bf9');

-- ---------------------------------------------------------------------------
-- ROLLBACK: set reason back to 'auto_resolved_sibling' on the 4 entry_ids and
-- rebuild each group's rejected_entries as every entry except selected_entry
-- (end_time + duration_min pairs); resolved_by was 'correction'. Then
-- rebuild_timer_clean().
-- ---------------------------------------------------------------------------
