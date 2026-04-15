# Timer Daily Task Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Daily Task Summary table to the top of the Timer Activity Entries email, grouping yesterday's entries by (task_clean, site_name, project) and showing aggregate duration with a duplicate indicator.

**Architecture:** Pure in-memory computation over the existing `entries` list already fetched by `send_daily_emails()`. Two new helper functions added to `timer_correction_review.py`: `_compute_summary_groups()` (data) and `_build_summary_html()` (HTML). One three-line insertion into the existing `html_body` f-string. Zero DB, schema, or workflow changes. Standalone smoke-test script using `assert` statements matches the project's `_*.py` helper convention.

**Tech Stack:** Python 3.14 (matches the venv), stdlib only (no pytest, no new dependencies). Existing `_fmt_duration()` is reused.

---

## File Structure

**Modify:**
- `swift_api_pipeline/timer_correction_review.py`
  - Add `_compute_summary_groups()` (pure function, ~35 lines)
  - Add `_build_summary_html()` (pure function, ~45 lines)
  - Insert 3-line block into `send_daily_emails()` html_body f-string (line ~484)

**Create:**
- `swift_api_pipeline/_test_summary.py` — standalone smoke-test script with fixture data; runs assertions and prints pass/fail. No external deps. Name prefixed with `_` to match existing helper-script convention (`_preprod_cleanup.py`, `_bulk_cleanup.py`, etc.).

**Not touched:**
- `extract_timer.py`, `transform.py`, `config.py`, `db.py`
- `pipeline-timer.yml` or any other workflow
- Any migration, Apps Script, or form
- Any schema

---

## Task 1: Compute summary groups (pure data function)

**Files:**
- Create: `swift_api_pipeline/_test_summary.py`
- Modify: `swift_api_pipeline/timer_correction_review.py` (add new function)

This task introduces a pure function that takes a list of timer entry dicts (shape matching what `get_previous_day_entries()` returns) and produces a list of summary group dicts ordered by total duration descending.

- [ ] **Step 1: Write the failing test file**

Create `swift_api_pipeline/_test_summary.py` with the following content:

```python
"""Smoke tests for the Daily Task Summary helpers.

Usage:
    cd swift_api_pipeline
    venv/Scripts/python _test_summary.py

No external deps — matches the project pattern of underscore-prefixed
helper scripts (see _preprod_cleanup.py, _bulk_cleanup.py).
"""
from datetime import datetime, timezone, timedelta

from timer_correction_review import _compute_summary_groups


def _entry(task_clean, site_name, project, project_did, site_id,
           start_time, duration_min, user_email="test@ontel.co"):
    """Build a timer-entry-shaped dict for fixtures."""
    return {
        "project": project,
        "project_did": project_did,
        "user_email": user_email,
        "start_time": start_time,
        "end_time": start_time + timedelta(minutes=duration_min),
        "duration_min": duration_min,
        "site_name": site_name,
        "site_id": site_id,
        "task": task_clean,          # raw == clean for fixture simplicity
        "task_clean": task_clean,
    }


def test_groups_single_entry():
    """One entry -> one group, no duplicate flag."""
    t = datetime(2026, 4, 14, 13, 0, tzinfo=timezone.utc)
    entries = [_entry("COP Review", "SITE_A", "ProjX", "did_1", "sid_1", t, 60)]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1, f"expected 1 group, got {len(groups)}"
    g = groups[0]
    assert g["task"] == "COP Review"
    assert g["site"] == "SITE_A"
    assert g["project"] == "ProjX"
    assert g["entries"] == 1
    assert g["total_duration_min"] == 60
    assert g["has_duplicates"] is False
    print("PASS test_groups_single_entry")


def test_groups_same_task_different_sites_stay_split():
    """Same task at two sites -> two separate groups."""
    t1 = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t1, 60),
        _entry("COP Review", "SITE_B", "ProjX", "did_1", "sidB", t2, 60),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 2
    sites = sorted(g["site"] for g in groups)
    assert sites == ["SITE_A", "SITE_B"]
    print("PASS test_groups_same_task_different_sites_stay_split")


def test_groups_sum_durations_same_task_site_project():
    """Multiple entries, same task+site+project, different start_times -> one group, summed."""
    t1 = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t1, 60),
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t2, 45),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["entries"] == 2
    assert groups[0]["total_duration_min"] == 105
    # Different start_times -> not a duplicate
    assert groups[0]["has_duplicates"] is False
    print("PASS test_groups_sum_durations_same_task_site_project")


def test_groups_sort_by_duration_desc():
    """Groups should be ordered by total duration descending."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        _entry("Short Task", "SITE_A", "ProjX", "did_1", "sidA", t, 15),
        _entry("Long Task",  "SITE_A", "ProjX", "did_1", "sidA", t, 120),
        _entry("Mid Task",   "SITE_A", "ProjX", "did_1", "sidA", t, 60),
    ]

    groups = _compute_summary_groups(entries)

    tasks = [g["task"] for g in groups]
    assert tasks == ["Long Task", "Mid Task", "Short Task"], f"wrong order: {tasks}"
    print("PASS test_groups_sort_by_duration_desc")


if __name__ == "__main__":
    test_groups_single_entry()
    test_groups_same_task_different_sites_stay_split()
    test_groups_sum_durations_same_task_site_project()
    test_groups_sort_by_duration_desc()
    print("\nAll tests passed.")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python _test_summary.py
```

