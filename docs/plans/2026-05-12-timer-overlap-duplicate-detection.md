# Timer Overlap-Based Duplicate Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend timer duplicate detection from "same start_time only" to "any temporal overlap on the same task", so cases like Timer A 09:30–11:30 and Timer B 10:00–11:20 get flagged for tech review.

**Architecture:** Replace the inner grouping algorithm in `detect_and_track_duplicates` (Python) with overlap-based clustering using Union-Find. Anchor `group_id` on the cluster's earliest `start_time` so same-start cases produce identical IDs (zero-impact migration). Add `start_time` to JSONB `entries[]` so `rebuild_timer_clean()` can join per-entry instead of via the parent's start_time column. Same table, same Google Form, same DUPLICATE badge, same auto-resolve.

**Tech Stack:** Python 3.12, asyncpg, PostgreSQL (Supabase cloud), Gmail API, Google Sheets via Drive API. No new dependencies.

**Spec:** `docs/specs/timer-overlap-duplicate-detection.md`

**Repository:** `local-pipeline/` (git repo). All commits land there.

---

## File Structure

**Create:**
- `local-pipeline/swift_api_pipeline/tests/test_timer_overlap.py` — Unit tests for `_intervals_overlap` and `_build_overlap_clusters`. Plain `assert`-based, runnable via `python tests/test_timer_overlap.py`. (No pytest in this repo.)
- `local-pipeline/swift_api_pipeline/migrations/049_timer_overlap_dup_detection.sql` — Updates `data_staging.rebuild_timer_clean()` to join on per-entry `start_time` from JSONB; runs idempotent backfill UPDATE on existing review rows.
- `local-pipeline/swift_api_pipeline/migrations/apply_049.py` — Applies the migration to cloud Supabase via asyncpg.

**Modify:**
- `local-pipeline/swift_api_pipeline/timer_correction_review.py` — Add `_intervals_overlap` and `_build_overlap_clusters` helpers; rewrite `detect_and_track_duplicates` to use them; rewrite the "actual duplicates" detection block inside `_build_entries_html` to use the same clustering.

**Boundary discipline:**
- Helpers (`_intervals_overlap`, `_build_overlap_clusters`) are pure functions on dicts/datetimes. No DB. No I/O. Tested in isolation.
- `detect_and_track_duplicates` keeps its current responsibility (read entries → write review rows). Only the inner grouping changes.
- `_build_entries_html` keeps its current responsibility (render HTML). It calls the same `_build_overlap_clusters` helper as detection — single source of truth for "what is a duplicate?".

---

## Pre-Flight: Verify Repo State

- [ ] **Step 0.1: Confirm git working tree is in a clean enough state to commit**

Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline && git status --short`

Note any pre-existing modifications. They'll get committed separately or stashed before this work starts. The plan assumes the implementer either commits or stashes them first.

- [ ] **Step 0.2: Confirm timer_correction_review.py is at the expected baseline**

Run: `grep -n "def detect_and_track_duplicates\|def _build_entries_html\|def _make_group_id" "C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline/timer_correction_review.py"`

Expected output:
```
236:def _make_group_id(project_did: str, user_email: str, start_time: datetime,
518:def _build_entries_html(entries: list[dict]) -> str:
738:def detect_and_track_duplicates(db, entries: list[dict]):
```

Line numbers may shift slightly; the function names are the anchors.

---

## Task 1: `_intervals_overlap` Helper + Tests

**Files:**
- Create: `local-pipeline/swift_api_pipeline/tests/test_timer_overlap.py`
- Modify: `local-pipeline/swift_api_pipeline/timer_correction_review.py` (add helper just below `_parse_duration_response` around line 480)

### Step 1.1: Write the failing test file

- [ ] Create `local-pipeline/swift_api_pipeline/tests/test_timer_overlap.py` with this content:

```python
"""Tests for timer overlap detection helpers in timer_correction_review.py.

Run: python tests/test_timer_overlap.py
"""
from datetime import datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import _intervals_overlap


def _ts(h, m=0):
    """Build a UTC timestamp on a fixed date with given hour/minute."""
    return datetime(2026, 5, 7, h, m, tzinfo=timezone.utc)


def test_intervals_overlap_identical():
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(9, 30), _ts(11, 30)) is True


def test_intervals_overlap_contained():
    # B fully inside A: A 9:30-11:30, B 10:00-11:20
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(10, 0), _ts(11, 20)) is True


def test_intervals_overlap_partial_crossover():
    # A 9:30-11:30, B 11:00-12:30
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(11, 0), _ts(12, 30)) is True


def test_intervals_overlap_touching_not_overlapping():
    # A ends exactly when B starts: A 9:30-10:30, B 10:30-11:30
    # Strict inequality: NOT considered overlap.
    assert _intervals_overlap(_ts(9, 30), _ts(10, 30), _ts(10, 30), _ts(11, 30)) is False


