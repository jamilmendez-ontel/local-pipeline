# Timer Daily Task Summary — Design Spec

**Date:** 2026-04-15
**Author:** Jamil Mendez (with Claude)
**Scope:** `swift_api_pipeline/timer_correction_review.py` — `--send` path only
**Status:** Design approved; awaiting implementation plan

---

## 1. Problem

Techs receive a Timer Activity Entries email every night listing their previous day's timer entries with per-entry Correct / Remove buttons. The email shows every row as-is, but offers no aggregated view. When a tech has multiple entries for the same task (including duplicates from Swift sync quirks), they can't tell at a glance how much total time that task consumed. The common question after reviewing entries — "how long did I spend on this task today?" — requires manual mental summing.

## 2. Goal

Add a Daily Task Summary table at the top of the email so techs can see "where my day went" at a glance before drilling into individual entries below.

## 3. Out of scope

- No database schema changes.
- No new Google Forms, Apps Script triggers, or GHA workflow changes.
- No changes to the `--apply` or `--remind` flows.
- No changes to the detail entries table.
- No changes to recipient logic, thread_id storage, or reminder subject matching.
- Correction/removal state is **not** reflected in the summary numbers (same raw-from-Swift behavior as the existing detail table).

## 4. User-facing behavior

### Placement
Directly under the greeting paragraph, above the existing bullet list that introduces the detail table. A new `<h3>Daily Task Summary</h3>` heading precedes the new table.

### Columns
Left to right:

| Column | Source | Notes |
|---|---|---|
| Task | `task_clean` | Falls back to `task` if `task_clean` is null. |
| Site | `site_name` | |
| Project | `project` | |
| Entries | count of raw rows in the group | e.g. `3` |
| Total Duration | sum of `duration_min`, formatted via `_fmt_duration()` | e.g. `2h 15m` |
| ⚠ | `has_duplicates` flag | `⚠️` in red if any rows in the group share duplicate detection key; em dash `—` otherwise |

### Row ordering
`total_duration_min` descending; `task` ascending for ties. Rationale: largest time-consumers first for quick signal-spotting.

### No interactivity
The summary table has no buttons. Correct / Remove remain exclusively on the detail table below (where each row has a unique `entry_id` to act on).

### Visual style
Matches the existing detail table — Arial 13px, same header background, 1px cell borders. Rows with `has_duplicates=True` get a subtle yellow-tinted background to call attention; the `⚠` cell is centered and colored red only for those rows.

## 5. Computation

### Input
The existing `entries: list[dict]` that `send_daily_emails()` already has in hand, populated by `get_previous_day_entries()` from `stg_timer_activities` for the target date in `America/New_York`. **No additional DB queries.**

### Grouping key
`(task_clean, site_name, project)`. Nulls are permitted and group together.

### Per-group output
```python
{
    "task": str,          # task_clean or task
    "site": str,          # site_name
    "project": str,       # project
    "entries": int,       # len(group_rows)
    "total_duration_min": float,  # sum of duration_min
    "has_duplicates": bool,       # see below
}
```

### Duplicate detection
A group has `has_duplicates = True` iff **any two rows** inside it share the full duplicate-detection key:

```
(project_did, user_email, start_time, site_name, site_id, task)
```

This mirrors the existing key in `detect_and_create_duplicate_reviews()` (~line 580). Note that duplicate detection uses raw `task`, while summary grouping uses `task_clean` — the summary key is *coarser* than the duplicate key, so a summary row can contain both distinct entries (different start_times) and duplicates (same start_time, different end_times). `has_duplicates` fires only for the latter.

### Raw vs. cleaned durations
Durations are summed **raw** from `stg_timer_activities` — inclusive of any duplicates. Rationale: the summary must visually tie to the detail table above it (which also shows raw rows). The `⚠` column signals where the total might be inflated by duplicates, so the tech can investigate and Remove if appropriate. Mirrors option C of the duplicate-handling design decision.

## 6. Code changes

### New helper
`_build_summary_html(entries: list[dict]) -> str` in `timer_correction_review.py`, adjacent to the existing `_build_entries_html()`. Pure function: takes the entries list, performs grouping + duplicate detection in Python, emits HTML.

### Call site
One small insertion inside `send_daily_emails()`'s `html_body` f-string, immediately after the greeting paragraph:

```python
<p>Hi {_first_name(user_email)},</p>
<p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
   from <strong>{date_str}</strong>.</p>

<h3 style="margin-top:20px;margin-bottom:8px;">Daily Task Summary</h3>
{_build_summary_html(user_entries)}

<ul style="font-size:13px;color:#555;margin:8px 0 16px;">
    ...
</ul>
```

### Files touched
- `swift_api_pipeline/timer_correction_review.py` — new function + one insertion.

### Files NOT touched
- `pipeline-timer.yml` (no workflow change needed; the same `--send` command produces the enhanced email)
- Any migration file
- Any Apps Script
- `extract_timer.py`, `transform.py`
- `_fmt_duration()` (reused as-is)

## 7. Testing & rollout

### Local test (pre-production)
```bash
cd swift_api_pipeline
venv/Scripts/python timer_correction_review.py --send --test --date 2026-04-14
```
`--test` routes all emails to `jamil.mendez@ontel.co`. Inspect in Jamil's inbox.

### Acceptance criteria
- Summary appears above the existing detail table, clearly labelled.
- For the test date, sum of `Total Duration` in the summary equals sum of `Duration` in the detail table.
- `Entries` column count per summary row equals number of detail rows matching that task+site+project.
- `⚠` fires on rows with known duplicate entries (verify against `stg_timer_duplicate_reviews`) and only on those rows.
- Row order is total-duration descending, task-ascending tiebreaker.
- Rendering doesn't break on any email client (Gmail web, Gmail mobile, Apple Mail — the usual suspects).
- Numbers formatted via `_fmt_duration()` match the detail table ("2h 15m", "45 min", etc.).

### Edge cases to explicitly test
| Case | Expected behavior |
|---|---|
| Day with only 1 entry | Single summary row, `entries=1`, `⚠=—` |
| Day with duplicates at same task+site+project | One summary row, `entries>=2`, `⚠=⚠️` |
| Day with same task across 2 different sites | Two summary rows (one per site) |
| Null `project` | Row still appears; empty cell shown |
| Tech with 0 entries | No email sent (existing behavior; unchanged) |

### Production deploy
After Jamil has verified against ≥2 test dates and is satisfied:
- `git push origin main`
- Next scheduled `pipeline-timer` dispatch from Apps Script (12:09 AM EST daily) picks up the new code automatically.
- No downtime, no migration, no credential rotation.

### Rollback
Single `git revert <commit> && git push origin main`. Pure HTML change; reversible in under a minute.

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Email rendering breaks in some client | Low | Inline CSS only (same pattern as existing table); test in Gmail web before push. |
| Summary totals don't match detail totals (computation bug) | Low | Pure in-memory computation on same list; local `--test` run verifies before push. |
| Increases email size near Gmail's 25MB limit | Very low | Adds ~10-50 rows of HTML (<5KB) per email; no change to scale. |
| Tech confusion from duplicates-inflated totals | Low-medium | ⚠ column signals the issue; the raw-sum-with-flag approach was explicitly chosen in design (option C) for exactly this reason. |

## 9. Future extensions (not in scope here)

- Show per-task *start → end* time range (e.g. "09:00 – 17:00").
- Summary roll-up across the week at end-of-week.
- Separate row/callout for entries already corrected/removed by the tech.
- Bookmark the last-completed-task for one-click "resume".

These are deferred until the basic summary has been live and battle-tested.