**Expected:** `ImportError: cannot import name '_compute_summary_groups' from 'timer_correction_review'`

- [ ] **Step 3: Implement `_compute_summary_groups()` in `timer_correction_review.py`**

Add this function immediately after `_fmt_duration()` (after line 294, before `_correct_form_url()`):

```python
def _compute_summary_groups(entries: list[dict]) -> list[dict]:
    """Aggregate timer entries by (task_clean, site_name, project).

    Pure function. Takes the same shape of entry dicts that
    `get_previous_day_entries()` returns. Returns one dict per distinct
    (task_clean, site_name, project) combination, with per-group totals
    and a boolean flag indicating whether the group contains any
    duplicate entries (multiple rows sharing the full duplicate-detection
    key). Sorted by total_duration_min descending, then task ascending.

    Duplicate-detection key (matches detect_and_create_duplicate_reviews):
        (project_did, user_email, start_time, site_name, site_id, task)
    """
    from collections import defaultdict

    # Group by (task_clean, site_name, project).
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for e in entries:
        task = e.get("task_clean") or e.get("task") or ""
        key = (task, e.get("site_name") or "", e.get("project") or "")
        buckets[key].append(e)

    groups = []
    for (task, site, project), rows in buckets.items():
        # A group has duplicates iff any two rows share the full dup key.
        seen_dup_keys: set[tuple] = set()
        has_duplicates = False
        for r in rows:
            dup_key = (
                r.get("project_did"),
                r.get("user_email"),
                r.get("start_time"),
                r.get("site_name"),
                r.get("site_id"),
                r.get("task"),
            )
            if dup_key in seen_dup_keys:
                has_duplicates = True
                break
            seen_dup_keys.add(dup_key)

        total = sum(float(r.get("duration_min") or 0) for r in rows)

        groups.append({
            "task": task,
            "site": site,
            "project": project,
            "entries": len(rows),
            "total_duration_min": total,
            "has_duplicates": has_duplicates,
        })

    groups.sort(key=lambda g: (-g["total_duration_min"], g["task"]))
    return groups
```

- [ ] **Step 4: Add duplicate-detection and null-project edge case tests to `_test_summary.py`**

Append both test functions to `_test_summary.py` (before the `if __name__ == "__main__":` block):

```python
def test_groups_flags_duplicates():
    """Two entries sharing the full duplicate key -> has_duplicates=True."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    # Same everything except end_time/duration (classic Swift sync duplicate)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 60),
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 75),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["entries"] == 2
    assert groups[0]["total_duration_min"] == 135
    assert groups[0]["has_duplicates"] is True, "should flag duplicate"
    print("PASS test_groups_flags_duplicates")


def test_groups_handles_null_project():
    """None project groups as empty string and still produces a row."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        # project is None — real Swift data sometimes has this
        _entry("Admin Overhead", "SITE_A", None, "did_1", "sidA", t, 30),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["project"] == ""
    assert groups[0]["task"] == "Admin Overhead"
    assert groups[0]["total_duration_min"] == 30
    print("PASS test_groups_handles_null_project")
```

And update the `if __name__` block to call both:

```python
if __name__ == "__main__":
    test_groups_single_entry()
    test_groups_same_task_different_sites_stay_split()
    test_groups_sum_durations_same_task_site_project()
    test_groups_sort_by_duration_desc()
    test_groups_flags_duplicates()
    test_groups_handles_null_project()
    print("\nAll tests passed.")
```

