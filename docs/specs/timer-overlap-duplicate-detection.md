# Timer Overlap-Based Duplicate Detection — Design

**Date:** 2026-05-12
**Project:** `local-pipeline/swift_api_pipeline/`
**Owner:** Jamil Mendez
**Status:** Approved, ready for implementation plan

---

## Background

The current timer duplicate detection system flags entries as duplicates only when they share an identical `(project_did, user_email, start_time, site_name, site_id, task)` tuple. This catches the common case where Swift returns the same timer twice with different `end_time` / `duration_min`, but misses a real-world pattern: a tech accidentally starts a second timer for the same task while the first is still running.

**Example:** Task 1, Timer A 09:30–11:30, Timer B 10:00–11:20. B starts inside A's window but with a different `start_time`. Today both rows land in `stg_timer_activities_clean` and double-count toward the tech's day.

This spec extends duplicate detection to flag any two entries with overlapping time ranges on the same task, so techs can resolve them through the existing Google Form workflow.

---

## Scope

In scope:
- Detection logic for overlapping timer entries on the same task.
- Stable `group_id` so existing pending reviews keep their Google Form threads.
- `rebuild_timer_clean()` update so the per-entry start_time inside the JSONB array is used for exclusion joins.
- One-time backfill of `start_time` into the JSONB `entries[]` array for existing review rows.
- Daily email DUPLICATE badge becomes overlap-aware (same badge, broader rule).

Out of scope:
- New UI badges (no separate OVERLAP badge — same DUPLICATE badge as today).
- Changes to the Google Form, Apps Script triggers, or `--apply` flow.
- Cross-task overlap detection (different tasks on the same site).
- Fixing the cross-midnight gap where open timers slip past detection. Known limitation, not addressed here.

---

## Design Decisions

| Question | Decision |
|---|---|
| Detection scope | Same task only. Group by `(project_did, user_email, site_name, site_id, task)`. |
| Overlap rule | Any temporal overlap: `a.start_time < b.end_time AND b.start_time < a.end_time`. |
| Transitive clustering | Yes. A↔B and B↔C means {A, B, C} forms one cluster even if A and C don't directly touch. |
| Auto-resolve preference | Keep today's rule — entry with the latest `end_time` wins after 7 days. |
| NULL end_time handling | Skip, same as today. |
| Badge in daily email | Existing DUPLICATE badge. No new visual treatment. |
| Same-start regression | Same-start groups still produce the same `group_id` as today, so existing pending/notified reviews migrate cleanly. |

---

## Architecture

One function changes meaningfully: `detect_and_track_duplicates` in `timer_correction_review.py` (~line 738). It still:

- Runs nightly inside `run_send`.
- Reads yesterday's entries via `get_previous_day_entries`.
- Writes to `stg_timer_duplicate_reviews`.
- Uses the existing JSONB `entries[]` array shape.

The grouping algorithm inside the function changes from "bucket by exact full duplicate key" to "bucket by `(project_did, user_email, site_name, site_id, task)`, then form overlap clusters within each bucket."

`_build_entries_html` (~line 544) reuses the same cluster logic to decide which rows get the DUPLICATE badge in the daily email — not a separate exact-match check.

One SQL migration (049) updates `rebuild_timer_clean()` and runs a backfill UPDATE to inject `start_time` into existing review rows' JSONB arrays.

No new tables. No new columns. No new emails. No new GHA workflows. No new form fields. No changes to the Apps Script trigger or the Google Form.

---

## Components

### Overlap predicate

```python
def _intervals_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Standard interval intersection. NULL end_time filtered out before call."""
    return a_start < b_end and b_start < a_end
```

### Clustering algorithm

Inside `detect_and_track_duplicates`:

1. Drop entries where `end_time is None`.
2. Bucket entries by `(project_did, user_email, site_name, site_id, task)`. Note: `start_time` removed from bucket key.
3. For each bucket of ≥2 entries, build connected components using Union-Find or O(n²) sweep. Two entries belong to the same component if they overlap directly, or transitively via a shared third entry.
4. For each component of ≥2 entries, create one cluster row in `stg_timer_duplicate_reviews`.

Components of 1 entry are not duplicates.

### Stable group_id

```python
earliest = min(c["start_time"] for c in cluster)
group_id = _make_group_id(project_did, user_email, earliest, site_name, site_id, task)
```

`_make_group_id` formula unchanged: `md5(project_did|user_email|start_time_iso|site_name|site_id|task)[:12]`.

Migration property: for today's same-start clusters, `earliest` equals the shared start time, so the resulting `group_id` is identical to today's. All existing `pending` / `notified` rows retain their IDs and their Google Form threads.

### JSONB entries[] payload

Today's shape (kept):
```json
[
  {"label": "A", "end_time": "2026-05-07T15:30:00+00:00", "duration_min": "120"},
  {"label": "B", "end_time": "2026-05-07T15:20:00+00:00", "duration_min": "80"}
]
```