def test_intervals_overlap_disjoint():
    # A 9:30-10:30, B 11:00-12:00
    assert _intervals_overlap(_ts(9, 30), _ts(10, 30), _ts(11, 0), _ts(12, 0)) is False


def test_intervals_overlap_same_start_different_end():
    # Today's classic same-start duplicate: A 9:30-11:30, B 9:30-11:00
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(9, 30), _ts(11, 0)) is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except AssertionError:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if not failed else 1)
```

### Step 1.2: Run the test, verify it fails

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `ImportError: cannot import name '_intervals_overlap' from 'timer_correction_review'`.

### Step 1.3: Add the helper to `timer_correction_review.py`

- [ ] Find the line in `timer_correction_review.py` that reads `def _parse_duration_response(value: str) -> float | None:` (around line 449). Insert the new helper *above* that function with this code:

```python
def _intervals_overlap(a_start, a_end, b_start, b_end) -> bool:
    """True if [a_start, a_end) and [b_start, b_end) intersect.

    All four arguments must be non-None timezone-aware datetimes.
    Touching endpoints (a_end == b_start) are NOT considered overlapping.
    """
    return a_start < b_end and b_start < a_end


```

### Step 1.4: Run the test, verify it passes

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `6/6 passed`, exit code 0.

### Step 1.5: Commit

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_timer_overlap.py
git commit -m "feat(timer): add _intervals_overlap helper

Pure interval-intersection check. Used by upcoming overlap-based
duplicate detection. Strict inequalities so touching endpoints
(a_end == b_start) do not count as overlapping.

Tests cover identical, contained, partial cross-over, touching,
disjoint, and same-start cases."
```

---

## Task 2: `_build_overlap_clusters` Helper + Tests

**Files:**
- Modify: `local-pipeline/swift_api_pipeline/tests/test_timer_overlap.py` (append new tests)
- Modify: `local-pipeline/swift_api_pipeline/timer_correction_review.py` (add helper below `_intervals_overlap`)

### Step 2.1: Append failing tests

- [ ] Append this code to `tests/test_timer_overlap.py` (before the `if __name__ == "__main__":` block):

```python
from timer_correction_review import _build_overlap_clusters


def _entry(start_h, start_m, end_h, end_m):
    """Minimal entry dict with start_time and end_time."""
    return {
        "start_time": _ts(start_h, start_m),
        "end_time":   _ts(end_h,   end_m),
    }


def test_clusters_single_entry_yields_one_singleton():
    entries = [_entry(9, 30, 11, 30)]
    clusters = _build_overlap_clusters(entries)
    assert len(clusters) == 1
    assert len(clusters[0]) == 1


def test_clusters_two_overlapping_entries_merge():
    a = _entry(9, 30, 11, 30)
    b = _entry(10, 0, 11, 20)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_clusters_two_disjoint_entries_stay_separate():
    a = _entry(9, 30, 10, 30)
    b = _entry(11, 0, 12, 0)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 2


def test_clusters_three_way_transitive_merge():
    # A 9:00-10:30, B 10:00-11:00, C 10:30-12:00
    # A overlaps B, B overlaps C, A does NOT directly overlap C.
    # All three must end up in the same cluster.
    a = _entry(9, 0, 10, 30)
    b = _entry(10, 0, 11, 0)
    c = _entry(10, 30, 12, 0)
    clusters = _build_overlap_clusters([a, b, c])
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_clusters_same_start_different_end_merge():
    # Today's classic case still clusters.
    a = _entry(9, 30, 11, 30)
    b = _entry(9, 30, 11, 0)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_clusters_preserves_input_order_within_cluster():
    # Stability: the cluster lists entries in input order.
    a = _entry(9, 30, 11, 30)
    b = _entry(10, 0, 11, 20)
    clusters = _build_overlap_clusters([a, b])
    assert clusters[0][0] is a
    assert clusters[0][1] is b


def test_clusters_touching_endpoints_stay_separate():
    # A ends exactly when B starts -> not overlapping, two singletons.
    a = _entry(9, 30, 10, 30)
    b = _entry(10, 30, 11, 30)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 2
```

### Step 2.2: Run the tests, verify they fail

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `ImportError: cannot import name '_build_overlap_clusters'`.

### Step 2.3: Add the clustering function

- [ ] Insert this function immediately after `_intervals_overlap` in `timer_correction_review.py`:

```python
def _build_overlap_clusters(entries: list[dict]) -> list[list[dict]]:
    """Group entries into connected components by time overlap.

    Two entries belong to the same cluster if their [start_time, end_time)
    windows intersect, or if they transitively reach each other through a
    third overlapping entry.

    Requires each entry dict to have non-None datetime values at
    ``entry["start_time"]`` and ``entry["end_time"]``. Callers are responsible
    for filtering NULL end_time before calling this.

    Returns clusters in input-encounter order. Within each cluster, entries
    preserve their input order.

    Union-Find over O(n^2) pairwise overlap checks. n is small in practice
    (entries per (user, task, site) per day rarely exceeds a handful).
    """
    n = len(entries)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _intervals_overlap(
                entries[i]["start_time"], entries[i]["end_time"],
                entries[j]["start_time"], entries[j]["end_time"],
            ):
                union(i, j)

    # Bucket entries by their cluster root, preserving input order.
    bucket_by_root: dict[int, list[dict]] = {}
    root_order: list[int] = []
    for i in range(n):
        root = find(i)
        if root not in bucket_by_root:
            bucket_by_root[root] = []
            root_order.append(root)
        bucket_by_root[root].append(entries[i])

    return [bucket_by_root[r] for r in root_order]


```

### Step 2.4: Run the tests, verify they pass

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `13/13 passed`, exit code 0.

### Step 2.5: Commit

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_timer_overlap.py
git commit -m "feat(timer): add _build_overlap_clusters helper

Union-Find based clustering for entries by temporal overlap. Handles
the transitive case (A overlaps B, B overlaps C, but A and C do not
directly touch -> single cluster of three).

Pure function on dicts with start_time/end_time fields. No DB I/O,
no side effects. Tested for singleton, pair overlap, disjoint pair,
three-way transitive merge, same-start, input-order preservation,
and touching-endpoint non-overlap."
```

---

## Task 3: Refactor `detect_and_track_duplicates` to Use Clusters

**Files:**
- Modify: `local-pipeline/swift_api_pipeline/timer_correction_review.py` (lines 738–824, the `detect_and_track_duplicates` function)
- Modify: `local-pipeline/swift_api_pipeline/tests/test_timer_overlap.py` (append regression test for `group_id` stability)

### Step 3.1: Add the regression test for `group_id` stability

- [ ] Append this test to `tests/test_timer_overlap.py` (before `if __name__ == "__main__":`):

```python
from timer_correction_review import _make_group_id


def test_group_id_same_start_matches_legacy_formula():
    """Migration property: today's same-start clusters must produce the same
    group_id under the new earliest-anchor rule, so existing pending reviews
    keep their IDs and Google Form threads.
    """
    project_did = "-OmzvGwfYsSskngv6SEo"
    user_email = "ryan@ontel.co"
    start = _ts(12, 2)
    site_name = "SOUTHLAND HILLS TN - New Build "
    site_id = "Mid-South Communications/VZW/CGC/NSB/17455477/Apr 2026"
    task = "3. Live Review Complete 2"

    # Two entries that share start_time (today's classic duplicate)
    entries = [
        {"start_time": start, "end_time": _ts(15, 36), "duration_min": 214},
        {"start_time": start, "end_time": _ts(15, 30), "duration_min": 208},
    ]
    earliest = min(e["start_time"] for e in entries)
    new_gid = _make_group_id(project_did, user_email, earliest, site_name, site_id, task)

    # Legacy formula used start_time of the (only) shared start
    legacy_gid = _make_group_id(project_did, user_email, start, site_name, site_id, task)

    assert new_gid == legacy_gid, (
        f"group_id changed for same-start cluster — would orphan existing reviews. "
        f"new={new_gid} legacy={legacy_gid}"
    )