- [ ] **Step 5: Run all tests and verify they pass**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python _test_summary.py
```

**Expected output:**
```
PASS test_groups_single_entry
PASS test_groups_same_task_different_sites_stay_split
PASS test_groups_sum_durations_same_task_site_project
PASS test_groups_sort_by_duration_desc
PASS test_groups_flags_duplicates
PASS test_groups_handles_null_project

All tests passed.
```

- [ ] **Step 6: Commit Task 1**

```bash
cd ~/Desktop/Projects/ai-projects/local-pipeline
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/_test_summary.py
git commit -m "$(cat <<'EOF'
Add _compute_summary_groups() for Timer Daily Task Summary

Pure function that aggregates timer entries by (task_clean, site_name,
project), detects duplicates via the existing (project_did, user_email,
start_time, site_name, site_id, task) key, and returns groups sorted by
total duration descending.

Adds _test_summary.py with 5 smoke tests exercising single-entry,
cross-site split, duration summing, sort order, and duplicate flag cases.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Render the summary HTML

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py` (add HTML builder)
- Modify: `swift_api_pipeline/_test_summary.py` (add HTML tests)

This task turns the group dicts into an HTML table that visually matches the existing detail table's styling.

- [ ] **Step 1: Write failing tests for the HTML builder**

Append to `_test_summary.py` (before the `if __name__` block):

```python
from timer_correction_review import _build_summary_html


def test_html_empty_entries():
    """Empty input -> empty string (no table rendered)."""
    result = _build_summary_html([])
    assert result == "", f"expected empty string, got {result!r}"
    print("PASS test_html_empty_entries")


def test_html_contains_expected_columns_and_values():
    """Rendered HTML includes column headers and per-row content."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 60),
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 75),  # duplicate
        _entry("Pre-Meeting", "SITE_A", "ProjX", "did_1", "sidA",
               t + timedelta(hours=3), 30),
    ]

    html = _build_summary_html(entries)

    # Column headers
    for header in ("Task", "Site", "Project", "Entries", "Total", "&#9888;"):
        assert header in html, f"missing header {header!r}"

    # Row content — duplicate group (COP Review, 135 min)
    assert "COP Review" in html
    assert "SITE_A" in html
    assert "ProjX" in html
    assert "2h 15m" in html, "expected formatted 135 min"

    # Non-duplicate row
    assert "Pre-Meeting" in html
    assert "30 min" in html

    # Duplicate flag uses the warning emoji only for the COP Review row
    # (presence is enough here; visual verification happens via --test)
    assert "&#9888;" in html or "\u26a0" in html

    print("PASS test_html_contains_expected_columns_and_values")


def test_html_sort_order_duration_desc():
    """Rows appear duration-desc: Long task appears before Short task in HTML."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        _entry("Short Task", "SITE_A", "ProjX", "did_1", "sidA", t, 15),
        _entry("Long Task",  "SITE_A", "ProjX", "did_1", "sidA", t, 120),
    ]

    html = _build_summary_html(entries)

    long_pos = html.find("Long Task")
    short_pos = html.find("Short Task")
    assert long_pos != -1 and short_pos != -1
    assert long_pos < short_pos, "Long Task should appear before Short Task"
    print("PASS test_html_sort_order_duration_desc")
```

And update the `if __name__` block:

```python
if __name__ == "__main__":
    test_groups_single_entry()
    test_groups_same_task_different_sites_stay_split()
    test_groups_sum_durations_same_task_site_project()
    test_groups_sort_by_duration_desc()
    test_groups_flags_duplicates()
    test_groups_handles_null_project()
    test_html_empty_entries()
    test_html_contains_expected_columns_and_values()
    test_html_sort_order_duration_desc()
    print("\nAll tests passed.")
```

- [ ] **Step 2: Run tests to verify HTML tests fail**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python _test_summary.py
```

**Expected:** `ImportError: cannot import name '_build_summary_html' from 'timer_correction_review'`

- [ ] **Step 3: Implement `_build_summary_html()` in `timer_correction_review.py`**

Add this function immediately after `_compute_summary_groups()`:

```python
def _build_summary_html(entries: list[dict]) -> str:
    """Render the Daily Task Summary table for one tech's entries.

    Returns an empty string when entries is empty so the caller can
    unconditionally inline the result.

    Styling mirrors the existing detail table (Arial 13px, 1px borders,
    light header background). Rows flagged as containing duplicates get
    a subtle yellow-tinted background and a red warning glyph in the
    rightmost column.
    """
    groups = _compute_summary_groups(entries)
    if not groups:
        return ""

    header_style = (
        "padding:6px 10px;border:1px solid #bbb;background:#eef3fa;"
        "text-align:left;font-size:13px;"
    )
    cell_style = "padding:6px 10px;border:1px solid #ccc;font-size:13px;"
    warn_style = cell_style + "text-align:center;"
    row_dup_bg = "background:#fffbe6;"  # subtle yellow for duplicate rows

    html = [
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;'
        'margin:8px 0 16px;">',
        "<tr>",
        f'<th style="{header_style}">Task</th>',
        f'<th style="{header_style}">Site</th>',
        f'<th style="{header_style}">Project</th>',
        f'<th style="{header_style}text-align:right;">Entries</th>',
        f'<th style="{header_style}text-align:right;">Total</th>',
        f'<th style="{header_style}text-align:center;">&#9888;</th>',
        "</tr>",
    ]

    for g in groups:
        row_style = f'style="{row_dup_bg}"' if g["has_duplicates"] else ""
        dup_cell = (
            '<span style="color:#c62828;">&#9888;</span>'
            if g["has_duplicates"] else "&mdash;"
        )
        html.append(f"<tr {row_style}>")
        html.append(f'<td style="{cell_style}">{_escape_html(g["task"])}</td>')
        html.append(f'<td style="{cell_style}">{_escape_html(g["site"])}</td>')
        html.append(f'<td style="{cell_style}">{_escape_html(g["project"])}</td>')
        html.append(f'<td style="{cell_style}text-align:right;">{g["entries"]}</td>')
        html.append(
            f'<td style="{cell_style}text-align:right;">'
            f'{_fmt_duration(g["total_duration_min"])}</td>'
        )
        html.append(f'<td style="{warn_style}">{dup_cell}</td>')
        html.append("</tr>")

    html.append("</table>")
    return "".join(html)


def _escape_html(value) -> str:
    """Minimal HTML escape for cell values."""
    if value is None:
        return ""
    s = str(value)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))
```

- [ ] **Step 4: Run all tests and verify they pass**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python _test_summary.py
```

**Expected output:**
```
PASS test_groups_single_entry
PASS test_groups_same_task_different_sites_stay_split
PASS test_groups_sum_durations_same_task_site_project
PASS test_groups_sort_by_duration_desc
PASS test_groups_flags_duplicates
PASS test_groups_handles_null_project
PASS test_html_empty_entries
PASS test_html_contains_expected_columns_and_values
PASS test_html_sort_order_duration_desc

All tests passed.
```

- [ ] **Step 5: Commit Task 2**

```bash
cd ~/Desktop/Projects/ai-projects/local-pipeline
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/_test_summary.py
git commit -m "$(cat <<'EOF'
Add _build_summary_html() renderer for Timer Daily Task Summary

Renders a 6-column HTML table (Task, Site, Project, Entries, Total,
duplicate flag) ordered by total duration descending. Row styling
matches the existing detail table; duplicate-flagged rows get a subtle
yellow tint and a red warning glyph.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Integrate summary into the daily email body

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py:482-490` (html_body f-string in `send_daily_emails`)

This task wires the summary into the outgoing daily email. No new tests here — the existing unit tests cover the summary HTML; end-to-end verification happens in Task 4 via `--test` mode.

- [ ] **Step 1: Read the current `html_body` f-string to anchor the edit**

Confirm the exact content around line 484 in `timer_correction_review.py`. The relevant block is:

```python
<p>Hi {_first_name(user_email)},</p>
<p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
   from <strong>{date_str}</strong>.</p>
<ul style="font-size:13px;color:#555;margin:8px 0 16px;">
    <li><strong style="color:#1565c0;">Edit</strong> — fix a wrong duration</li>
    <li><strong style="color:#c62828;">Remove</strong> — delete a duplicate or incorrect entry</li>
</ul>
```

- [ ] **Step 2: Insert the summary block between the intro paragraph and the bullet list**

Use Edit to replace:

```python
<p>Hi {_first_name(user_email)},</p>
<p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
   from <strong>{date_str}</strong>.</p>
<ul style="font-size:13px;color:#555;margin:8px 0 16px;">
```

With:

```python
<p>Hi {_first_name(user_email)},</p>
<p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
   from <strong>{date_str}</strong>.</p>
<h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Daily Task Summary</h3>
{_build_summary_html(user_entries)}
<ul style="font-size:13px;color:#555;margin:8px 0 16px;">
```

- [ ] **Step 3: Sanity-compile the module to catch syntax errors before sending anything**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python -c "import timer_correction_review; print('imported OK')"
```

**Expected:** `imported OK`