New required field for overlap clusters: `start_time` per entry. Updated shape:
```json
[
  {"label": "A", "start_time": "2026-05-07T13:30:00+00:00", "end_time": "2026-05-07T15:30:00+00:00", "duration_min": "120"},
  {"label": "B", "start_time": "2026-05-07T14:00:00+00:00", "end_time": "2026-05-07T15:20:00+00:00", "duration_min": "80"}
]
```

The JSONB has no schema constraint, so this addition is non-breaking.

### `rebuild_timer_clean()` SQL update

Migration 049 changes the per-entry exclusion join to use the JSONB `start_time` field instead of the parent column.

```sql
-- before (line 96 of migration 048)
AND t.start_time = r.start_time

-- after
AND t.start_time = (rej->>'start_time')::timestamptz
```

Same change applies to the "unresolved duplicates, keep latest end_time" subquery (lines 104–121 of migration 048).

### One-time backfill

For every existing row in `stg_timer_duplicate_reviews`, inject `start_time` into each `entries[i]` element (and each `rejected_entries[i]` element where present), copying from the parent `start_time` column. This is valid because today every entry in a same-start cluster shares the parent start_time.

```sql
UPDATE data_staging.stg_timer_duplicate_reviews
SET entries = (
    SELECT jsonb_agg(
        jsonb_set(elem, '{start_time}', to_jsonb(start_time::text))
    )
    FROM jsonb_array_elements(entries) elem
)
WHERE entries IS NOT NULL
  AND NOT (entries->0 ? 'start_time');

UPDATE data_staging.stg_timer_duplicate_reviews
SET rejected_entries = (
    SELECT jsonb_agg(
        jsonb_set(elem, '{start_time}', to_jsonb(start_time::text))
    )
    FROM jsonb_array_elements(rejected_entries) elem
)
WHERE rejected_entries IS NOT NULL
  AND NOT (rejected_entries->0 ? 'start_time');
```

---

## Data Flow

**Nightly `run_send`** (12:01 AM ET):
1. Fetch yesterday's entries from `stg_timer_activities`.
2. Send the daily entries email (DUPLICATE badge now reflects overlap-based clustering).
3. `detect_and_track_duplicates` runs the new logic. Each cluster either matches an existing `group_id` or becomes a new review row.

**Form-submission `--apply`** (GHA `repository_dispatch`):
1. Read response from the form's response sheet.
2. Look up review by `group_id`.
3. Mark rejected entries in `stg_timer_duplicate_reviews.rejected_entries`.
4. Run `rebuild_timer_clean()` — exclusion now joins per-entry `start_time` from JSONB.
5. Send confirmation email.

**Auto-resolve** (≥7 days unresolved):
- Keep the entry with the latest `end_time`. All others marked rejected. Rebuild excludes them.

---

## Edge Cases and Limitations

- **NULL end_time:** Filtered out before clustering. Same gap as today — cross-midnight open timers escape detection. Out of scope for this design.
- **Single-entry cluster after filter:** Skipped.
- **Anchor shift on later runs:** If a same-day Swift re-extraction surfaces an entry with an even earlier `start_time` than the cluster's existing earliest, the computed `group_id` would shift. In practice Swift's daily extracts produce stable snapshots, so this is near-zero probability. Accepted limitation; mitigation (lookup-by-membership before group_id generation) deferred unless we observe it.
- **Correction or removal on a clustered entry mid-review:** Existing "correction supersedes duplicate review" logic applies unchanged.
- **3+ entry transitive cluster:** Single review row holds all members. Tech picks which one to keep; others are rejected.

---

## Testing

Required tests:

1. **`_intervals_overlap` unit tests** — boundary cases: touching but not overlapping (`a_end == b_start`), full containment, partial cross-over, identical intervals.
2. **Clustering unit tests** — same-start clusters produce the same `group_id` as the pre-change behavior (regression guard); contained clusters get one group; transitive 3-way clusters merge correctly; non-overlapping same-task entries stay separate.
3. **`detect_and_track_duplicates` integration test** — feed a fixture day's entries, assert the expected `stg_timer_duplicate_reviews` rows.
4. **`rebuild_timer_clean()` regression test** — insert a review with mixed-start_time JSONB entries, run the function, assert rejected entries are excluded from `stg_timer_activities_clean`.
5. **Backfill verification** — run the JSONB backfill UPDATE on a snapshot of production data, spot-check that `entries[i].start_time` matches the parent column on every existing row.

---

## Migration Order

1. Land code changes behind no flag (single PR).
2. Apply migration 049 (SQL function update + backfill UPDATEs) in the same window as the code deploy.
3. Verify next nightly run with a manual `--send --test --target-date 2026-05-XX` against a day known to have overlaps.
4. Watch the next 2–3 production nightly runs for unexpected DUPLICATE flags.

Rollback path: revert the code change and re-apply the previous version of `rebuild_timer_clean()`. The JSONB backfill is forward-compatible — old code ignores the added `start_time` field.

---

## Open Questions

None. All design decisions confirmed in the 2026-05-12 brainstorm session.