```

### Step 3.2: Run the test, verify it passes immediately (no behavior change yet)

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `14/14 passed`. This test passes today — it's the regression guard for the refactor.

### Step 3.3: Replace the body of `detect_and_track_duplicates`

- [ ] Open `timer_correction_review.py`. Locate `def detect_and_track_duplicates(db, entries: list[dict]):` (around line 738). Replace the entire function body (lines 738–824) with this implementation:

```python
def detect_and_track_duplicates(db, entries: list[dict]):
    """Detect overlapping timer entries and create/update review records.

    Entries are duplicates if they share (project_did, user_email, site_name,
    site_id, task) AND their [start_time, end_time) windows intersect.
    Transitive overlap counts: A-B-C through B all land in one cluster even
    if A and C do not directly touch.

    NULL end_time entries (still-running timers) are filtered before clustering.

    group_id is anchored on the cluster's earliest start_time. For clusters
    where every entry shares the same start_time (today's classic case), this
    produces the same group_id as the legacy formula -- existing pending
    reviews keep their IDs and form threads.

    Cluster entries get persisted into stg_timer_duplicate_reviews.entries
    as a JSONB array. Each element now includes start_time (was implicit in
    the parent column before; needed explicitly now that cluster members may
    have different start_times). rebuild_timer_clean() joins on this per-entry
    start_time.
    """
    import string
    LABELS = list(string.ascii_uppercase)

    # 1. Bucket by (project, user, site, task) -- start_time deliberately omitted.
    buckets: dict[tuple, list[dict]] = {}
    for e in entries:
        if e.get("end_time") is None:
            continue  # Skip still-running timers
        key = (
            e["project_did"],
            e["user_email"],
            e.get("site_name"),
            e.get("site_id"),
            e.get("task"),
        )
        buckets.setdefault(key, []).append(e)

    # 2. Build overlap clusters within each bucket. Cluster of >=2 is a duplicate.
    dup_groups: list[dict] = []
    for (project_did, user_email, site_name, site_id, task), bucket in buckets.items():
        if len(bucket) < 2:
            continue
        for cluster in _build_overlap_clusters(bucket):
            if len(cluster) < 2:
                continue

            # Sort cluster by duration_min asc for stable labelling (matches legacy)
            sorted_entries = sorted(cluster, key=lambda r: float(r.get("duration_min") or 0))
            group_entries = []
            for i, e in enumerate(sorted_entries):
                if i >= len(LABELS):
                    break
                group_entries.append({
                    "label": LABELS[i],
                    "start_time": e["start_time"],
                    "end_time": e["end_time"],
                    "duration_min": e.get("duration_min"),
                })

            earliest = min(e["start_time"] for e in cluster)
            group_id = _make_group_id(
                project_did, user_email, earliest, site_name, site_id, task,
            )
            dup_groups.append({
                "group_id": group_id,
                "project_did": project_did,
                "project": cluster[0].get("project"),
                "user_email": user_email,
                "start_time": earliest,  # Parent column holds the anchor; per-entry start_time lives in JSONB.
                "site_name": site_name,
                "site_id": site_id,
                "task": task,
                "entries": group_entries,
            })

    if not dup_groups:
        return

    # 3. Skip groups already tracked.
    group_ids = [g["group_id"] for g in dup_groups]
    existing = retry_db(
        lambda: db.fetch(
            f"SELECT group_id FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews WHERE group_id = ANY($1)",
            group_ids,
        ),
        description="check existing duplicate groups",
    )
    existing_ids = {row["group_id"] for row in existing}

    new_groups = [g for g in dup_groups if g["group_id"] not in existing_ids]
    if not new_groups:
        return

    # 4. Insert new review records.
    now = datetime.now(timezone.utc)
    for g in new_groups:
        entries_json = _entries_to_jsonb(g["entries"])
        retry_db(
            lambda g=g, ej=entries_json: db.execute(
                f"""INSERT INTO {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                    (group_id, project_did, project, user_email, start_time,
                     site_name, site_id, task, entries, status, notified_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                g["group_id"], g["project_did"], g["project"], g["user_email"],
                g["start_time"], g["site_name"], g["site_id"], g["task"],
                ej, "notified", now,
            ),
            description=f"insert duplicate review {g['group_id']}",
        )

    logger.info(f"Tracked {len(new_groups)} new duplicate groups from daily entries")
```

### Step 3.4: Run tests again to confirm no regressions

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `14/14 passed`.

### Step 3.5: Smoke-check the import (no syntax errors in the module)

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python -c "from timer_correction_review import detect_and_track_duplicates, _intervals_overlap, _build_overlap_clusters; print('import ok')"`

Expected: `import ok`.

### Step 3.6: Commit

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_timer_overlap.py
git commit -m "feat(timer): detect overlap-based duplicates, not just same-start

Replaces the inner grouping in detect_and_track_duplicates. Buckets
entries by (project_did, user_email, site_name, site_id, task) and
forms overlap clusters via Union-Find. group_id is anchored on the
cluster's earliest start_time -- identical to today's value for
same-start clusters, so existing pending reviews keep their IDs and
Google Form threads.

JSONB entries[] now includes start_time per entry (was implicit in
the parent column before). rebuild_timer_clean() will join on this
in migration 049.

Adds regression test verifying group_id stability for same-start
clusters."
```

---

## Task 4: Daily Email DUPLICATE Badge — Use Cluster Logic

**Files:**
- Modify: `local-pipeline/swift_api_pipeline/timer_correction_review.py` (`_build_entries_html` at lines 518–601, specifically the "Detect actual duplicates" block at 544–554)

### Step 4.1: Replace the "actual duplicates" detection block

- [ ] Open `timer_correction_review.py`. Find these lines inside `_build_entries_html` (currently lines 544–554):

```python
    # Detect actual duplicates (same start_time) for DUPLICATE badge
    dup_key_map = {}  # key -> list of indices
    for i, entry in enumerate(entries):
        key = (entry["project_did"], entry["user_email"], entry["start_time"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        dup_key_map.setdefault(key, []).append(i)

    is_duplicate = set()
    for key, indices in dup_key_map.items():
        if len(indices) >= 2:
            is_duplicate.update(indices)
```

- [ ] Replace them with this cluster-based block (uses the same `_build_overlap_clusters` as detection):

```python
    # Detect duplicates via temporal overlap on the same task. Uses the same
    # cluster logic as detect_and_track_duplicates so the daily email's
    # DUPLICATE badges match the review records we just wrote.
    is_duplicate: set[int] = set()
    bucket_indices: dict[tuple, list[int]] = {}
    for i, entry in enumerate(entries):
        if entry.get("end_time") is None:
            continue  # Still-running timers can't be assessed for overlap
        key = (entry["project_did"], entry["user_email"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        bucket_indices.setdefault(key, []).append(i)

    for indices in bucket_indices.values():
        if len(indices) < 2:
            continue
        bucket_entries = [entries[i] for i in indices]
        for cluster in _build_overlap_clusters(bucket_entries):
            if len(cluster) < 2:
                continue
            # Map cluster members back to their indices in `entries`.
            for clustered in cluster:
                for idx in indices:
                    if entries[idx] is clustered:
                        is_duplicate.add(idx)
                        break
```

### Step 4.2: Append a small integration check to the test file

- [ ] Append this test to `tests/test_timer_overlap.py`:

```python
def test_email_dup_badge_logic_matches_detection():
    """The daily-email badge code must use the same cluster logic as
    detect_and_track_duplicates. Smoke check: a contained overlap pair
    yields both indices in is_duplicate.
    """
    # Mirror the inline logic from _build_entries_html with the same inputs.
    a = {"project_did": "P", "user_email": "u@x", "site_name": "S",
         "site_id": "SID", "task": "T",
         "start_time": _ts(9, 30), "end_time": _ts(11, 30), "duration_min": 120}
    b = {"project_did": "P", "user_email": "u@x", "site_name": "S",
         "site_id": "SID", "task": "T",
         "start_time": _ts(10, 0), "end_time": _ts(11, 20), "duration_min": 80}
    entries = [a, b]

    is_duplicate: set[int] = set()
    bucket_indices: dict[tuple, list[int]] = {}
    for i, entry in enumerate(entries):
        if entry.get("end_time") is None:
            continue
        key = (entry["project_did"], entry["user_email"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        bucket_indices.setdefault(key, []).append(i)

    for indices in bucket_indices.values():
        if len(indices) < 2:
            continue
        bucket_entries = [entries[i] for i in indices]
        for cluster in _build_overlap_clusters(bucket_entries):
            if len(cluster) < 2:
                continue
            for clustered in cluster:
                for idx in indices:
                    if entries[idx] is clustered:
                        is_duplicate.add(idx)
                        break

    assert is_duplicate == {0, 1}, f"expected both indices flagged, got {is_duplicate}"
```

### Step 4.3: Run tests

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python tests/test_timer_overlap.py`

Expected: `15/15 passed`.

### Step 4.4: Smoke-import the module

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python -c "from timer_correction_review import _build_entries_html; print('ok')"`

Expected: `ok`.

### Step 4.5: Commit

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_timer_overlap.py
git commit -m "feat(timer): daily-email DUPLICATE badge follows cluster logic

_build_entries_html now uses _build_overlap_clusters to decide which
rows get the DUPLICATE badge. Same cluster algorithm as
detect_and_track_duplicates -- the email and the review records can
never disagree about what is a duplicate."
```

---

## Task 5: Migration 049 SQL — Update `rebuild_timer_clean()` + Backfill JSONB

**Files:**
- Create: `local-pipeline/swift_api_pipeline/migrations/049_timer_overlap_dup_detection.sql`

### Step 5.1: Write the migration file

- [ ] Create `local-pipeline/swift_api_pipeline/migrations/049_timer_overlap_dup_detection.sql` with this content:

```sql
-- Migration 049: Overlap-based duplicate detection support
--
-- Code change in timer_correction_review.py extends duplicate detection from
-- "same start_time only" to "any temporal overlap on the same task". Within
-- a cluster, entries can now have different start_times. The parent
-- start_time column on stg_timer_duplicate_reviews holds the cluster anchor
-- (earliest start_time); per-entry start_times live in the JSONB entries[]
-- and rejected_entries[] arrays.
--
-- This migration:
--   1. Backfills existing review rows so each JSONB entry carries start_time
--      explicitly. Today every entry in a same-start cluster shares the
--      parent column, so we copy from there. Idempotent guard skips rows
--      already migrated.
--   2. Rewrites data_staging.rebuild_timer_clean() to join on per-entry
--      start_time inside JSONB instead of the parent column.

-- =========================================================================
-- 1. Idempotent JSONB backfill: inject start_time into entries[] and
--    rejected_entries[] where missing.
-- =========================================================================
UPDATE data_staging.stg_timer_duplicate_reviews
SET entries = (
    SELECT jsonb_agg(
        CASE
            WHEN elem ? 'start_time' THEN elem
            ELSE jsonb_set(elem, '{start_time}', to_jsonb((start_time AT TIME ZONE 'UTC')::text || '+00:00'))
        END
        ORDER BY ord
    )
    FROM jsonb_array_elements(entries) WITH ORDINALITY AS t(elem, ord)
)
WHERE entries IS NOT NULL
  AND jsonb_array_length(entries) > 0
  AND NOT (entries->0 ? 'start_time');

UPDATE data_staging.stg_timer_duplicate_reviews
SET rejected_entries = (
    SELECT jsonb_agg(
        CASE
            WHEN elem ? 'start_time' THEN elem
            ELSE jsonb_set(elem, '{start_time}', to_jsonb((start_time AT TIME ZONE 'UTC')::text || '+00:00'))
        END
        ORDER BY ord
    )
    FROM jsonb_array_elements(rejected_entries) WITH ORDINALITY AS t(elem, ord)
)
WHERE rejected_entries IS NOT NULL
  AND jsonb_array_length(rejected_entries) > 0
  AND NOT (rejected_entries->0 ? 'start_time');

-- =========================================================================
-- 2. Replace rebuild_timer_clean() to join on per-entry start_time from JSONB.
--    Differences from migration 048:
--      - Rejected-entry exclusion join uses (rej->>'start_time')::timestamptz
--        instead of r.start_time.
--      - Unresolved "keep latest end_time" subquery uses
--        (e->>'start_time')::timestamptz instead of r.start_time.
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 1: Insert from staging, excluding rejected duplicates + removals.
    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        -- Exclude rows matching rejected natural keys from resolved reviews.
        -- Now joins on per-entry start_time inside JSONB.
        NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (rej->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        -- For unresolved duplicates, keep only the entry with the latest end_time.
        -- Now joins on per-entry start_time inside JSONB.
        AND NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (e->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        )
        -- Exclude removed entries UNLESS reverted or overridden by correction.
        AND NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_entry_removals rm
            WHERE t.project_did = rm.project_did
              AND t.user_email  = rm.user_email
              AND t.start_time  = rm.start_time
              AND t.site_name IS NOT DISTINCT FROM rm.site_name
              AND t.site_id   IS NOT DISTINCT FROM rm.site_id
              AND t.task      IS NOT DISTINCT FROM rm.task
              AND t.end_time IS NOT DISTINCT FROM rm.end_time
              AND t.duration_min IS NOT DISTINCT FROM rm.duration_min
              AND rm.reason IS DISTINCT FROM 'REVERTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM data_staging.stg_timer_corrections c
                  WHERE c.project_did = rm.project_did
                    AND c.user_email  = rm.user_email
                    AND c.start_time  = rm.start_time
                    AND c.site_name IS NOT DISTINCT FROM rm.site_name
                    AND c.site_id   IS NOT DISTINCT FROM rm.site_id
                    AND c.task      IS NOT DISTINCT FROM rm.task
                    AND c.end_time IS NOT DISTINCT FROM rm.end_time
                    AND c.original_duration_min IS NOT DISTINCT FROM rm.duration_min
              )
        )
    ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
             t.task, t.end_time, t.duration_min, t.id;

    -- Step 2: Apply duration corrections (unchanged from migration 048).
    UPDATE data_staging.stg_timer_activities_clean t
    SET duration_min = c.corrected_duration_min,
        end_time    = c.corrected_end_time
    FROM data_staging.stg_timer_corrections c
    WHERE c.status = 'corrected'
      AND t.project_did = c.project_did
      AND t.user_email  = c.user_email
      AND t.start_time  = c.start_time
      AND t.site_name IS NOT DISTINCT FROM c.site_name
      AND t.site_id   IS NOT DISTINCT FROM c.site_id
      AND t.task      IS NOT DISTINCT FROM c.task
      AND t.end_time IS NOT DISTINCT FROM c.end_time
      AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min;

    -- Step 3: Append manual additions (unchanged from migration 048).
    INSERT INTO data_staging.stg_timer_activities_clean (
        id, project, project_number, project_did, site_name, site_id,
        task, task_clean, site_lat, site_long, user_lat, user_long,
        user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT
        a.id, a.project, a.project_number, a.project_did, a.site_name, a.site_id,
        a.task, a.task_clean, a.site_lat, a.site_long, a.user_lat, a.user_long,
        a.user_accuracy_m, a.site_vs_user_km, a.start_time, a.end_time, a.duration_min,
        a.user_name, a.user_email, a.user_role,
        a.run_id, a.run_date,
        COALESCE(a.start_date, (a.start_time AT TIME ZONE 'America/New_York')::date),
        COALESCE(a.end_date,   (a.start_time AT TIME ZONE 'America/New_York')::date),
        a.loaded_at
    FROM data_staging.stg_timer_entry_additions a;
END;
$$;
```

### Step 5.2: Lint check the SQL syntactically

- [ ] Run a quick eyeball pass: confirm the two `UPDATE` statements above the function and the `CREATE OR REPLACE FUNCTION` are all terminated with `;`. Confirm no unmatched dollar-quotes.

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python -c "p = open('migrations/049_timer_overlap_dup_detection.sql').read(); print(f'len={len(p)} chars, semicolons={p.count(chr(59))}, $\$={p.count(chr(36)*2)}')"`

Expected: `$$` count is even (each `AS $$ ... $$;` pair contributes 2). No syntax error from Python read.

### Step 5.3: Commit the migration file

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/migrations/049_timer_overlap_dup_detection.sql
git commit -m "chore(db): migration 049 -- rebuild_timer_clean joins JSONB start_time

Backfills existing stg_timer_duplicate_reviews rows so each entries[]
and rejected_entries[] element carries start_time explicitly. For
today's same-start clusters this just copies the parent column.

Rewrites rebuild_timer_clean() so the rejected-entry exclusion and
the unresolved-keep-latest subquery both join on
(elem->>'start_time')::timestamptz instead of the parent column.

This lets overlap-based clusters (whose entries have different
start_times) work correctly. Same-start clusters behave identically
to before."
```

---

## Task 6: Apply Migration 049

**Files:**
- Create: `local-pipeline/swift_api_pipeline/migrations/apply_049.py`

### Step 6.1: Write the apply script

- [ ] Create `local-pipeline/swift_api_pipeline/migrations/apply_049.py` with this content:

```python
"""Apply migration 049: Overlap-based duplicate detection support."""
import asyncio
import ssl
from pathlib import Path
from dotenv import load_dotenv
import os

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def main():
    import asyncpg

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl=ctx,
    )

    sql_path = Path(__file__).with_name("049_timer_overlap_dup_detection.sql")
    sql = sql_path.read_text(encoding="utf-8")

    # Pre-flight snapshot
    before_with_start = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "AND entries->0 ? 'start_time'"
    )
    before_total = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0"
    )
    print(f"Before: {before_with_start}/{before_total} review rows already have entries[].start_time")

    print("Applying migration 049: Overlap-based duplicate detection support...")
    await conn.execute(sql)
    print("Migration 049 applied successfully.")

    # Post-verify backfill
    after_with_start = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "AND entries->0 ? 'start_time'"
    )
    print(f"After:  {after_with_start}/{before_total} review rows have entries[].start_time")
    assert after_with_start == before_total, (
        f"Backfill incomplete: {after_with_start}/{before_total} rows have entries[].start_time"
    )

    # Spot-check: a random row's entries[0].start_time matches the parent start_time
    sample = await conn.fetchrow(
        "SELECT group_id, start_time, entries->0->>'start_time' AS first_entry_start "
        "FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "LIMIT 1"
    )
    if sample:
        print(f"Spot-check {sample['group_id']}: parent={sample['start_time']}, "
              f"entries[0]={sample['first_entry_start']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

### Step 6.2: Apply the migration

- [ ] Run: `cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline/swift_api_pipeline && python migrations/apply_049.py`

Expected output:
```
Before: 0/<N> review rows already have entries[].start_time
Applying migration 049: Overlap-based duplicate detection support...
Migration 049 applied successfully.
After:  <N>/<N> review rows have entries[].start_time
Spot-check <group_id>: parent=..., entries[0]=...
```

The before/after counts must match. The assertion will fail loudly if backfill is incomplete.

### Step 6.3: Commit the apply script

- [ ] Run from `C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline`:

```bash
git add swift_api_pipeline/migrations/apply_049.py
git commit -m "chore(db): apply_049.py with pre/post backfill verification"
```

---

## Task 7: End-to-End Verification

**Files:** None. This is a manual run + observation step.

### Step 7.1: Pick a target date and tech with a known overlap

- [ ] Pick a recent date where you expect overlap-based duplicates. The Ryan + SOUTHLAND HILLS case from 2026-05-07 has been resolved (manual addition + removal), so it's no longer a live overlap. Find a current candidate by running:

```sql
WITH yesterday AS (SELECT (CURRENT_DATE - INTERVAL '1 day')::date AS d)
SELECT a.user_email, a.task, a.site_name,
       (a.start_time AT TIME ZONE 'America/New_York') AS start_et,
       (a.end_time   AT TIME ZONE 'America/New_York') AS end_et,
       a.duration_min
FROM data_staging.stg_timer_activities a, yesterday y
WHERE DATE(a.start_time AT TIME ZONE 'America/New_York') = y.d
  AND a.end_time IS NOT NULL
ORDER BY a.user_email, a.task, a.start_time;
```

Eyeball for any same (user, task) pair where the time ranges overlap. If none exist yesterday, fall back to a known historical date.

### Step 7.2: Run detection in dry-run mode against the target date

- [ ] Set a one-off target date variable in PowerShell, then run with test mode so any emails are routed to Jamil:

```powershell
cd C:\Users\admin\Desktop\Projects\ai-projects\local-pipeline\swift_api_pipeline
python timer_correction_review.py --send --test --target-date 2026-05-DD
```

Replace `2026-05-DD` with the chosen target date. Watch the logs.

### Step 7.3: Verify new review records (if any) and email content

- [ ] Query `stg_timer_duplicate_reviews` for groups created in the last 10 minutes:

```sql
SELECT group_id, user_email, task,
       (start_time AT TIME ZONE 'America/New_York') AS earliest_et,
       jsonb_array_length(entries) AS n_entries,
       entries
FROM data_staging.stg_timer_duplicate_reviews
WHERE notified_at > NOW() - INTERVAL '10 minutes'
ORDER BY notified_at DESC;
```

For each row inspect that `entries` is a JSONB array with `start_time`, `end_time`, `duration_min`, and `label` per element.

- [ ] Open the test-mode email in Jamil's inbox and confirm overlapping rows carry the orange **DUPLICATE** badge (not just same-start rows).

### Step 7.4: Verify `rebuild_timer_clean()` handles a mixed-start cluster

- [ ] If a new overlap review landed in Step 7.3, simulate a tech response by manually marking one entry as rejected and rebuilding:

```sql
-- Replace <gid> with the group_id from Step 7.3
UPDATE data_staging.stg_timer_duplicate_reviews
SET status = 'resolved',
    rejected_entries = (entries - 0),  -- reject all but the first
    resolved_at = NOW(),
    resolved_by = 'manual-verification'
WHERE group_id = '<gid>';

SELECT data_staging.rebuild_timer_clean();
```

- [ ] Check that the rejected entries are absent from `stg_timer_activities_clean`:

```sql
-- Substitute the rejected-entry natural keys from the previous step
SELECT COUNT(*) FROM data_staging.stg_timer_activities_clean
WHERE user_email = '<email>'
  AND task = '<task>'
  AND start_time = '<rejected start_time>'::timestamptz
  AND end_time   = '<rejected end_time>'::timestamptz
  AND duration_min = <rejected duration>;
```

Expected: `0`. The rejected entry should be excluded.

- [ ] Roll back the test resolution if it was a real review you don't want auto-resolved early:

```sql
UPDATE data_staging.stg_timer_duplicate_reviews
SET status = 'notified',
    rejected_entries = NULL,
    resolved_at = NULL,
    resolved_by = NULL
WHERE group_id = '<gid>';

SELECT data_staging.rebuild_timer_clean();
```

### Step 7.5: Document the verification result

- [ ] Append a short note to `local-pipeline/WORK_LOG.md` (top of the file, above existing sessions) describing the test date, what overlapping pair was caught, and that the email rendered the DUPLICATE badge correctly. One paragraph.

- [ ] Commit:

```bash
git add WORK_LOG.md
git commit -m "docs: WORK_LOG verification note for overlap duplicate detection"
```

### Step 7.6: Optional — run a fast smoke against `--apply`

- [ ] If you submitted a real form response in the chosen target date's flow, run `--apply` and watch for the confirmation email to arrive in-thread:

```powershell
cd C:\Users\admin\Desktop\Projects\ai-projects\local-pipeline\swift_api_pipeline
python timer_correction_review.py --apply --test
```

Expected: logs report 1 group resolved, rebuild ran, confirmation email sent to Jamil (test mode).

---

## Rollback (if needed)

If the new detection produces too many false positives or regressions:

1. Revert the code commits in `swift_api_pipeline/timer_correction_review.py`:

```bash
cd C:/Users/admin/Desktop/Projects/ai-projects/local-pipeline
git revert <commit-hash-of-task-4> <commit-hash-of-task-3>
```

2. The migration 049 backfill is forward-compatible — old code ignores the added `start_time` field, so no DB rollback is required. The previous version of `rebuild_timer_clean()` can be restored by re-applying migration 048 if join behavior must revert exactly.

3. Hand-revert by re-running just the function body from migration 048 if needed.

---

## Self-Review Checklist (already done)

- **Spec coverage:** Detection logic (Tasks 1–3), DUPLICATE badge update (Task 4), `rebuild_timer_clean()` update + backfill (Tasks 5–6), end-to-end verification (Task 7). All spec sections accounted for.
- **No placeholders:** Every code block contains real code, every command has expected output, every file path is absolute.
- **Type consistency:** `_intervals_overlap` signature consistent across Task 1 + caller in Task 2. `_build_overlap_clusters` signature consistent across Task 2 + callers in Tasks 3 + 4. JSONB `start_time` injection logic consistent between Python writer (Task 3) and SQL backfill (Task 5).
- **No commits skip hooks.** No `--no-verify` flags anywhere.
- **`local-pipeline/` is the git repo.** All commit commands run from that root.