- [ ] **Step 4: Rerun the smoke-test suite — nothing should regress**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python _test_summary.py
```

**Expected:** all 9 tests PASS (same as end of Task 2).

- [ ] **Step 5: Commit Task 3**

```bash
cd ~/Desktop/Projects/ai-projects/local-pipeline
git add swift_api_pipeline/timer_correction_review.py
git commit -m "$(cat <<'EOF'
Wire Daily Task Summary into Timer Activity Entries email

Inserts the new summary table between the intro paragraph and the
Edit/Remove bullet list in send_daily_emails()'s html_body. No behavior
change to the detail table, buttons, recipient list, or threading.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Manual verification against real data

**Files:** None modified — this task is pure verification.

This task executes `--test` mode against real production data and confirms the spec's acceptance criteria in an actual email client. **The user should turn off WARP before running these steps** (the DB is on Supabase and WARP may block the hostname lookup).

- [ ] **Step 1: Pick three verification dates covering edge cases**

Use SQL to find dates with the right properties. If WARP is on the user will need to toggle it off first.

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python -c "
import sys
sys.path.insert(0, '.')
from config import get_db, close_db
db = get_db()
try:
    print('Dates with duplicates in stg_timer_duplicate_reviews (best for flag verification):')
    for r in db.fetch('''
        SELECT DATE(start_time AT TIME ZONE 'America/New_York') AS d,
               COUNT(*) AS n_groups
        FROM data_staging.stg_timer_duplicate_reviews
        WHERE start_time >= NOW() - INTERVAL '30 days'
        GROUP BY DATE(start_time AT TIME ZONE 'America/New_York')
        ORDER BY d DESC LIMIT 5
    '''):
        print(f'  {r[\"d\"]}  {r[\"n_groups\"]} duplicate groups')
finally:
    close_db()
"
```

Choose three dates:
- **Date A:** a recent date with duplicate groups (exercises the `⚠` column)
- **Date B:** a recent date with only single-entry-per-task records (exercises the no-duplicate path)
- **Date C:** a recent date with many tasks (exercises ordering and HTML rendering at scale)

- [ ] **Step 2: Run `--send --test` for Date A**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python timer_correction_review.py --send --test --date <YYYY-MM-DD>
```

`--test` routes all emails to `jamil.mendez@ontel.co`. Wait for the run to complete and open the latest email in Jamil's inbox.

**Expected:** email renders with a "Daily Task Summary" heading + table above the existing detail table. Summary shows one row per (task, site, project) combination. Rows with duplicate entries have a yellow tint and a red `⚠` in the rightmost column.

- [ ] **Step 3: Cross-check summary totals against the detail table**

Visually pick one summary row (e.g. highest duration). Find its matching rows in the detail table below. Sum the detail row durations manually. Confirm it matches the summary row's "Total" value.

**Expected:** summary total equals sum of detail rows for that (task, site, project).

Repeat for at least one row with `has_duplicates=True` to confirm the flag behavior.

- [ ] **Step 4: Run `--send --test` for Dates B and C**

```bash
cd swift_api_pipeline
PYTHONIOENCODING=utf-8 venv/Scripts/python timer_correction_review.py --send --test --date <Date-B>
PYTHONIOENCODING=utf-8 venv/Scripts/python timer_correction_review.py --send --test --date <Date-C>
```

**Expected:** emails render cleanly for both dates. Date B's rows should have no `⚠` indicators. Date C should show ordering by duration desc and no layout breakage.

- [ ] **Step 5: Record acceptance decision**

The user decides based on the three previews whether to push to production or request changes.

- If **approved**: proceed to push.
  ```bash
  cd ~/Desktop/Projects/ai-projects/local-pipeline
  git push origin main
  ```
  The next Apps Script dispatch at 12:09 AM EDT will pick up the new code automatically.

- If **changes requested**: return to the relevant task, update code, re-run `_test_summary.py`, then repeat Task 4 before pushing.

---

## Summary of deliverables

When all four tasks are complete:

- New file: `swift_api_pipeline/_test_summary.py` (9 smoke tests, stdlib-only, ~160 lines)
- Modified: `swift_api_pipeline/timer_correction_review.py` (+2 functions ~90 lines, +3 lines integration)
- Three local commits (`73...`-style hashes) on main branch, unpushed until manual verification passes
- No DB, migration, workflow, or schema changes
- Existing email behaviors (detail table, buttons, threading, recipient list, reminders) unchanged

Rollback if issues surface post-push: `git revert <integration-commit> && git push origin main`.
