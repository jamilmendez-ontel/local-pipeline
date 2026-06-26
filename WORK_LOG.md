# AI Projects - Work Log

## Session: 2026-06-25 — QA Forms pipeline stalled (trigger fix)

### Symptom
`data_staging.stg_qa_form` / `analytics.v_qa_forms` frozen at 2026-06-22 00:49 ET (3 days stale). User noticed "last refresh is 6-22".

### Root cause (Apps Script trigger, not the pipeline)
- `forms_extract` ran successfully daily through 6-22, then **zero** run records on 6-23/24/25 — the job was never invoked, not failing.
- GHA confirms it: `pipeline-forms` workflow received **no `repository_dispatch`** after 2026-06-22 04:29 UTC, while `pipeline-timer` (fired by the SAME Apps Script function) kept firing daily through 6-25.
- Both are dispatched by `triggerLightPipelines()` in `scripts/pipeline_trigger.gs`: Timer fires immediately (works), QA Forms fired only after `Utilities.sleep(8 * 60 * 1000)`. After the 6-22 redeploy (commits 9bc1fc8 retire-discrepancies + 6e2ed50 priorities refactor restructured the function), the post-sleep dispatch stopped executing, silently dropping QA Forms.

### Fix
Split QA Forms onto its own dedicated time-driven trigger, removing the in-process sleep:
- `triggerLightPipelines()` now fires Timer only (no sleep).
- New `triggerForms()` fires `pipeline-forms` from its own short execution (matches the triggerOrgs/triggerCalendarLeave/triggerAssetTasks pattern).
- Corrected the self-contradictory "6-min limit / 8-min sleep" header note.
- Audited the rest of `pipeline_trigger.gs`: no other trigger used sleep-staggering, so QA Forms was the only pipeline exposed to this failure mode.

### Manual steps required (code is inert until deployed)
1. Paste updated `pipeline_trigger.gs` into the Apps Script project.
2. Create a new time-driven trigger: function `triggerForms`, daily 12:00–1:00 AM EST.
3. Leave the existing `triggerLightPipelines` trigger (now Timer-only) in place.
4. (Optional) Check Apps Script Executions log for `triggerLightPipelines` 6-23→6-25 to confirm why the sleep was cut short.

Backfill of the 3 missing days deferred per user (not yet run).

### Second pipeline with the same problem: Open Items Report Data
Checked whether any other pipeline was similarly affected. Cross-referenced all 15
workflow `repository_dispatch` types against the event types the committed
`pipeline_trigger.gs` actually fires:
- **`pipeline-open-items-data`** (targeted_asset_tasks + targeted_task_requirements)
  has the identical signature as forms: clean daily success at 02:36 ET through 6-22,
  then zero dispatches. `stg_targeted_asset_tasks` / `stg_targeted_task_requirements`
  frozen at 6-22 02:44 ET → the Open Items Report (Mon/Fri) ran on stale data.
- Root cause: its trigger function `triggerOpenItemsData` **was never committed** — it
  existed only in the deployed Apps Script editor and was lost in the 6-22 redeploy.
  Same date as forms because both were collateral of the 6-22 redeploy.
- Added `triggerOpenItemsData()` to the committed source (fires `pipeline-open-items-data`,
  ~02:00 AM EST, after priorities) so it's recreatable; updated header schedule + testAllDispatches.
- All other dispatch types missing from the trigger are fired by other mechanisms
  (downstream chain, own cron, cross-repo, app) and are fresh as of 6-25.
- `asset_tasks_gc_extract` is also stale (since 6-03) but that's the separately-known
  paused GC pilot failing on timeouts, NOT this trigger-drop bug.

### Trigger design — evaluated event-driven, kept standalone 2 AM timer
Considered firing OIR as a downstream event off the nightly asset-tasks run (briefly
shipped, then reverted). Decision: keep OIR on its **own standalone Apps Script time
trigger** (~02:00 AM EST), matching every other pipeline (Orgs/Timer/Calendar).
Reasoning:
- GHA cron is off the table by convention (unreliable → we use Apps Script triggers).
- The 6-22 failure was NOT the standalone-timer pattern (Orgs/Timer/Calendar use it and
  never broke). It was solely that `triggerOpenItemsData` was never committed, so a
  redeploy from source wiped it. Fix = keep the function committed (done) — then the
  timer is as reliable as the others.
- Standalone keeps OIR fully decoupled and runs it in isolation (~2 AM, no concurrency
  with the asset-tasks/invoicing tail), vs. the event approach which coupled OIR to
  asset-tasks' health and overlapped its tail jobs.
- OIR has no real data dependency on asset-tasks anyway: it runs its own report-scoped
  `targeted_asset_tasks` extract and reads `stg_user_priorities` (fresh every 10 min).

### CONFIRMED root cause (6-26, via Apps Script execution log)
The time trigger was never lost — it fires nightly (~2:36 AM). The bound *function*
was gone. Execution log showed, every night since 6-23:
`Error  Script function not found: triggerOpenItemsData`. The trigger's 57.14%
failure rate = exactly 4 of the last 7 days (fails 6-23/24/25/26, succeeds 6-20/21/22).
So the 6-22 redeploy from committed source (which lacked the function — the drift)
removed the function while the trigger survived, and every fire since threw "not found"
→ no dispatch → OIR stale. (My earlier "lost trigger" wording was wrong; it was a lost
function.)

### Resolution (6-26)
- Restored `triggerOpenItemsData()` to committed source (`168a25b`) with a DO-NOT-REMOVE
  guard, and synced local main.
- User pasted the function back into the deployed Apps Script project and saved.
- Manual Run of `triggerOpenItemsData` fired the dispatch successfully → a new
  `pipeline-open-items-data` run started on GitHub and reloaded `stg_targeted_*`
  (verified the function → fireDispatch_ → GitHub → workflow chain works end-to-end).
- No new trigger needed — the existing ~2:36 AM trigger now finds the function and
  succeeds. Future redeploys can't lose it since it's committed.

### Files touched
- `scripts/pipeline_trigger.gs`

---

## Session: 2026-06-16 — asset_did matching overhaul (timer + QA), migration 104

### Context (02:00–02:30 ET)
Resumed the asset_did path-collision work (migrations 099/100). Audited the timer clean table: 0 dids on 6+ sites confirmed, but the long tail still held a ~33-row mislink class (e.g. `9JK2240A (Civil)` -> non-civil parent `9JK2240A`), and the QA form was still on the old "take-first" (`DISTINCT ON`) + poisoned `qa_form_asset_did_lookup` logic.

### Investigation (02:30–03:30 ET)
- Matching already uses exact `=` on asset_name/asset_id; the mislinks come from the name-blind site_id-path / FA fallback, not loose matching.
- Measured project-scope effect: dropping scope alone loses ~4,000 name matches, but the combined (path + name) key absorbs that. site_id paths are shared batch folders (3,350 timer sites map to ~17 candidate assets each); site_name recurs across projects/efforts (e.g. `7WDC100B` = a Replacement asset and a Decom asset). Neither key alone is reliable; the combination is.
- Verified 0 timer entries still reference multiple assets after correct resolution; remaining problems were name-mismatch (256 sites: path -> 1 wrong-named asset) or orphan path (145 sites: site_id on no asset).
- Simulated the new design unscoped: timer +244 net, 11 re-points (all exact-name corrections), 1,421 wrong links -> NULL, 99.2% unchanged. QA: 37,208 wrong links corrected, 0 regressions.

### Implementation — migration 104 (03:30–04:00 ET)
Rewrote `data_staging.backfill_asset_did()` for BOTH timer and QA:
- Pass A: `asset_id = site_id AND TRIM(asset_name) = TRIM(site_name)`, unique did (path breaks name ties, name breaks path ties).
- Pass B: `TRIM(asset_name) = TRIM(site_name)`, globally unique did (cross-project moves + shared paths).
- Dropped: project scope, the site_id-path-only pass, the FA-number pass, and (QA) the poisoned lookup restore + take-first DISTINCT ON.
- Principle: only link when the asset name matches what the tech typed; otherwise leave NULL.

### Repair + verification (04:00–04:20 ET)
Re-derivation (NULL all asset_did -> backfill -> rebuild_timer_clean) run via `out/*_repair.py` over the Supavisor pooler, since the full backfill exceeds the MCP 2-min ceiling.
- Timer: base 266,955 -> 267,199 linked; clean 263,350; 0 dids on 6+ sites; 0 name-mismatched linked rows; `9JK2240A (Civil)` -> NULL; EDGEWATER 2 SC / ROBBINSVILLE / HOPE / LEB MILL STREET re-pointed to their exact-name assets.
- QA: 386,531 -> 383,230 linked; 37,208 wrong links corrected; 0 name-mismatched, 0 smears. Closes the QA follow-up open since 099/100.

### Files touched
- `swift_api_pipeline/migrations/104_asset_did_combined_key_no_project_scope.sql` (new)
- `swift_api_pipeline/migrations/099_*.sql`, `100_*.sql` (prior series, committed now — were untracked)
- `swift_api_pipeline/out/timer_asset_did_repair.py`, `qa_asset_did_repair.py` (repair record), `timer_asset_link_inspect.py`, `timer_multi_asset_inspect.py`, `timer_problematic_links.py` (analysis)

This document tracks all development sessions, changes made, and issues identified across both projects.

---

## Session: 2026-05-12 — Timer Overlap-Based Duplicate Detection

Extended timer duplicate detection from "same start_time only" to "any temporal overlap on the same task". Old rule caught only entries that shared an exact start_time; new rule also catches the common real-world case where a tech accidentally starts a second timer for the same task while the first is still running (e.g. Timer A 09:30–11:30 + Timer B 10:00–11:20 inside A).

### Brainstorm decisions

Spec at `docs/superpowers/specs/2026-05-12-timer-overlap-duplicate-detection-design.md`. Scope: same task only — group by `(project_did, user_email, site_name, site_id, task)`. Overlap rule: any temporal intersection (widest option; same-start is a special case). Auto-resolve: keep today's "latest end_time wins" rule unchanged. NULL end_time: skip from clustering, same as today. UI: existing DUPLICATE badge, no new visual treatment. Cross-midnight gap (open timers slipping past detection because the daily query only fetches yesterday) noted as out of scope here.

### Implementation

Plan at `docs/superpowers/plans/2026-05-12-timer-overlap-duplicate-detection.md`. Executed via subagent-driven development on branch `feature/timer-overlap-dup`. Seven planned tasks plus one follow-up bug fix uncovered by reviewing a real email:

- `3045a40` Pure `_intervals_overlap` predicate + 6 boundary tests.
- `778c956` Union-Find `_build_overlap_clusters` for connected components by time overlap (handles transitive A↔B↔C clusters where A and C don't directly touch).
- `cd98952` Refactored `detect_and_track_duplicates` to bucket without `start_time` and use the cluster logic. `group_id` anchors on the cluster's earliest start_time — for same-start clusters this produces the legacy value, so existing pending reviews keep their Google Form thread IDs.
- `eea4b75` Strengthened the group_id-stability regression test to walk the actual clustering pipeline (the original draft was tautological — code reviewer caught it).
- `db28108` `_build_entries_html` DUPLICATE badge now uses the same cluster logic — single source of truth between the email row badges and the persisted review records.
- `4f14422` Aligned `_has_duplicate_entries` and the email copy ("share the same start time" → "overlap in time on the same task") with the new rule. Code reviewer caught that the badge would fire without the explanatory bullets because the gate that injects them was still on the old key.
- `e4df11c` Migration 049 SQL: rewrites `data_staging.rebuild_timer_clean()` so the rejected-entry exclusion and the unresolved-keep-latest subquery both join on `(elem->>'start_time')::timestamptz` from JSONB instead of the parent column. Includes an idempotent backfill that injects `start_time` into existing `entries[]` and `rejected_entries[]` arrays.
- `ae2e755` Applied migration 049 to cloud Supabase via `apply_049.py`. Pre/post counts: 1,490 review rows backfilled, all rows now carry `entries[0].start_time`, parent and JSONB values match on spot-check.
- `00a770b` First WORK_LOG verification stub (since rewritten — this entry).
- `551ad92` Bug fix from inspecting the real May 11 email landed in jamil's inbox. Two issues hit at once: (1) `_intervals_overlap` used strict-less-than on both sides, so a 0-minute timer mis-fire at 08:54 plus a real 08:54–09:09 entry was missed because `b_start < a_end` reduced to `08:54 < 08:54` = False — the spec's same-start invariant wasn't enforced when one interval was degenerate. (2) `_compute_summary_groups` still used the old exact-match key, so the Daily Task Summary's ⚠ column and the row badges could disagree. Fixed: same-start always counts as overlap regardless of duration, and the summary now uses the cluster helper too.

### Verification

Ran `python timer_correction_review.py --send --test --date 2026-05-11` after the fix — sent 81 emails to jamil covering 671 entries. Confirmed in the rendered HTML that mila's email shows the DUPLICATE badge on both the REYNOLDSON Data Pre-Fill same-start pair (identical entries) AND the (no site)/Tools and Automation 08:54–08:54 + 08:54–09:09 pair (zero-duration same-start, the fix). glaiza's email shows the new partial-cross-over case — "1. General Admin and Support" entries 08:54–16:31 and 13:00–16:59 (different starts, intersecting windows) flagged across a transitively-merged 4-entry cluster.

Also tested the per-entry JSONB join in `rebuild_timer_clean()` end-to-end: marked a real review's entries[0] as rejected, ran rebuild, confirmed the rejected natural-key was excluded from `stg_timer_activities_clean` (leftover_count = 0), then reverted to the original state and re-ran rebuild to restore the clean table.

### Files touched

- `swift_api_pipeline/timer_correction_review.py` — added `_intervals_overlap` and `_build_overlap_clusters`, refactored `detect_and_track_duplicates` and `_build_entries_html`, aligned `_has_duplicate_entries` and `_compute_summary_groups`, updated email copy.
- `swift_api_pipeline/tests/test_timer_overlap.py` — new file, 17 tests covering overlap math, cluster building, group_id stability, badge consistency, and zero-duration same-start.
- `swift_api_pipeline/migrations/049_timer_overlap_dup_detection.sql` — new function body + idempotent JSONB backfill.
- `swift_api_pipeline/migrations/apply_049.py` — apply script with pre/post backfill verification.

10 commits on `feature/timer-overlap-dup`, ready to merge.

---

## Session: 2026-02-26 (Part 2)

### New Project: Gmail Scraper (`gmail-scraper/`)

Built a new standalone project at `C:\Users\admin\Desktop\Projects\ai-projects\gmail-scraper` connected to `https://github.com/jamilmendez-ontel/gmail-scraper.git`.

**Purpose:** Scrape Gmail inbox for COP (Close Out Package) emails sent to/from Ontel teams, extract structured data from the email body table, and store in Supabase for downstream use.

#### Architecture

Same DB pattern as local-pipeline: asyncpg pool on a background event loop thread with sync bridge via `run_coroutine_threadsafe()`.

Files:
- `config.py` — env vars, logging, schema constants
- `db.py` — asyncpg pool + sync bridge (singleton `get_db()`)
- `gmail_client.py` — OAuth2 auth, paginated search, recursive HTML/plain-text body extraction, retry logic on `get_full_message()`
- `extractor.py` — incremental `run_scraper()`: queries `MAX(received_at)` → builds `after:` filter → dedup by message_id → batch insert to `data_raw.raw_emails` + `data_staging.stg_emails`
- `parser.py` — `run_parser()`: reads `stg_emails.html_body`, finds first "CLOSE OUT PACKAGE" table, extracts all label:value pairs into JSONB → upserts `data_staging.stg_cop_emails`
- `main.py` — CLI: `--reprocess`, `--parse-only`, `--reparse`, `--query`, `--max-results`
- `scheduled_gmail_scraper.bat` — Windows Task Scheduler wrapper

#### Gmail Query

Final query: `swiftprojects.io from:ontel.co`

Iteration:
1. Started with `to/cc:vzw.cgc@ontel.co` — missed FTTH, AAHI, MP team emails
2. Added `-subject:Re:` + 8-digit number filter — too restrictive, missed valid Re: emails with COP tables
3. Switched to `swiftprojects.io` body filter — catches all COP emails regardless of team or Re: prefix
4. Added `from:ontel.co` — excluded pipeline notification emails from `jamil.mendez@nanoninth.com` that also referenced swiftprojects.io

#### DB Tables (applied to Supabase cloud)

- `data_raw.raw_emails` — message_id (PK), thread_id, sender, recipients (JSONB), subject, received_at, html_body, headers (JSONB), labels (JSONB)
- `data_staging.stg_emails` — parsed: sender_email, sender_name, recipients_to (TEXT[]), recipients_cc (TEXT[])
- `data_staging.stg_cop_emails` — package_type, fields (JSONB all label:value pairs), dropbox_url, swift_url, parse_error

#### COP Email Parser

Identified two distinct HTML layouts and multiple email types (REVIEW, REVISION, PMI). Key insight: the important data is always in the **first "CLOSE OUT PACKAGE" table** in the email body.

Parser strategy:
1. BeautifulSoup finds the cell containing "CLOSE OUT PACKAGE" → determines package_type (REVIEW/REVISION/PMI)
2. Walks up to the containing `<table>`
3. For each `<tr>`, uses `recursive=False` to get only direct child cells (avoids nested table bleed)
4. Labels = `<th>` tags OR `<td>` text ending with ":"
5. All pairs stored in `fields` JSONB — new field types appear automatically without schema changes
6. Dropbox/Swift URLs extracted from `<a href>` tags

parse_error is set (not null) for emails that contain swiftprojects.io in quoted text but have no actual COP table — expected for conversation threads.

#### Issues Encountered and Fixes

**1. Wrong Gmail account (nanoninth vs Ontel)**
- Copied `token.pickle` from `local-pipeline/swift_api_pipeline/gmail_credentials/` assuming it was the Ontel account
- First run returned 28 emails from nanoninth senders (`jamil.mendez@nanoninth.com`, `myka.florano@ontel.co`) — pipeline notification emails, not COP emails
- Root cause: that token was used for sending pipeline notification emails (nanoninth Gmail account)
- Fix: deleted the token, re-ran to trigger browser OAuth — logged in with Ontel account
- Cleared the 28 nanoninth emails from DB and re-ran with correct account

**2. Gmail API timeout on first Ontel run**
- First Ontel run fetched 500 inbox emails; timed out (`TimeoutError: The read operation timed out`) at message ~401/500
- Nothing had been inserted yet (fetch happens before insert) so re-run was safe
- Fix 1: Added 5-retry exponential backoff to `get_full_message()` in `gmail_client.py`
- Fix 2: Reduced `BATCH_SIZE` from 50 to 25 to avoid DB timeout on large HTML bodies

**3. Pipeline notification emails in results**
- After switching to `swiftprojects.io from:ontel.co`, 18/38 emails were "Pipeline SUCCESS/FAILED: Asset Tasks" notifications from `jamil.mendez@nanoninth.com`
- These contained `swiftprojects.io` because the pipeline email HTML referenced it
- Fix: added `from:ontel.co` to Gmail query — excluded nanoninth senders entirely

#### Sample Email Analysis

Analyzed 4 `.eml` files to understand body structure and identify the universal filter:
- `TEALBROOK - PMI COP Complete` (vzw.cgc) — Layout A with site photo, `<th>` labels
- `D-HDT238 - FTTH - COP Review` (ftth@ontel.co) — same body structure, different team
- `JUPITER STADIUM - Re: COP Review` (vzw.aahi@ontel.co) — Re: email but still has COP table
- `Interlocken - Re: COP Review` (vzw.mp@ontel.co) — Layout B (dark header, no photo)

Key finding: `swiftprojects.io` appears in **every** COP email body regardless of team, layout, or Re: status. Used as the primary Gmail body filter.

#### Sender Breakdown (last 30 days)

9 distinct `@ontel.co` senders captured: `vzw.cgc`, `ftth`, `vzw.bawa`, `vzw.norcal`, `vzw.mp`, `att.oh`, `merjien`, `darren`, `jamil.mendez`

#### Parse Results (initial run, 20 emails)

- 5 successfully parsed — had the COP table embedded (REVIEW and REVISION types confirmed working)
- 15 parse errors (`no CLOSE OUT PACKAGE header found`) — conversation/reply threads where swiftprojects.io only appeared in quoted text, no actual COP table present. Expected behavior.

#### Authentication

Re-used `credentials.json` from `local-pipeline/swift_api_pipeline/gmail_credentials/` (same Google Cloud project). Re-authenticated with Ontel account to get a fresh `token.pickle` with `gmail.readonly` scope.

#### Task Scheduler

`GmailScraper-Nightly` task registered via PowerShell — runs daily at 11:00 PM. Logon mode: Interactive only (runs while logged in; to run while logged out, configure password in Task Scheduler GUI).

---

## Session: 2026-02-26 (Part 1)

### Pipeline Failure Handling Improvements

Added three layers of resilience to the asset tasks extraction pipeline to handle sustained 503 errors on individual projects (e.g., TS16) without requiring a full re-run.

#### Change 1: Project-Level Auto-Retry (`extract_asset_tasks.py`)

After the parallel extraction finishes, if any projects failed, the pipeline now:
1. Waits 5 minutes (`RETRY_WAIT_SECONDS = 300`)
2. Deletes partial raw rows for each failed project (`project_did + run_id`) — clean slate
3. Re-extracts each failed project sequentially
4. Only marks as failed if the project still fails after retry

Retry happens *before* index restore so writes are still fast (no indexes during retry).

#### Change 2: Single-Project Recovery Mode (`extract_asset_tasks.py`, `main.py`)

New `project_filter` parameter on `run_asset_task_pipeline()` enables manual recovery of a single project the morning after a failure:

```bash
python main.py --pipeline asset_tasks --project TS16
```

Behavior:
- Reuses the latest `run_id` from `pipeline_runs` (success or failed) — no new run created
- Removes stale rows for the matched project only; all other projects untouched
- Re-extracts the matched project
- Recalculates total: `existing_rows - old_project_rows + new_rows`
- Marks the pipeline run as `success`
- Automatically runs transforms + `backfill_asset_did()` + `refresh_analytics()`
- Sends success email via `run_pipeline_with_notification`

Works for any project (TS13–TS18 etc.), not just TS16. Filter is substring match on `project_name`.

Also added a `--project` CLI guard in `main.py`: errors immediately if used without `--pipeline asset_tasks`.

#### Change 3: Export Guard in `.bat` (`scheduled_main_pipeline.bat`)

Captures `main.py` exit code into `%PIPELINE_EXIT%` before anything else runs. If non-zero, logs "Pipeline FAILED - skipping all exports" and jumps to `:end`, skipping all three Excel exports.

### Dry Run Results (2026-02-26 04:18–05:57 ET)

Ran a full nightly pipeline dry run (`python main.py --no-email`) to validate the new retry/recovery code. The run encountered a real TS16 503 outage — validating the feature in a live scenario.

**Timeline:**
- `04:18:35` — Pipeline started
- `04:20:05` — TS16 asset tasks began, immediately hit sustained 503s
- `04:20:36–04:27:19` — 10 per-page retries all failed (503s for ~7 min)
- `04:27:49` — TS16 marked FAILED, other 5 projects continue in parallel
- `05:06:28` — All other projects finished; **new project-level retry fired**: `WARNING: Retrying 1 failed project(s) after 300s: ['TECH-OPS: TS16']`
- `05:12:09` — TS16 retry started; hit 2 more 503s then recovered on page 3
- `05:47:19` — TS16 completed (402,248 rows); all 6 projects done, pipeline marked success
- `05:57:40` — Full pipeline complete (all 7 steps SUCCESS)

**Final results:**

| Pipeline | Status | Records |
|----------|--------|---------|
| Orgs & Projects | SUCCESS | 1,420 |
| User Priorities | SUCCESS | — |
| Timer Activities | SUCCESS | — |
| QA Forms | SUCCESS | 352,415 |
| Asset Tasks | SUCCESS | 2,269,586 |
| Asset DID Backfill | SUCCESS | Timer: 2,184 / QA: 337,816 / Carrier: 29,107 |
| Analytics MV Refresh | SUCCESS | 3 MVs (~20–36s each) |

**Total runtime: 1h 39min** (vs. normal ~55 min). Extra time entirely due to TS16 503 outage + retry sequence. No manual intervention required — pipeline self-healed.

**Key observations:**
- TS16 503s lasted 45+ min; per-page retries (10×) cannot bridge outages that long — project-level retry is the right mechanism
- The 5-min `RETRY_WAIT_SECONDS` was effectively redundant tonight (other projects took 39 min to finish, API had already recovered by retry time)
- On clean nights with no 503s, runtime remains ~55 min

---

## Session: 2026-02-25

### n8n Integration: Supabase Connection

Set up n8n credentials for Supabase:
- **Supabase API node**: Host `https://voqfjfngdpcvevbkikud.supabase.co` + service role key
- **Postgres node**: Direct DB connection hit IPv6 `ENETUNREACH` error — `db.voqfjfngdpcvevbkikud.supabase.co` only resolves to IPv6
- **Fix**: Use Supabase pooler instead: `aws-0-ap-southeast-1.pooler.supabase.com` with user `postgres.voqfjfngdpcvevbkikud` (Supavisor connection pooler supports IPv4)

### Fix: Calendar Pipeline NameError

**Problem**: Calendar pipeline failed at 12:30 AM with `NameError: name 'mode' is not defined`. Caused by yesterday's change removing the `mode` variable from `run_label` — missed a second reference to `mode` in the logger on line 647 (`run_calendar_leave_pipeline()` function).

**Fix**: Changed `logger.info(f"Calendar Leave Pipeline ({mode})")` → `logger.info("Calendar Leave Pipeline")`. The `mode` variable still exists in the inner `_run_pipeline()` function for pipeline metadata.

**Rerun**: Calendar pipeline ran successfully — 778 incremental events upserted, email sent.

### Nightly Pipeline Status (Feb 25)

All pipelines completed successfully by 12:56 AM ET (~55 min):

| Pipeline | Duration | Records |
|----------|----------|---------|
| Orgs & Projects | 1.0 min | 1,418 |
| User Priorities | 0.9 min | 10,523 |
| Timer | 0.3 min | 2,553 |
| Forms | 11.1 min | 351,799 |
| Asset Tasks | 43 min | 2,264,828 |
| Asset DID Backfill | ~49s | Timer: 2,137 / QA: 337,200 / Carrier: 29,038 |
| MV Refresh | ~1.7 min | 3 MVs refreshed |

### Timer Data Recovery Update

TS18 timer data partially recovering from Swift API:

| Date | TS18 Rows | TS18 Range |
|------|-----------|------------|
| Feb 23 | 3,998 | Feb 11–22 (baseline) |
| Feb 24 | 994 | Feb 20–23 (lost Feb 11–19) |
| **Feb 25** | **1,987** | **Feb 19–24** (Feb 19 back, still missing Feb 11–18) |

---

## Session: 2026-02-24

### Metadata Enrichment: Calendar Leave Views

**Migration** (applied via Supabase MCP `enrich_calendar_leave_metadata`):
- Updated 3 existing `v_calendar_leave` rows with `business_context` and `example_values`
- Added 13 new column-level metadata rows for `v_calendar_leave`
- Added 2 column-level metadata rows for `stg_calendar_leave` (`team_normalized`, `leave_type_normalized`)

### New View: analytics.v_calendar_leave_daily

**Purpose**: Daily exploded view — one row per person per day on leave. Multi-day events (e.g. 5-day vacation) are expanded into 5 individual date rows using `generate_series()`. Makes day-level queries much simpler (no more `BETWEEN start_date AND end_date`).

**Migration 033** (`migrations/033_analytics_calendar_leave_daily.sql`):
```sql
CREATE OR REPLACE VIEW analytics.v_calendar_leave_daily AS
SELECT event_id, d::date AS leave_date, ...
FROM data_staging.stg_calendar_leave,
     generate_series(start_date, end_date, interval '1 day') AS d;
```

**Schema metadata**: 14 rows added via migration (`metadata_calendar_leave_daily`).

**Claude project prompt** (`docs/claude_project_prompt.md`):
- Added `v_calendar_leave_daily` (~13K rows) documentation
- Added 3 new daily view example queries (who's on leave today, headcount per day, team absences per day)
- Updated guidance: use `v_calendar_leave_daily` for day-level queries, `v_calendar_leave` for event-level

### Investigation: Timer Staging Data Drop (-2,967 rows)

**Symptom**: Pipeline email showed `stg_timer_activities` went from 267,709 → 264,742 (-2,967) while raw went from 91,269 → 92,810 (+1,541).

**Root cause**: Swift API stopped returning TS18 timer data before Feb 20. This is an API-side issue, not a pipeline bug.

| Run | Total Raw | TS18 | TS18 Range | TS17 | TS16 |
|-----|-----------|------|-----------|------|------|
| Feb 23 (yesterday) | 4,508 | 3,998 | Feb 11–22 | 481 | 29 |
| Feb 24 (nightly) | 1,541 | 994 | Feb 20–23 | 513 | 34 |
| Feb 24 (manual rerun) | 1,541 | 994 | Feb 20–23 | 513 | 34 |

- **90 users** in TS18 affected (everyone who logged time Feb 11–19)
- TS17 and TS16 unaffected (gained a few entries from new activity)
- Manual rerun confirmed identical results — data is missing from the API itself
- **Action**: Flag with Swift API team that TS18 timer history for Feb 11–19 is gone

### Fix: Calendar Pipeline ModuleNotFoundError

**Problem**: Calendar pipeline (scheduled 12:30 AM) failed with `ModuleNotFoundError: No module named 'anthropic'`. The `anthropic` package was installed in the system Python 3.14 but not in the venv used by Task Scheduler.

**Fix**: `pip install anthropic` in the venv (`swift_api_pipeline/venv/`). Installed v0.83.0.

**Verification**: Ran calendar pipeline successfully — picked up 10 incremental events, email sent to all 4 recipients.

### Pipeline Email Recipient

Added `merjien@ontel.co` to `NOTIFICATION_RECIPIENTS` in `pipeline_notifier.py` (now 4 recipients).

### Nightly Pipeline Status (Feb 24)

All pipelines completed successfully by 1:01 AM ET (~59 min total):

| Pipeline | Duration | Records |
|----------|----------|---------|
| Orgs & Projects | 0.9 min | 1,418 |
| User Priorities | 1.4 min | 10,462 |
| Timer | 0.3 min | 1,541 |
| Forms | 12.4 min | 351,001 |
| Asset Tasks | 48.3 min | 2,259,212 |
| Asset DID Backfill | ~47s | Timer: 1,337 / QA: 336,400 / Carrier: 28,969 |
| MV Refresh | ~1.5 min | 3 MVs refreshed |

Some 503 retries during asset_tasks extraction (normal). One `ConnectionDoesNotExistError` on COPY retry (recovered).

### Timer Data Verification: Manual Pull vs Pipeline

Compared manual Swift API export (`TimeData_202602_20260224.zip`) against pipeline data:

| Project | Manual Pull | Pipeline | Diff |
|---------|------------|----------|------|
| TS18 | 992 (Feb 20–23) | 994 (Feb 20–23) | -2 |
| TS17 | 511 (Feb 2–23) | 513 (Feb 2–23) | -2 |
| TS16 | 34 (Feb 2–23) | 34 (Feb 2–23) | 0 |

Tiny -4 difference from timing (entries created between pulls). **Manual pull confirms TS18 Feb 11–19 data is missing from the API itself.**

### Asset Tasks Export: Add Email Recipients

Added `hajie@ontel.co` and `sheena@ontel.co` to `export_asset_tasks_excel.py`:
- Changed `EMAIL_RECIPIENT` (single string) → `EMAIL_RECIPIENTS` (list of 3: jamil, hajie, sheena)
- `send_export_email()` now sends to all recipients in one email

### Calendar Leave Email: Remove Mode Label

Removed "(INCREMENTAL)" / "(FULL REFRESH)" from the email subject. Now just says "Calendar Leave".

### Gmail Pipeline: Increase Frequency

Changed `SwiftPipeline-Gmail-Hourly` Task Scheduler repetition interval from **30 min → 15 min** to capture AR aging and sales detail emails earlier. Schedule: every 15 min from 1:00–10:00 AM.

### Claude Project Prompt: Calendar Leave Documentation

Enhanced `docs/claude_project_prompt.md` so the AI better understands calendar leave data:
- Added **Teams & Leave** domain context (team types, leave codes, employee nicknames)
- Moved calendar views into their own **"Calendar Leave Views (HR / people data)"** section (was mixed under Materialized Views)
- Added **"Choosing the right view"** guidance (`v_calendar_leave` for event-level, `v_calendar_leave_daily` for day-level)
- Added calendar leave note to **Query Guidelines** (view selection + `COUNT(*)` vs `SUM(days)` gotcha)
- Expanded example queries from 5 → 8 (added: tomorrow's leave, team breakdown, top leave takers)

### DARA: Daily Finance Report — Full Build (13 sessions)

**Purpose**: Built a fully automated 5-page Daily Finance Report as a self-contained HTML artifact with Chart.js visualizations, generated by Claude from Supabase data + Excel supplementary data.

**Pages built**:
- **Page 1**: Total Pending Invoicing Tasks — summary cards, pending PO line chart (10 business days), aging stacked bar chart, daily trend table
- **Page 2**: Overdue Invoices Per Customer — donut chart + customer breakdown table by aging bucket
- **Page 3**: Total Pending P.O. Per Customer — customers with `po_number LIKE 'Requested%'`, donut + table
- **Page 4**: Payment Average vs Days Past Due — SQL for overdue days + Excel extraction for payment averages
- **Page 5**: PO Status & QP-to-PO Duration — Excel-only (`PDF_Raw5` sheet), two side-by-side tables

**Key discoveries**:
- Trend X-axis must use `email_received_date` (not `as_of_date`) to align with business days
- PO classification: actual PO (`PO-2026-001`), Requested (`'Requested 01/19/2026'`), NULL/empty — Page 1 Pending PO = `LIKE 'Requested%' AND past_due > 0`
- Chart.js stacked bars needed custom `stackedTotalPlugin` for total labels
- `Ontel_Icon.png` is actually a JPEG with black background — embedded correct transparent PNG base64 directly in template

**Deliverables**:
| File | Purpose |
|------|---------|
| `DARA_daily_finance_report_prompt_v3.md` | Prompt spec — SQL queries, formulas, rules, generation steps |
| `DARA_daily_finance_report_html_template_reference.md` | HTML template — CSS, JS, Chart.js configs, embedded logo (848 lines) |
| `daily_finance_report_20260224.html` | Generated report for Feb 24, 2026 (verified) |
| `DARA_daily_finance_report_worklog.md` | Detailed 13-session worklog |

**Open items**:
1. Page 4 payment averages depend on Excel `Payments` sheet upload (could be automated if payment data lands in Supabase)
2. Page 5 entirely Excel-dependent (PO status tracked externally)
3. Full automation possible: n8n → Claude API → HTML artifact → email distribution

---

## Session: 2026-02-23

### DARA: Daily Finance Report Prompt

**Purpose**: Created a project prompt for the DARA Claude project (Anthropic console) to auto-generate the Daily Finance Report — a 5-page report currently produced manually from an Excel file (.xlsb → PDF).

**Report structure** (mapped from `Daily Finance Report_02232026.pdf`):
- **Page 1**: Total Pending Invoicing Tasks — summary cards, pending PO line chart (10-day trend), aging stacked bar chart, daily trend table
- **Page 2**: Overdue Invoices Per Customer — aging bucket breakdown by customer, sorted by largest overdue balance
- **Page 3**: Total Pending P.O. Per Customer — invoices missing PO numbers, sorted by largest balance
- **Page 4**: Payment Average in Days vs Average Days Past Due — per-customer payment speed and overdue days
- **Page 5**: PO Status & QP to PO Duration — **cannot be auto-generated** (data not in Supabase, tracked externally)

**Data source**: `data_staging.stg_ar_aging` (QuickBooks AR aging snapshots). Key finding: report only counts `transaction_type = 'Invoice'` — must exclude Payment, Credit Memo, and Expense rows. Verified all aging bucket numbers match the PDF exactly with this filter.

**Prompt features**:
- Full SQL queries for each page section
- Chart.js specifications (line chart, stacked bar, donut charts with colors)
- Sorting rules per section
- HTML artifact output with dark navy theme
- Aging bucket color mapping (green → blue → orange → red)

**Gaps noted in prompt**:
- Page 4 "Payment average in days" requires payment receipt dates (not in current data) — shows "No Data"
- Page 5 requires QP-to-PO and PO status data from external system
- Page 1 "Invoicing Tasks with pending P.O." ($42,375) — exact filter unclear, may differ from simple `po_number IS NULL`

**File created**: `C:\Users\admin\Downloads\DARA_daily_finance_report_prompt.md`

### Fix: Prevent Pipeline Deadlock on Queue.join()

**Problem**: The nightly pipeline hung for 22+ hours on Feb 23. Root cause: when `load_batch()` blocks forever inside the loader worker thread (due to DB connection pool starvation during Phase 2), `task_done()` is never called, and `queue.join()` blocks indefinitely.

**Fix**: Added `_queue_join_with_timeout()` helper that mirrors `Queue.join()` but raises `RuntimeError` after a timeout, allowing the pipeline to fail cleanly instead of hanging forever.

**Files changed**:
| File | Timeout | Rationale |
|---|---|---|
| `extract_forms.py` | 3600s (1 hr) | QA forms: worst case 71 min total runtime |
| `extract_timer.py` | 600s (10 min) | Timer: always finishes in <1 min |
| `extract_requirements.py` | 1800s (30 min) | Dead file (not called in main.py), added for completeness |

**Behavior**: Normal runs unaffected. Deadlock scenario: pipeline raises `RuntimeError`, marks run as failed, sends failure email, exits cleanly.

**Note**: Task Scheduler already has PT3H (3 hour) execution time limit as secondary safety net. Changing to 2h requires re-entering admin password via Task Scheduler GUI.

### Enhancement: Add Data Freshness Metadata to Asset Tasks Export Email

**Purpose**: Add `loaded_at` and `run_id` to the asset tasks export email so recipients can confirm the Excel file contains data from the latest pipeline run.

**Changes**: Modified `scripts-reference/export_asset_tasks_excel.py`:
- Added metadata query in `export()` to fetch latest `loaded_at` and `run_id` from `stg_asset_tasks`
- Updated `send_export_email()` to display **Data Loaded At** (Eastern Time) and **Pipeline Run ID** in the email summary table
- Updated `main()` to pass new values through

**Verification**: Full export + Drive upload + email tested successfully.
- 2,257,028 rows across 6 tabs in ~4.7 min
- Email shows: Data Loaded At = Feb 23, 2026 12:54 AM ET, Run ID = 2679f242-...

### New Pipeline: Google Calendar Leave Data

**Purpose**: Load employee leave/RD/weekend work events from the shared Google Calendar ("Leave/RD/Weekend Work Calendar") into Supabase for reporting.

**Migration 030** (`migrations/030_calendar_leave_tables.sql`):
- `data_raw.raw_calendar_leave` — raw JSONB event storage (event_id, data, run_id)
- `data_staging.stg_calendar_leave` — parsed fields (leave_type, team, person, person_note, start_date, end_date, days, is_all_day, creator_email, etc.)

**Migration 031** (`migrations/031_calendar_leave_normalized_columns.sql`):
- Added `team_normalized` and `leave_type_normalized` columns to staging
- AI normalization step uses Claude Haiku to map raw values to canonical forms each run

**Extractor** (`extract_calendar_leave.py`):
- Standalone pipeline (like gmail), not part of main.py
- Uses `calendar_client.authenticate_calendar()` for Google Calendar API auth
- **Incremental mode (default)**: Checks `MAX(event_updated)` from staging, uses `updatedMin` API param to only fetch new/changed events, upserts into staging via `ON CONFLICT (event_id) DO UPDATE`
- **Full refresh mode** (`--full-refresh`): Re-fetches all events, truncates+reloads staging
- **Email notifications**: Uses `pipeline_notifier.py` (same as main pipeline), sends to all 3 recipients with HTML summary + log attachment. Suppress with `--no-email`
- **Summary parser**: Handles `"LeaveType - Group - Person (note)"` format with edge cases:
  - Missing spaces around dashes (`"RD- Alpha"`, `"Admin and Ops -Merj"`)
  - Holiday entries (`"PH: Christmas Day"`)
  - Reversed 2-part entries (`"Steph - Weekend Work"`)
  - Bare-dash entries (`"SDL-CG1-Tads"`)
  - Parenthetical notes (`"Corey (3pm onwards)"` -> person=Corey, person_note=3pm onwards)
- **AI normalization**: Collects distinct team + leave_type values, sends to Claude Haiku, applies mapping. Handles duplicates (ACCTG/Acctg/Accounting -> Acctg), typos (GC2 -> CG2), compound leave types (UT SL -> UT/SL), and filters non-team names from team field

**Data loaded**:
- 10,488 events total (Jan 2024 onward)
- 0 parse errors
- 10,182 (97%) have person parsed, 10,156 (97%) have team parsed
- 306 without person = holidays (PH entries, by design)
- Top leave types: RD (5,348), VL (1,805), SL (1,118), WW (702), SDL (460)
- Top teams: CG1 (2,714), QPI (957), Admin and Ops (810), CRTV (768), Alpha (726)
- 33 canonical teams after AI normalization (from 53 raw), 26 canonical leave types (from 44 raw)

**Performance**:
- Full refresh: ~45 sec (API ~12s, raw load ~15s, AI normalization ~6s, staging load ~8s)
- Incremental (no changes): ~2 sec

**Task Scheduler**: `SwiftPipeline-Calendar` — daily at 12:30 AM
- Batch wrapper: `scheduled_calendar_pipeline.bat`
- Logs: `pipeline_logs/calendar_YYYYMMDD_HHMM.log`
- ~~ACTION NEEDED~~: Resolved — set to "Run whether user is logged on or not" via Task Scheduler GUI.

**Files created**:
| File | Description |
|---|---|
| `migrations/030_calendar_leave_tables.sql` | Raw + staging tables |
| `migrations/031_calendar_leave_normalized_columns.sql` | AI-normalized columns |
| `extract_calendar_leave.py` | Extractor/transformer with AI normalization + email |
| `scheduled_calendar_pipeline.bat` | Task Scheduler batch wrapper |
| `register_calendar_task.ps1` | PowerShell registration script (needs elevation) |
| `register_calendar_task.xml` | XML task definition (needs elevation) |

**Files modified**:
| File | Change |
|---|---|
| `pipeline_notifier.py` | Added "Calendar Leave" to `PIPELINE_TABLES` |

### Analytics + Metadata for Calendar Leave

**Migration 032** (`migrations/032_analytics_calendar_leave.sql`):
- Created `analytics.v_calendar_leave` view over `stg_calendar_leave`
- Uses `COALESCE(leave_type_normalized, leave_type)` to prefer normalized values, exposing raw as `leave_type_raw` / `team_raw`

**Schema metadata**: Inserted 17 rows into `agent.schema_metadata`:
- 1 table-level description for `stg_calendar_leave`
- 16 column-level descriptions for `v_calendar_leave` (all columns)

**Claude project prompt** (`docs/claude_project_prompt.md`):
- Added `analytics.v_calendar_leave` (~10.5K rows) documentation with full column descriptions
- Added 3 example queries: leave by type, team absence overview, who is on leave today

**Schema cache**: No code changes needed — `schema_cache.py` auto-discovers new views/tables from `information_schema` + `agent.schema_metadata` on TTL refresh.

### Fix: Calendar Leave Parser Misparses (swapped fields + holidays)

**Problem**: 11 entries had structural misparses where team/person or leave_type/team were in the wrong positions, plus 1 holiday parsing failure:
- **Swapped team/person** (7): e.g. "SDL - Euge - CG1" → team=Euge, person=CG1 (should be reversed)
- **Swapped leave/team** (2): e.g. "QPI - SL - Paolo" → leave=QPI, team=SL (should be reversed)
- **Holiday miss** (1): "Christmas Holiday (Company-Wide)" → hyphen in "Company-Wide" broke dash splitting

**Fix** (`extract_calendar_leave.py`):
- Added `_KNOWN_TEAMS` / `_KNOWN_TEAMS_UPPER` sets with 30+ canonical team names
- After 3-part split, detects swapped leave↔team (if parts[0] is a known team and parts[1] is a leave code, swap them)
- After 3-part split, detects swapped team↔person (if parts[2] is a known team and parts[1] is not, swap them)
- Added broader holiday detection: any summary containing "holiday" → PH

**Result**: Reduced from 41 teams (11 garbage) to 30 clean teams. Re-ran with `--full-refresh`.

**TODO — Rare teams to review later**:
| Team | Events | Person(s) | Notes |
|---|---|---|---|
| SD | 4 | Emman | Rest days in Feb-Mar 2024, possibly short-lived team label |
| T&D | 2 | Francis, Jehane | Mid-2024, could be "Training & Development" or variant of T&A |
| PHIDSM | 2 | John | Aug-Sep 2024, likely same as "PHI DS" just written differently |
| Gamma/Beta | 1 | Mikaela | Aug 2024, person split between both teams |
| AI | 1 | Jez | May 2025, possibly newer team |
| AIE | 1 | Quino | Jan 2026, possibly newer team |

These are all real entries (not misparses), just rare. Decide whether to map them to existing teams (e.g. PHIDSM → PHI DS) or keep as-is.

---

## Session: 2026-02-21

### Migration 029: Complete Metadata Coverage

**Purpose**: Fill all remaining gaps in `agent.schema_metadata` to reach 100% column-level coverage.

**Before**: 233 rows, significant gaps in stg_qa_form (36%), v_qa_forms (14%), v_timer_activities (0%), v_asset_tasks (37%), v_user_priorities (0% col-level)

**After**: 375 rows, 0 gaps across all 15 tables

**Changes**:
- **Section A** (stg_qa_form, 53 new): Pipeline internals (id/run_id/loaded_at), 29 issue detail fields (*_issues, *_other_issues patterns), 4 PMI sub-fields, 17 migration-006 QA fields (rcm_approval, sector_photos, conditional_pass, supports, etc.)
- **Section B** (analytics views, 63 new): v_asset_tasks (19 cols updated/inserted), v_timer_activities (20 cols), v_qa_forms (18 cols), v_user_priorities (25 cols)
- **Section C** (pipeline internals, 26 new): id/run_id/loaded_at across stg_asset_tasks(8), stg_assets(3), stg_organizations(5), stg_projects(4), stg_timer_activities(3), stg_user_priorities(3)

**Files created**:
- `local-pipeline/swift_api_pipeline/migrations/029_complete_metadata_coverage.sql` — ~150 INSERT statements with ON CONFLICT DO UPDATE
- `local-pipeline/swift_api_pipeline/migrations/apply_029.py` — runner with coverage gap audit verification

**Verification**: All 15 data_staging + analytics tables show gap=0. Schema docs re-exported.

### Fix: Timer Pipeline Duplication (stg_timer_activities)

**Problem**: The timer pipeline's nightly run extracts the entire month-to-date from the Swift API, but the transform step only deleted staging rows matching the current `run_id`. Previous runs' rows persisted, causing the same timer entries to stack across runs.

**Evidence**: Feb 2026 had 16 runs / 40,591 rows (should be ~4,500 unique). All other months had 1 run each (no duplication — pre-automation manual runs).

**Root cause**: `transform_timer_activities()` used `DELETE FROM stg_timer_activities WHERE run_id = $1`, which only removed the current run's rows. Old runs' rows with different run_ids persisted.

**Fix**: Changed DELETE scope from `WHERE run_id = $1` to `WHERE start_date = $1`. Since `start_date` is always the 1st of the extraction month, every nightly run within a month shares the same `start_date`. The latest run's data is always a superset (MTD grows each day).

**Cleanup**: Deleted 15 stale Feb 2026 runs, keeping only the latest (end_date 2026-02-20, 4,508 rows).

**Post-cleanup**: Re-ran `data_staging.backfill_asset_did()` and refreshed all 3 analytics MVs (mv_project_summary, mv_technician_stats, mv_daily_completion).

**Files changed**: `swift_api_pipeline/transform.py` (~line 844)

**Result**:
| Metric | Before | After |
|---|---|---|
| Feb 2026 runs | 16 | 1 |
| Feb 2026 rows | 40,591 | 4,508 |
| Total stg_timer_activities | ~283K | 267,709 |

### Fix: Gmail Pipeline Duplication (stg_ar_aging + stg_sales_detail)

**Problem**: Same duplication bug in the aging and sales transforms. The bulk migration on Feb 11 loaded all historical staging data, then nightly Gmail pipeline runs re-processed emails and stacked duplicate rows. Most email dates showed 2 runs; 3 dates (Feb 3, Dec 2, Nov 3) had 3 runs due to duplicate emails sent minutes apart.

**Evidence**:
- `stg_ar_aging`: 327,969 rows / 198 runs (should be ~171K / 104 email dates)
- `stg_sales_detail`: 9,683 rows / 204 runs (should be ~5K / 104 email dates)

**Fix**: Changed both transforms to DELETE by `email_received_date` instead of `run_id`:
- `transform_ar_aging()`: `DELETE WHERE email_received_date IN (SELECT DISTINCT email_received_date FROM raw_ar_aging WHERE run_id = $1)`
- `transform_sales_detail()`: Same pattern with `raw_sales_detail`

This ensures that if the same email is re-processed in a different pipeline run, the old staging rows are replaced rather than stacked.

**Cleanup**: Two-phase cleanup:
1. For each `email_received_date`, kept only the latest `run_id` (by `loaded_at`) — removed the bulk of duplicates
2. For 3 dates with duplicate emails received minutes apart (identical data), kept only the latest timestamp per calendar date

**Files changed**: `swift_api_pipeline/transform.py` (~lines 938-944, 1030-1036)

**Result**:
| Table | Before | After | Removed |
|---|---|---|---|
| stg_ar_aging | 327,969 | 171,390 | 156,579 (48%) |
| stg_sales_detail | 9,683 | 4,942 | 4,741 (49%) |

### Database: Added Missing Indexes

**Problem**: The new aging/sales DELETE queries filter by `email_received_date`, but no index existed on that column.

**Fix**: Created two indexes:
- `idx_stg_ar_aging_email_received_date` on `data_staging.stg_ar_aging (email_received_date)`
- `idx_stg_sales_detail_email_received_date` on `data_staging.stg_sales_detail (email_received_date)`

Timer already had coverage via `idx_stg_timer_activities_dates` on `(start_date, end_date)`. Raw tables already had `run_id` indexes for the subqueries.

### Pipeline Validation

Traced the full pipeline call chain to confirm no issues with next nightly run:

- **Timer**: DELETE removes ~4,508 Feb rows → INSERT fresh MTD → backfill fills asset_did → validation compares raw_count vs transformed_count (both run_id scoped, will match)
- **Aging/Sales**: Gmail scheduler checks `MAX(email_received_date)` in raw → only runs if new email exists → extraction creates run_id per email → DELETE subquery scopes to that email's timestamp → INSERT fresh data
- **validate_transform_counts()**: Confirmed it compares `raw_count` (run_id scoped) vs `transformed_count` (rows inserted) — not affected by broader DELETE scope. `stg_count` is informational only.
- **Edge cases**: No raw data → timer returns early before DELETE; aging/sales subquery returns empty → deletes nothing. All safe.

### Discussion: MCP (Model Context Protocol)

Discussed the concept of MCP — Anthropic's open standard for connecting AI models to external tools and data sources.

**Key points covered**:
- **What it is**: A universal protocol (like USB for AI) that standardizes how AI apps integrate with external services. Uses JSON-RPC 2.0 over STDIO (local) or Streamable HTTP (remote).
- **Architecture**: Host (AI app) → Client (one per server) → Server (exposes tools). The Supabase MCP server used throughout this session is a live example — `execute_sql`, `apply_migration`, `list_tables` etc. are all MCP tools.
- **Problem solved**: Before MCP, every AI integration required custom code. With MCP, build one server and it works with all MCP-compatible hosts (Claude, VS Code, custom agents).
- **Adoption**: Donated to Linux Foundation (Dec 2025), adopted by OpenAI and Google DeepMind. 75+ official connectors.
- **Relevance to our project**: The Supabase MCP server is how Claude Code directly queries and modifies our database without any custom integration code. Potentially relevant for the local-ai-agent backend if we want to expose our pipeline/database as MCP tools.

---

## Session: 2026-02-20

### Migration 028: Schema Metadata Enrichment

**Purpose**: Close gaps in `agent.schema_metadata` to improve AI agent query accuracy.

**Changes (233 total rows, up from ~200)**:
- **Section A** (stg_ar_aging): Added table data_notes (append mode, QuickBooks source), 3 new column entries (id, run_id, loaded_at), enriched 6 columns with example_values/data_notes. **Bug fix**: `past_due` corrected from "dollars" to "days past due"
- **Section B** (stg_sales_detail): Same pattern — table data_notes, 3 new columns, 6 enrichments
- **Section C** (lookup tables): Full metadata for `qa_form_asset_did_lookup` (5 rows) and `carrier_group_lookup` (5 rows) — both had zero metadata before
- **Section D** (fixes & enrichments): Fixed `stg_qa_form.requirement_status` (was still Pass/Fail in staging, only fixed in analytics by migration 022), added missing `carrier_group` columns for `stg_assets` and `analytics.v_asset_tasks`, fixed timer row count (~11.6K → ~273K), added example_values to status columns, added `related_tables` FK references to 11 columns

**Files created**:
- `local-pipeline/swift_api_pipeline/migrations/028_enrich_schema_metadata.sql` — 50 SQL statements
- `local-pipeline/swift_api_pipeline/migrations/apply_028.py` — asyncpg runner with verification queries
- `local-pipeline/swift_api_pipeline/migrations/export_schema_metadata.py` — reusable export script

**Files updated**:
- `local-pipeline/swift_api_pipeline/docs/schema_metadata.json` — full DB export (233 rows)
- `local-pipeline/swift_api_pipeline/docs/schema_metadata.md` — human-readable markdown

**Remaining**: Restart backend so `SchemaCache.refresh()` picks up new metadata

---

## Session: 2026-02-19

### Pipeline: Add Email Recipients

**Change**: Added hajie@ontel.co and sheena@ontel.co as pipeline email recipients alongside jamil.mendez@ontel.co.
- Changed `NOTIFICATION_RECIPIENT` (single string) to `NOTIFICATION_RECIPIENTS` (list)
- Updated `send_pipeline_email` signature from `recipient: str` to `recipients: List[str]`
- All pipeline step emails now go to all three recipients

**Files changed**: `swift_api_pipeline/pipeline_notifier.py`

### Pipeline: Fix QA Forms Loader Race Condition (8,217 missing rows)

**Problem**: `stg_qa_form` was missing ~8,217 rows compared to raw tables. TS17 had 60,000 staging vs 63,942 raw; TS18 had 50,000 vs 54,275. The staging counts were exact multiples of `LOAD_BATCH_SIZE = 10,000` — the last partial batch per table was lost.

**Root cause**: In `extract_forms.py`, the loader worker calls `result_queue.task_done()` after accumulating data in memory (line 167), NOT after writing to the database. `result_queue.join()` returns thinking all work is done, but partial batches remain in `pending_batches`. The loader flushes these after the while loop breaks, but `loader_thread.join(timeout=120)` could return before the flush completes. The transform then runs against incomplete raw data.

**Fix**:
- Removed `timeout=120` from `loader_thread.join()` — main thread now waits for flush to fully complete
- Added exception handling and row count logging around the flush step
- Net effect: transform always sees complete raw data

**Files changed**: `swift_api_pipeline/extract_forms.py`

**Verification**: Next nightly run should show TS17 and TS18 staging counts matching raw counts (not capped at round 10K multiples).

### Pipeline: Reduce COPY Batch Size for Asset Tasks

**Problem**: During nightly pipeline (~12:30 AM), asset tasks COPY operations hit 300s+ timeouts with retry escalation up to attempt 3/5. Root cause: `LOAD_BATCH_SIZE = 100000` in `extract_asset_tasks.py` — each of the 6 concurrent workers attempts to COPY up to 100K JSONB rows in a single operation, overwhelming Supabase. Other extractors use much smaller batches (Forms: 10K, Timer: 1K) without issues.

**Fix**: Reduced `LOAD_BATCH_SIZE` from `100000` to `25000` in `extract_asset_tasks.py:26`.
- With 6 workers, worst case is 6 × 25K = 150K rows in-flight (was 6 × 100K = 600K)
- Each batch should complete in ~30-60s instead of 300s+, well within the 600s timeout
- More batches per run (~90 vs ~23 for 2.2M rows) but each completes faster and more reliably
- Net effect: eliminates COPY timeout retries (~5-10 min savings), pipeline should be faster overall

**Files changed**: `swift_api_pipeline/extract_asset_tasks.py`

**Verification**: Monitor next nightly pipeline run for absence of COPY timeout retries.

### Pipeline: Disable Email Notifications for Post-Phase 2 Steps

**Change**: Hardcoded `send_email=False` for Asset DID Backfill and Analytics MV Refresh in `main.py`.
- These are quick housekeeping steps that don't need individual email alerts
- Steps still run normally and results still appear in `pipeline_results` and the final summary log
- Only the per-step email notification is suppressed

**Files changed**: `swift_api_pipeline/main.py` (lines 295, 299)

### Analytics: Add carrier_group to v_asset_tasks

**Change**: Added `carrier_group` column to `analytics.v_asset_tasks` view and seeded `agent.schema_metadata`.
- New column appended to end of view (CREATE OR REPLACE can't reorder columns)
- Values: Verizon (1.7M), TMO/USCC (304K), AT&T/DISH (198K)
- Schema metadata includes business context synonyms (carrier, client, vendor) and example values
- Agent schema cache will pick it up on next refresh

**Files changed**: `swift_api_pipeline/migrations/027_v_asset_tasks_carrier_group.sql` (new)

**Migration applied**: Yes, live on cloud Supabase

### Pipeline: Carrier Group Backfill in Post-Phase 2

**Change**: Added carrier_group backfill step to `backfill_asset_did()` in `transform.py`.
- After the existing asset_did backfill, matches `stg_assets` to `carrier_group_lookup` table via `ILIKE` on `asset_id` containing the lookup `search_term`
- Uses `DISTINCT ON (asset_did) ... ORDER BY match_order` for deterministic selection when multiple terms match
- Only updates assets where `carrier_group IS NULL` (write-once pattern)
- Logs count of updated assets

**Files changed**: `swift_api_pipeline/transform.py`

---

## Session: 2026-02-18

### Pipeline: Log Cross-Contamination Fix + Assets RPC Timeout + Raw Tables in Email

**Problem 1: Log Cross-Contamination in Parallel Pipeline Emails**
- During Phase 2, 4 pipelines run in parallel, each with `capture_logs()` on the same `pipeline` logger
- All 4 `LogCaptureHandler` instances captured ALL logs from ALL threads
- Result: Timer email shows asset_tasks/forms logs, etc.

**Fix**: Thread-based + logger-name-prefix filtering in `LogCaptureHandler`:
- Each handler records the calling thread's ID (`owner_thread`)
- Accepts `logger_prefixes` (e.g., `["pipeline.asset_tasks"]` for asset_tasks)
- `emit()` only accepts records from owner thread (shared loggers) OR matching prefix (child workers)
- Sequential pipelines (Phase 1, Post-Phase 2) pass no prefixes → capture everything (no change)
- Files: `pipeline_notifier.py`, `main.py`

**Problem 2: Assets RPC Timeout (connection closed)**
- Feb 18 nightly: Asset Tasks extraction SUCCESS (2,243,924 rows), but transform FAILED
- `aggregate_assets_from_raw` RPC took >5 min, connection killed by Supavisor
- `db.fetch()` hardcoded `statement_timeout=300s`, no override option
- Function-level `SET statement_timeout = '300s'` (migration 022) also limited it

**Fix**:
- Added `statement_timeout` parameter to `db.fetch()`, `db.fetchrow()`, `db.fetchval()` (same pattern as `db.execute()`)
- Pass `statement_timeout=600` for assets RPC in `transform.py`
- Migration 024: `ALTER FUNCTION aggregate_assets_from_raw ... SET statement_timeout = '600s'`
- Migration 024: Recreated `backfill_asset_did` with `SET statement_timeout = '600s'` (was 300s)
- Manually ran assets transform + asset_tasks transform + backfill + MV refresh to recover data

**Problem 3: Per-Pipeline Row Counts in Emails**
- Email showed ALL staging table row counts — irrelevant tables cluttered each pipeline's email
- No raw tables shown at all — couldn't see extraction results

**Fix**: Each pipeline email now shows only its own relevant tables (raw + staging):
- `PIPELINE_TABLES` dict in `pipeline_notifier.py` maps pipeline name → `[(schema, table), ...]`
- `snapshot_row_counts(tables)` accepts a table list (only counts what's needed)
- `_build_row_counts_html` filters display by pipeline's table list with Raw/Staging section headers
- E.g., Asset Tasks email shows: `raw_asset_tasks`, `stg_assets`, `stg_asset_tasks`
- Removed final consolidated summary email (redundant with individual emails)

**Nightly Run Results (Feb 18)**:
- All extractions: SUCCESS (zero COPY failures with 600s timeout)
- Asset Tasks transform: FAILED — `connection was closed in the middle of operation` (assets RPC exceeded 300s)
- Manually recovered: ran assets transform + asset_tasks transform + backfill + MV refresh
- All data fully restored: 29,199 assets, 2,243,924 asset_tasks, backfill + MV refresh OK

**Files Changed**:
- `pipeline_notifier.py` — LogCaptureHandler filtering, per-pipeline table mapping, snapshot_row_counts accepts table list
- `main.py` — logger_prefixes + row_count_tables passthrough, removed final summary email
- `db.py` — statement_timeout on fetch/fetchrow/fetchval
- `transform.py` — 600s timeout for assets RPC
- `migrations/024_increase_rpc_timeouts.sql` — function-level timeout increases (300s → 600s)
- `migrations/apply_024.py` — migration runner

### COP Report Automation

**Task**: Automate the "Final COP - Pending Task Report" Excel workbook that was previously created manually from Power BI exports.

**Created**:
- `scripts-reference/export_cop_report.py` — Python script generating 8-sheet Excel workbook:
  - **Summary**: Carrier group pivot table + pending LL/PMI COP lists
  - **48Hrs Summary**: 3 sections — 48Hr completed with ongoing FCOP, pending FCOP, 48Hr approved since 2025
  - **Raw**: Computed join of FCOP approved with LL/PMI lookups (carrier group, days since FCOP)
  - **FCOP approved / LL / PMI / 48Hrs / FCOP ongoing**: Full 28-column data sheets
- `scripts-reference/scheduled_cop_report.bat` — Task Scheduler wrapper (weekly Monday 1 AM)

**Key implementation details**:
- Uses asyncpg connection pool (5 parallel queries, ~17s total fetch)
- Joins computed in Python (not SQL) to avoid 300s query timeout on LEFT JOINs across 2.2M-row table
- Join key is `asset_did` (not `project_did` which is only 6 values for TS13-18)
- Each asset can have 3 FCOP tasks (6., 7., 8. Final COP Complete); FCOP approved is already 1:1 by asset_did
- LL/PMI/48Hrs have duplicate asset_dids (29,199 rows, 27,682 unique); lookups dedup first-row-wins
- Carrier group derived from asset_id text (VZW/Verizon/Westell→Verizon, DISH/AT&T→AT&T/DISH, T-Mobile/Viking/USCC/Gulf/FTTH→TMO/USCC)
- Google Drive upload to "COP Reports" folder + email notification with Drive link
- CLI: `--no-upload`, `--no-email`, `--output`

**Row counts (current data vs original manual report)**:
- FCOP approved: 21,130 (was 8,123 — more assets approved since original)
- LL/PMI/48Hrs: 29,199 each (was 28,813 — 386 new assets)
- FCOP ongoing: 7,906 (plan expected ~28,800 — may need filter adjustment)
- Raw: 21,130 with carrier split: Verizon 15,388, AT&T/DISH 2,323, TMO/USCC 3,189

**Scheduling**: Not yet registered in Task Scheduler. Run manually with:
```
python scripts-reference/export_cop_report.py --no-upload
```

---

## Session: 2026-02-17

### Pipeline: COPY Timeout Fix + Partial Failure Detection

**Problem**: Feb 17 nightly pipeline took 1h 44m (vs 50m normal) with 16+ COPY retry failures showing empty error messages. 3 projects (TS14, TS16, TS18) lost ~425K rows due to cascading timeouts, but pipeline still reported SUCCESS.

**Root Cause**: `asyncio.TimeoutError` from asyncpg's `command_timeout=300`. When `timeout=None` is passed to `copy_records_to_table`, asyncpg resolves it to the pool's 300s default — NOT "no timeout". Large JSONB COPY batches (100K records, ~200-500MB) under concurrent load exceeded 300s. The empty error message is because asyncpg constructs `asyncio.TimeoutError()` with no arguments internally.

**Changes**:
1. **`db.py` — COPY timeout increased to 600s**: `copy_records()` now defaults `timeout=600` and sets server-side `statement_timeout` to match, giving COPY operations double the headroom
2. **`db.py` — Improved retry logging**: `retry_db()` now logs `type(e).__name__` so we see `TimeoutError:` instead of empty string
3. **`extract_asset_tasks.py` — Partial failure detection**: Tracks which projects throw exceptions. If any fail, marks pipeline_runs as "failed" with error details and raises `RuntimeError` that propagates to `main.py`, which marks Asset Tasks as FAILED in the summary and email notification

### Pipeline: Individual Email Notifications per Pipeline

**Change**: Full pipeline run now sends individual email notifications per pipeline step instead of one consolidated email at the end.

**Files changed**:
1. **`main.py`** — Added `_run_and_notify()` helper that wraps each pipeline with `run_pipeline_with_notification` (log capture + row count snapshots + email). `run_all_pipelines` now uses this for all 7 steps: Phase 1 (Orgs/Projects), Phase 2 (Asset Tasks, User Priorities, QA Forms, Timer in parallel), Post-Phase 2 (Backfill, MV Refresh). No consolidated summary email — each pipeline sends its own.

### Pipeline: Gmail New-Email Detection + 30-min Scheduling

**Change**: Gmail pipelines (aging + sales) now check for actual new emails instead of just "does today's date have data". Scheduler updated from hourly to every 30 minutes.

**Files changed**:
1. **`run_gmail_pipelines.py`** — Replaced `has_todays_data()` (date-based check) with `has_new_emails()` that authenticates to Gmail once, checks 3 most recent "Daily Revenue Report" emails against max `email_received_date` in each raw table. Only runs pipeline if newer emails exist.
2. **`scheduled_gmail_pipeline.bat`** — Updated comments
3. **Windows Task Scheduler** — `SwiftPipeline-Gmail-Hourly` repetition changed from PT1H to PT30M (still 1-10 AM window)

---

## Project Overview

| Project | Path | Purpose |
|---------|------|---------|
| **Data Pipeline** | `local-pipeline/swift_api_pipeline/` | ETL pipeline: Swift Projects API → Supabase (raw → staging) |
| **AI Agent** | `local-ai-agent/` | Web app: natural language queries → SQL → reports with charts |

### Data Flow
```
Swift Projects API → local-pipeline (Extract & Transform) → Supabase → local-ai-agent (Query & Analyze) → User
```

---

## Session 1 — 2026-02-06: Initial Review & Critical Bug Fixes

### What Was Done
Full code review of both projects (pipeline + AI agent backend + frontend), identifying 50+ issues across security, performance, data integrity, and code quality.

### Critical Bugs Fixed

#### Data Pipeline

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `extract_asset_tasks.py` | `run_asset_task_pipeline()` never returned `run_id` — transforms used stale data | Added `return str(extractor.run_id)` after success path |
| 2 | `extract_asset_tasks.py` | `loader_worker` missing `task_done()` in except block — pipeline hangs forever on DB error | Added `result_queue.task_done()` in except block |
| 3 | `extract_forms.py` | Same loader deadlock bug | Added `result_queue.task_done()` in except block |
| 4 | `extract_timer.py` | Same loader deadlock bug | Added `result_queue.task_done()` in except block |
| 5 | `extract_requirements.py` | Same loader deadlock bug | Added `result_queue.task_done()` in except block |
| 6 | `extract.py` | Non-200 API responses silently skipped (data loss) | Added retry logic for non-200 status codes |
| 7 | `extract.py` | No request timeouts (infinite hang on stalled server) | Added `timeout=60` to all 3 `requests.get()` calls |
| 8 | `extract_asset_tasks.py` | `if records:` treats 0 as falsy — 0-record runs not logged | Changed to `if records is not None:` |
| 9 | `extract_forms.py` | Same `if records:` bug | Changed to `if records is not None:` |
| 10 | `extract_timer.py` | Same `if records:` bug | Changed to `if records is not None:` |
| 11 | `extract_requirements.py` | Same `if records:` bug | Changed to `if records is not None:` |
| 12 | `main.py` | `success` variable unbound when `--pipeline` arg doesn't match any branch | Added `else` clause with `success = False` |

#### AI Agent Backend

| # | File | Bug | Fix |
|---|------|-----|-----|
| 13 | `sql_guard.py` | SQL injection bypass: `UNION SELECT` (without ALL) not caught | Changed pattern to `UNION\s+(?:ALL\s+)?SELECT` |
| 14 | `sql_guard.py` | `pg_shadow`/`pg_authid` accessible (password hash exposure) | Added `BLOCKED_TABLES` set + schema validation checks |
| 15 | `sql_guard.py` | `information_schema` only partially blocked | Changed pattern to block all `pg_` and `information_schema` access |
| 16 | `pdf_export.py` | XSS via Jinja2 template (no autoescaping) | Enabled `autoescape=True` on Environment |
| 17 | `pdf_export.py` | Chart base64 images would be escaped by autoescape | Marked internally-generated base64 data with `Markup()` |
| 18 | `export.py` | PDF error handler calls same failing `get_pdf_bytes()` again | Removed try/except, detect HTML fallback via content sniffing |
| 19 | `models.py` | `datetime.utcnow()` deprecated (Python 3.12+), naive datetime | Changed to `datetime.now(timezone.utc)` |

#### AI Agent Frontend

| # | File | Bug | Fix |
|---|------|-----|-----|
| 20 | `next.config.js` | Missing `output: 'standalone'` — Docker build fails at COPY step | Added `output: 'standalone'` |

### Known Issues Still Open (Prioritized)

#### High Priority
- [ ] Session tokens stored in `localStorage` (XSS-vulnerable) — should use `httpOnly` cookies
- [ ] No React error boundary — any render crash = white screen
- [ ] Singleton race conditions in backend (`get_db_pool`, `get_schema_cache`, `get_report_store`, `get_rate_limiter`)
- [ ] Schema cache refresh clears `_tables` dict mid-request (concurrent queries see empty schema)
- [ ] Report access has no ownership check (any user can access any report by UUID)
- [ ] Rate limit bypass via `X-Forwarded-For` header spoofing
- [ ] Rate limit user ID uses `hash(token) % 10000` — only 10k buckets, collisions likely
- [ ] No timeout on Claude API calls (hangs indefinitely)
- [ ] Fake streaming endpoint (sends all progress events after work is done)
- [ ] `lib/api.ts` and `lib/auth.ts` are never used — pages use raw `fetch()`
- [ ] Type definitions duplicated 3x (`api.ts`, `ReportCard.tsx`, `chat/page.tsx`)

#### Medium Priority
- [ ] Pipeline: Non-atomic clear-then-load (data unavailable if extraction fails midway)
- [ ] Pipeline: No token refresh in `extract.py` (JWT can expire mid-extraction)
- [ ] Pipeline: Timezone-naive timestamps in `extract_timer.py`
- [ ] Pipeline: All files use `print()` instead of `logging`
- [ ] Pipeline: Massive code duplication (auth, loader, pipeline tracking across 5 files)
- [ ] Backend: Full schema sent to Claude every request (wastes tokens)
- [ ] Backend: Health check returns HTTP 200 when degraded (should be 503)
- [ ] Backend: CORS middleware ordering — 429 responses lack CORS headers
- [ ] Backend: New Anthropic client created per request (no connection reuse)
- [ ] Backend: Unbounded memory growth in ReportStore, MetricsCollector, rate limiter
- [ ] Frontend: Follow-up questions don't auto-submit (only populate input)
- [ ] Frontend: No request cancellation (no AbortController)
- [ ] Frontend: Unused dependencies (`zustand`, `react-markdown`)
- [ ] Frontend: Missing ARIA labels on icon-only buttons
- [ ] Frontend: Auto-scroll overrides user scroll position

#### Low Priority
- [ ] Pipeline: `datetime.utcnow()` deprecated in `load.py`
- [ ] Pipeline: New Supabase client on every `get_supabase_client()` call
- [ ] Pipeline: No pagination for organizations/projects (single page fetch)
- [ ] Backend: Duplicate JSON parsing logic in planner.py/analyzer.py
- [ ] Backend: Excel column letter bug for columns beyond index 51
- [ ] Frontend: Naive SQL formatting breaks on keywords in string literals
- [ ] Frontend: User email never cleared from localStorage on logout

---

## Session 2 — 2026-02-06: High-Priority Fixes

### What Was Done
Systematic fixes for all high-priority items identified in Session 1: backend concurrency/security, Claude API resilience, and frontend architecture overhaul.

### Backend Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `pool.py` | `get_db_pool()` singleton race condition — concurrent startup creates multiple pools | Added `asyncio.Lock` with double-check locking pattern |
| 2 | `schema_cache.py` | `get_schema_cache()` singleton race condition | Added `asyncio.Lock` with double-check locking |
| 3 | `schema_cache.py` | `refresh()` clears `_tables = {}` then rebuilds — concurrent queries see empty schema | Atomic swap: build `new_tables` dict, then assign `self._tables = new_tables` |
| 4 | `generator.py` | `get_report_store()` singleton race condition | Added `threading.Lock` with double-check locking |
| 5 | `rate_limit.py` | `hash(token) % 10000` — only 10k buckets, high collision rate | Replaced with `hashlib.sha256().hexdigest()[:16]` |
| 6 | `rate_limit.py` | Trusts `X-Forwarded-For` header — trivially spoofable to bypass rate limits | Removed header trust, use `request.client.host` only |
| 7 | `health.py` | Health/readiness return HTTP 200 even when degraded | Return `JSONResponse` with `status_code=503` when unhealthy |
| 8 | `planner.py` | No timeout on Claude API calls — hangs indefinitely on network issues | Added `timeout=60.0` to `AsyncAnthropic` client |
| 9 | `planner.py` | Empty/unexpected Claude response causes unhandled AttributeError | Added safety check: `if not response.content or not hasattr(response.content[0], 'text')` |
| 10 | `planner.py` | `_recover_from_error` JSON parse failure unhandled | Added try/except around recovery JSON parsing |
| 11 | `analyzer.py` | No timeout on Claude API calls | Added `timeout=60.0` to `AsyncAnthropic` client |
| 12 | `analyzer.py` | Empty Claude response crashes analysis | Added safety check, returns basic `AnalysisResult` on failure |

### Frontend Fixes

| # | File | Change | Details |
|---|------|--------|---------|
| 13 | `lib/types.ts` | **NEW** — Shared type definitions | Single source of truth for `Report`, `ReportSection`, `AuthResponse`, `Message` (discriminated union) |
| 14 | `components/ErrorBoundary.tsx` | **NEW** — React error boundary | Catches render crashes, shows user-friendly error UI with reload button |
| 15 | `app/layout.tsx` | Wrapped children in `<ErrorBoundary>` | No more white screen on render errors |
| 16 | `lib/auth.ts` | Rewritten as single auth module | `clearSession()` now removes `user_email`; `setSession()` accepts optional email; `getAuthHeaders()` centralized |
| 17 | `lib/api.ts` | Rewritten to use shared modules | Imports from `./auth` and `./types`; added `login()`, `getConfig()`, `AbortSignal` support; removed duplicate `getAuthHeaders()` |
| 18 | `app/page.tsx` | Rewired to use `api`/`auth` modules | Clears API key from state after validation; proper `APIError` handling |
| 19 | `app/chat/page.tsx` | Complete rewrite using shared modules | `AbortController` for cancellation; follow-up auto-submit via `form.requestSubmit()`; ARIA attributes (`role="log"`, `aria-live="polite"`, `aria-label`) |
| 20 | `components/ReportCard.tsx` | Removed duplicate type definitions | Now imports `Report` from `@/lib/types` instead of defining locally |

### Checklist Update — Items Resolved

- [x] Singleton race conditions (pool, schema cache, report store)
- [x] Schema cache empty during refresh
- [x] Rate limit hash collision vulnerability
- [x] Rate limit X-Forwarded-For bypass
- [x] Health check returns 200 when degraded
- [x] No timeout on Claude API calls
- [x] No React error boundary
- [x] `lib/api.ts` and `lib/auth.ts` never used
- [x] Type definitions duplicated 3x
- [x] Follow-up questions don't auto-submit
- [x] No request cancellation (AbortController)
- [x] Missing ARIA labels
- [x] User email never cleared on logout

### Known Issues Still Open

#### High Priority
- [ ] Session tokens stored in `localStorage` (XSS-vulnerable) — should use `httpOnly` cookies
- [ ] Report access has no ownership check (any user can access any report by UUID)
- [ ] Fake streaming endpoint (sends all progress events after work is done)

#### Medium Priority
- [ ] Pipeline: Non-atomic clear-then-load (data unavailable if extraction fails midway)
- [ ] Pipeline: No token refresh in `extract.py` (JWT can expire mid-extraction)
- [ ] Pipeline: Timezone-naive timestamps in `extract_timer.py`
- [ ] Pipeline: All files use `print()` instead of `logging`
- [ ] Pipeline: Massive code duplication (auth, loader, pipeline tracking across 5 files)
- [ ] Backend: Full schema sent to Claude every request (wastes tokens)
- [ ] Backend: CORS middleware ordering — 429 responses lack CORS headers
- [ ] Backend: New Anthropic client created per request (no connection reuse)
- [ ] Backend: Unbounded memory growth in ReportStore, MetricsCollector, rate limiter
- [ ] Frontend: Unused dependencies (`zustand`, `react-markdown`)
- [ ] Frontend: Auto-scroll overrides user scroll position

#### Low Priority
- [ ] Pipeline: New Supabase client on every `get_supabase_client()` call
- [ ] Pipeline: No pagination for organizations/projects (single page fetch)
- [ ] Backend: Duplicate JSON parsing logic in planner.py/analyzer.py
- [ ] Backend: Excel column letter bug for columns beyond index 51
- [ ] Frontend: Naive SQL formatting breaks on keywords in string literals

---

## Session 3 — 2026-02-06: Medium-Priority Fixes

### What Was Done
Systematic fixes for all medium-priority items: pipeline data integrity, observability, code deduplication; backend optimization; frontend cleanup.

### Pipeline Fixes

| # | File(s) | Issue | Fix |
|---|---------|-------|-----|
| 1 | `extract_asset_tasks.py`, `extract_forms.py` | Non-atomic clear-then-load — if extraction fails midway, tables are already empty (data loss) | Replaced `clear_tables()` (pre-extraction) with `clear_old_raw_data()` (post-extraction, deletes rows with different `run_id`) |
| 2 | `extract.py` | No token refresh — JWT can expire during long extractions | Added `_ensure_valid_token()` that checks JWT expiry and re-authenticates 5 min before expiry; added 401 retry handling to `extract_organizations()` and `extract_projects()` |
| 3 | `extract_timer.py` | Timezone-naive `datetime.now().date()` — can return wrong date near midnight | Changed to `datetime.now(zoneinfo.ZoneInfo(TIMEZONE)).date()`; made timestamp conversions timezone-aware with `.replace(tzinfo=tz)` |
| 4 | `config.py` | All pipeline files use `print()` — no log levels, timestamps, or filtering | Added `setup_logging()` and `get_logger()` functions |
| 5 | `extract.py`, `load.py`, `main.py`, `extract_asset_tasks.py`, `extract_forms.py`, `extract_timer.py`, `extract_requirements.py` | `print()` throughout | Replaced all `print()` with `logger.info()`/`logger.error()`/`logger.warning()` across all 7 files |
| 6 | `load.py` | `datetime.utcnow()` deprecated in Python 3.12+ | Changed to `datetime.now(timezone.utc)` |
| 7 | `base_extractor.py` | **NEW** — Auth, pipeline tracking, and counter logic duplicated across 4 extractor classes | Created `BaseExtractor` with shared `authenticate()`, `reauthenticate()`, `get_auth_headers()`, `start_pipeline_run()`, `complete_pipeline_run()`, `increment_loaded()` |
| 8 | `extract_asset_tasks.py`, `extract_forms.py`, `extract_timer.py`, `extract_requirements.py` | Each had its own copy of auth + pipeline methods (~40 lines each) | All now inherit from `BaseExtractor`; removed ~160 lines of duplicated code total |

### Backend Fixes

| # | File(s) | Issue | Fix |
|---|---------|-------|-----|
| 9 | `main.py` | CORS middleware added first (LIFO: runs last) — 429 rate limit responses lack CORS headers, browsers block them | Reordered: logging → rate limit → CORS (added last = runs first) |
| 10 | `planner.py` | Full schema sent to Claude every request — wastes tokens on large schemas | Added adaptive selection: uses `get_schema_context_compact()` when schema has >200 columns |
| 11 | `generator.py`, `planner.py`, `analyzer.py` | New `AsyncAnthropic` client created per Claude call — no connection reuse | Single client created per request in `generator.py`, passed to both planner and analyzer |
| 12 | `logging_config.py` | `MetricsCollector` dicts grow without bound | Added `MAX_ENDPOINTS=100` and `MAX_USERS=1000` limits with eviction |
| 13 | `rate_limit.py` | `RateLimiter._users` dict grows without bound | Added `MAX_USERS=10000`, `_evict_stale_users()` runs once/minute to remove idle users |

### Frontend Fixes

| # | File(s) | Issue | Fix |
|---|---------|-------|-----|
| 14 | `package.json` | `zustand` and `react-markdown` listed as dependencies but never imported | Removed both unused dependencies |
| 15 | `app/chat/page.tsx` | Auto-scroll always jumps to bottom — overrides user scroll position | Added scroll distance check: only auto-scrolls when user is within 200px of bottom |

### Checklist Update — Items Resolved

- [x] Pipeline: Non-atomic clear-then-load
- [x] Pipeline: No token refresh in `extract.py`
- [x] Pipeline: Timezone-naive timestamps in `extract_timer.py`
- [x] Pipeline: All files use `print()` instead of `logging`
- [x] Pipeline: Massive code duplication (auth, loader, pipeline tracking)
- [x] Pipeline: `datetime.utcnow()` deprecated in `load.py`
- [x] Backend: Full schema sent to Claude every request
- [x] Backend: CORS middleware ordering
- [x] Backend: New Anthropic client created per request
- [x] Backend: Unbounded memory growth in MetricsCollector, RateLimiter
- [x] Frontend: Unused dependencies (`zustand`, `react-markdown`)
- [x] Frontend: Auto-scroll overrides user scroll position

### Known Issues Still Open

#### High Priority
- [ ] Session tokens stored in `localStorage` (XSS-vulnerable) — should use `httpOnly` cookies
- [ ] Report access has no ownership check (any user can access any report by UUID)
- [ ] Fake streaming endpoint (sends all progress events after work is done)

#### Low Priority
- All resolved in Session 4

---

## Session 4 — 2026-02-06: Low-Priority Fixes

### What Was Done
Resolved all remaining low-priority items across pipeline, backend, and frontend.

### Pipeline Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `config.py` | `get_supabase_client()` creates a new client on every call | Added module-level singleton cache — client created once, reused thereafter |
| 2 | `extract.py` | `extract_organizations()` and `extract_projects()` fetch single page only — silently loses data if results exceed page size | Added pagination loops: fetches pages until empty or partial page received |

### Backend Fixes

| # | File(s) | Issue | Fix |
|---|---------|-------|-----|
| 3 | `agent/utils.py` | **NEW** — `_parse_json_response()` duplicated identically in planner.py and analyzer.py | Extracted to shared `parse_json_response()` in new `utils.py`; both classes now import it |
| 4 | `planner.py`, `analyzer.py` | Each had its own copy of JSON extraction (strip markdown fences, parse) | Replaced with `from .utils import parse_json_response` |
| 5 | `export/excel_export.py` | Column letter generation `chr(65+idx)` / `f"A{chr(65+idx-26)}"` breaks at column 52+ (beyond AZ) | Replaced with openpyxl's built-in `get_column_letter(idx + 1)` which handles any column count |

### Frontend Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 6 | `components/SQLPreview.tsx` | Naive regex SQL formatter: breaks on keywords inside string literals; compound keyword ordering issues (JOIN matched before LEFT JOIN) | Rewrote with string-literal protection (extract `'...'` to placeholders before formatting) and single-pass regex with compound keywords listed first for priority matching |

### Checklist Update — All Low-Priority Items Resolved

- [x] Pipeline: New Supabase client on every `get_supabase_client()` call
- [x] Pipeline: No pagination for organizations/projects (single page fetch)
- [x] Backend: Duplicate JSON parsing logic in planner.py/analyzer.py
- [x] Backend: Excel column letter bug for columns beyond index 51
- [x] Frontend: Naive SQL formatting breaks on keywords in string literals

### Known Issues Still Open

#### High Priority
- [ ] Session tokens stored in `localStorage` (XSS-vulnerable) — should use `httpOnly` cookies
- [ ] Report access has no ownership check (any user can access any report by UUID)
- [ ] Fake streaming endpoint (sends all progress events after work is done)

---

## Session 5 — 2026-02-06: High-Priority Architectural Fixes

### What Was Done
Resolved all remaining high-priority items: moved session tokens from localStorage to httpOnly cookies, added report ownership checks, and replaced fake streaming with real Server-Sent Events.

### Task #23 — httpOnly Cookie Authentication

Moved session tokens from XSS-vulnerable `localStorage` to server-set `httpOnly` cookies. Bearer token header still supported for programmatic API access.

| # | File | Change | Details |
|---|------|--------|---------|
| 1 | `backend/src/auth/models.py` | Session model updated | Added `user_id` field; fixed `is_expired()` to use `datetime.now(timezone.utc)` instead of deprecated `datetime.utcnow()` |
| 2 | `backend/src/auth/service.py` | Session creation updated | `create_session()` accepts `user_id` param (defaults to SHA256 hash of API key); `create_session_with_shared_key()` sets `user_id=email` |
| 3 | `backend/src/api/routes/auth.py` | Cookie management | `/validate` and `/login` set `httpOnly` cookie (`samesite=lax`, `path=/`); `/logout` clears cookie via `delete_cookie()` |
| 4 | `backend/src/api/dependencies.py` | Cookie fallback | `get_current_session()` reads from Bearer header first, falls back to `request.cookies.get("session_token")`; `get_api_key()` simplified to depend on `get_current_session()` |
| 5 | `frontend/lib/auth.ts` | Token removed from localStorage | `setSession()` only stores `expires_at` and `email`; removed `getSessionToken()`; `getAuthHeaders()` returns only `Content-Type` |
| 6 | `frontend/lib/api.ts` | Credentials included | Added `credentials: 'include'` to all 11 `fetch()` calls; removed manual Bearer header from export downloads |
| 7 | `frontend/app/page.tsx` | Login flow updated | `setSession()` calls no longer pass `token` — only `expires_at` and optional `email` |
| 8 | `frontend/app/chat/page.tsx` | Auth check simplified | Uses `isSessionValid()` (checks expiry) instead of `getSessionToken()`; removed redundant token check before API calls |

### Task #24 — Report Ownership

Reports now track the user who created them. Access is restricted to the owner.

| # | File | Change | Details |
|---|------|--------|---------|
| 9 | `backend/src/reports/models.py` | Report model updated | Added `owner_id: str = ""` field to `Report` dataclass |
| 10 | `backend/src/reports/generator.py` | Generator passes ownership | `generate()` accepts `owner_id` param, passes to `Report()` constructor |
| 11 | `backend/src/api/routes/query.py` | Ownership on create & read | `/ask` and `/ask/stream` pass `session.user_id` as `owner_id`; `/report/{id}` returns 403 if `owner_id` doesn't match |
| 12 | `backend/src/api/routes/export.py` | Ownership on exports | All 3 export endpoints (`/csv`, `/excel`, `/pdf`) check `report.owner_id` against `session.user_id`; return 403 if mismatched |

### Task #25 — Real Streaming Endpoint

Replaced fake progress events (all sent after report was generated) with real step-by-step SSE.

| # | File | Change | Details |
|---|------|--------|---------|
| 13 | `backend/src/reports/generator.py` | `generate_streaming()` method | Async generator yielding real events at each pipeline step: `planning` → `executing` (includes SQL) → `analyzing` (includes row count) → `charting` → `complete` (full report) |
| 14 | `backend/src/api/routes/query.py` | Stream endpoint rewritten | `/ask/stream` iterates `generate_streaming()` and yields each event as SSE; removed fake `asyncio.sleep()` delays |
| 15 | `frontend/lib/api.ts` | `askStream()` method | New SSE client: reads `ReadableStream`, parses events, calls `onProgress` callback for intermediate stages, returns report from `complete` event |
| 16 | `frontend/app/chat/page.tsx` | Uses streaming | `handleSubmit()` calls `api.query.askStream()` instead of `api.query.ask()`; progress callback updates `stage` state for real-time UI feedback |

### Checklist Update — All High-Priority Items Resolved

- [x] Session tokens stored in `localStorage` (XSS-vulnerable) — moved to httpOnly cookies
- [x] Report access has no ownership check — added owner_id field + 403 on mismatch
- [x] Fake streaming endpoint — replaced with real step-by-step SSE

### All Known Issues Resolved

All 50+ issues identified in Session 1 have been addressed across Sessions 1-5.

---

## Session 6 — 2026-02-09: Pipeline Timeout Fix, Metadata Synonyms & Performance Tuning

### What Was Done
Fixed pipeline statement timeout on large deletes, fixed duplicate staging data, improved AI agent metadata with synonym mappings, switched planner to Haiku for faster responses, and added domain knowledge to analyzer prompt.

### Pipeline Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `extract_asset_tasks.py` | `clear_old_raw_data()` does single DELETE of ~2.2M rows — exceeds Supabase statement timeout | Batched delete: iterates ID range in 50K chunks with `.gte('id', start).lt('id', end)` |
| 2 | `extract_forms.py` | Same unbatched DELETE in `clear_old_raw_data()` — could timeout on large form tables | Same batched delete fix for all 6 form tables |
| 3 | `transform.py` | All full-refresh transforms use `.delete().eq("run_id", run_id)` which only deletes rows matching the NEW (empty) run_id — old data never removed, causing duplicate rows (4.1M instead of 2.2M) | Added `batched_delete_all()` helper; changed `stg_assets`, `stg_asset_tasks`, `stg_qa_form`, and `stg_asset_task_requirements` to delete ALL existing data before inserting |
| 4 | `transform.py` | `stg_timer_activities` left as append mode (correct — preserves historical time logs across runs) | No change needed |

### AI Agent — Performance

| # | File | Change | Details |
|---|------|--------|---------|
| 5 | `backend/src/agent/planner.py` | Switched planner model to Haiku | Changed from `self._settings.claude_model` (Sonnet) to `claude-haiku-4-5-20251001` for both planning and recovery calls — reduces response time from ~14s to ~3s per query |

### AI Agent — Analyzer Domain Knowledge

| # | File | Change | Details |
|---|------|--------|---------|
| 6 | `backend/src/agent/prompts.py` | Added domain knowledge to `ANALYZER_SYSTEM_PROMPT` | New `DOMAIN KNOWLEDGE — STATUS WORKFLOWS` section explaining task statuses (`approved` = completed, not `completed`), project statuses, and that large pending counts are normal — eliminates false anomalies like "Zero completed assets" |

### AI Agent — Schema Metadata Synonyms

| # | File | Change | Details |
|---|------|--------|---------|
| 7 | `backend/migrations/009_schema_metadata.sql` | Added synonym/equivalent word mappings to all 23 metadata rows | Each `business_context` field now includes "User may say:" or "Synonyms:" sections mapping natural language to actual values |

Key synonym mappings added:
- **task_status**: "completed"/"done"/"finished"/"closed" = `approved`, "active"/"ongoing"/"started" = `in_progress`, "waiting"/"queued" = `pending`
- **task_name**: "antenna alignment" = `AAT`, "electrical tilt" = `RET`, "photos" = `Pictures`, etc.
- **task_approved_on**: "completed date"/"finish date"/"done date" → this column
- **asset_name/asset_id**: "site"/"tower"/"location"/"cell site"
- **task_assigned_to_name**: "technician"/"tech"/"worker"/"crew member"/"field tech"
- **stg_qa_form**: "QA"/"quality checks"/"inspections"/"checklists"/"QC"
- **requirement_status**: "passed"/"good" = `Pass`, "failed"/"bad" = `Fail`
- **stg_timer_activities**: "time logs"/"timesheets"/"work hours"/"labor hours"/"clock-in"
- **stg_projects**: "contracts"/"programs"/"phases"/"work orders"

### Pipeline Run Results (Post-Fix)

| Pipeline | Status | Records |
|----------|--------|---------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,111 projects, 10,000 user priorities |
| Asset Tasks | SUCCESS | 2,221,847 rows (no timeout, no duplicates) |
| QA Forms | SUCCESS | ~344K rows |
| Timer Activities | SUCCESS | 2,225 rows |

---

## Session 7 — 2026-02-09: Pipeline Reliability Hardening

### What Was Done
Addressed three reliability improvements recommended during external code review: retry logic on all Supabase writes, row count validation after transforms, and consolidation of duplicated QA_FORMS config.

### Pipeline Changes

| # | File(s) | Change | Details |
|---|---------|--------|---------|
| 1 | `config.py` | Added `retry_supabase()` utility | Wraps any Supabase operation with 3 retries and exponential backoff (1s, 2s, 4s cap 15s). Logs each retry with description. |
| 2 | `config.py` | Moved `QA_FORMS` dict here | Single source of truth for all 6 QA form IDs/table names. Previously duplicated in `extract_forms.py` and `transform.py`. |
| 3 | `base_extractor.py` | Retry on pipeline tracking writes | `start_pipeline_run()` insert and `complete_pipeline_run()` update now wrapped with `retry_supabase` |
| 4 | `extract_asset_tasks.py` | Retry on `load_batch()` and `clear_old_raw_data()` | All raw table inserts and batched deletes now retry on failure |
| 5 | `extract_forms.py` | Retry on `load_batch()` and `clear_old_raw_data()` + removed local `QA_FORMS` | Imports `QA_FORMS` from config instead of defining locally |
| 6 | `extract_timer.py` | Retry on `load_batch()` | Raw timer inserts now retry on failure |
| 7 | `extract_requirements.py` | Retry on `load_batch()` | Raw requirements inserts now retry on failure |
| 8 | `transform.py` | Retry on all staging writes + removed `QA_FORMS_CONFIG` | All 7 staging table insert/upsert operations and `batched_delete_all()` deletes wrapped with retry. Replaced `QA_FORMS_CONFIG` with imported `QA_FORMS`. |
| 9 | `transform.py` | Added `validate_transform_counts()` | Compares raw row count (for run_id) vs transformed count vs staging count after each transform. Logs `[OK]` or `[MISMATCH]` with warning if rows were dropped. |
| 10 | `transform.py` | Validation calls in all `run_*_transform()` functions | Organizations, projects, user priorities, asset tasks, QA forms, timer activities, and requirements all validated. Assets noted as aggregate (staging won't match raw 1:1). |

### Files Modified (7 files, +204/-75 lines)
- `config.py` — `retry_supabase()` + `QA_FORMS` dict
- `base_extractor.py` — retry on pipeline tracking
- `extract_asset_tasks.py` — retry on writes
- `extract_forms.py` — retry on writes + removed duplicate config
- `extract_timer.py` — retry on writes
- `extract_requirements.py` — retry on writes
- `transform.py` — retry on writes + validation + removed duplicate config

### Commit
- `2c09322` — "Add retry logic to Supabase writes, row count validation, and consolidate QA_FORMS config"
- Pushed to `origin/main`

---

## Architecture Notes

### Pipeline Schemas
- `data_raw` — Raw JSONB from API
- `data_staging` — Normalized/transformed tables
- `reference` — Lookup tables (project DIDs)
- `pipeline` — Run tracking metadata

### Key Staging Tables
| Table | ~Rows | Description |
|-------|-------|-------------|
| `stg_projects` | 1,100 | Master project list |
| `stg_asset_tasks` | 2,200,000 | Individual tasks per cell tower |
| `stg_qa_form` | 344,000 | QA checklist responses |
| `stg_timer_activities` | 11,600 | Time tracking with GPS |
| `stg_organizations` | 300 | Organization info |

### AI Agent Request Flow
```
User Question → /api/ask
  → QueryPlanner (Claude: NL → SQL)
  → SQLGuard (validation + security)
  → Database (read-only execute)
  → ResultAnalyzer (Claude: insights)
  → ChartGenerator (Plotly)
  → Report assembled & stored
  → Response returned
```

---

## Session 4 — 2026-02-10: Pipeline Performance Optimization

### Context & Motivation
Full pipeline took **1h 49m** to run all 4 pipelines sequentially. Key bottlenecks identified:
- **Asset Tasks extraction (52 min)**: Only 3 API workers, so 6 projects extracted in pairs instead of simultaneously
- **Asset Tasks transform (30 min)**: Single-threaded, 1000-row batches, Python dict aggregation scanning 2.2M rows
- **QA Forms extraction (17 min)**: Only 3 workers for 6 forms
- **Sequential execution**: All 4 pipelines waited for each other even though 3 are independent

**Goal**: Reduce total runtime from ~110 min to ~40-50 min by attacking each bottleneck.

### Changes Made

#### Optimization 1: Increased Batch Sizes (Low risk)
**Why**: Each Supabase round-trip has ~50-100ms overhead. With 1000-row reads, transforming 2.2M asset tasks required 2,200 round-trips just for reads. Increasing to 5000 cuts that to 440 — saving ~2-3 min per large transform. Same logic for write batches.

| File | Change |
|------|--------|
| `extract_asset_tasks.py` | `LOAD_BATCH_SIZE` 500 → 1000 |
| `extract_forms.py` | `LOAD_BATCH_SIZE` 500 → 1000 |
| `extract_timer.py` | `LOAD_BATCH_SIZE` 500 → 1000 |
| `transform.py` (all transforms) | Read `batch_size` 1000 → 5000 |
| `transform.py` (assets write) | `insert_batch_size` 500 → 2000 |

#### Optimization 2: Increased API Workers (Low risk)
**Why**: Asset tasks has 6 projects but only 3 workers — projects extracted in pairs, doubling extraction time. With 8 workers, all 6 projects extract simultaneously. Forms has 6 forms, so 6 workers is optimal. Conservative vs. 15+ to avoid Swift API rate limits (503 errors seen on TS16 at high load).

| File | Change |
|------|--------|
| `extract_asset_tasks.py` | `MAX_WORKERS` 3 → 8 |
| `extract_forms.py` | `MAX_WORKERS` 3 → 6 |
| `extract_timer.py` | `MAX_WORKERS` 3 → 6 |

#### Optimization 3: SQL Aggregation for Assets (High impact)
**Why**: `transform_assets()` scanned all 2.2M raw_asset_tasks rows in Python using 1000-row batches to build a dict of ~21K unique assets with task status counts. This took ~13 min. PostgreSQL can do this GROUP BY in seconds since the data is already in the DB. Moving aggregation to an RPC function eliminates 2,200+ round-trips entirely.

- Created migration `014_aggregate_assets_rpc.sql` — RPC function `data_raw.aggregate_assets_from_raw()` replaces Python dict aggregation that scanned 2.2M rows in 1000-row batches
- Rewrote `transform_assets()` to call RPC → single SQL GROUP BY query runs in seconds vs ~13 min Python loop
- Migration apply script: `migrations/apply_014.py`

#### Optimization 4: Parallel Transform Writes (Medium impact)
**Why**: Transform functions were read-process-write sequentially — the next batch couldn't start reading until the current batch finished writing to Supabase. By submitting writes to a thread pool, the next read starts immediately while the write completes in the background. This overlaps I/O and reduces wall-clock time.

- Added `ThreadPoolExecutor(max_workers=3)` to 4 transform functions:
  - `transform_asset_tasks()` — writes run in background while next batch is read
  - `transform_qa_forms()` — same pattern
  - `transform_timer_activities()` — same pattern
  - `transform_requirements()` — same pattern
- All futures collected and `.result()` called at end to catch errors

#### Optimization 5: Parallel Pipeline Execution (High impact)
**Why**: QA Forms (18 min) and Timer (18 sec) have zero dependencies on Asset Tasks but ran sequentially after it. Running all 3 in parallel means the total time is max(Asset Tasks, QA Forms, Timer) instead of the sum. Only Orgs/Projects must run first since it populates reference data the others depend on.

- Added `create_supabase_client()` to `config.py` — creates fresh client per thread (vs singleton)
- `main.py` `run_all_pipelines()` now runs:
  - **Phase 1**: Orgs/Projects (must complete first — reference data dependency)
  - **Phase 2**: Asset Tasks, QA Forms, Timer in parallel via `ThreadPoolExecutor(max_workers=3)`
- Each parallel pipeline creates its own Supabase client for thread safety
- Transform wrapper functions (`run_*_transform`) now accept optional `client` parameter

### Files Modified
1. `config.py` — added `create_supabase_client()`
2. `extract_asset_tasks.py` — `MAX_WORKERS` 3→8, `LOAD_BATCH_SIZE` 500→1000
3. `extract_forms.py` — `MAX_WORKERS` 3→6, `LOAD_BATCH_SIZE` 500→1000
4. `extract_timer.py` — `MAX_WORKERS` 3→6, `LOAD_BATCH_SIZE` 500→1000
5. `transform.py` — batch sizes, parallel writes, SQL aggregation for assets, `client` params
6. `main.py` — parallel pipeline execution

### New Files
- `migrations/014_aggregate_assets_rpc.sql` — RPC function for server-side asset aggregation
- `migrations/apply_014.py` — Script to apply migration 014

### Bug Fixes During Pipeline Runs

Three issues surfaced during testing that required fixes:

| # | Issue | Root Cause | Fix |
|---|-------|------------|-----|
| 1 | RPC function looked in `public` schema (PGRST202) | `create_supabase_client()` defaults to `public` schema; RPC function lives in `data_raw` | Changed `rpc_client.rpc(...)` to `rpc_client.schema(SCHEMA_RAW).rpc(...)` |
| 2 | RPC returned only 1,000 assets (expected ~29K) | PostgREST 1000-row cap applies to RPC TABLE return types | Added pagination loop: fetches pages of 1000 with `.range(offset, offset + 999)` until partial page |
| 3 | RPC timed out on 2.2M-row aggregation | PostgREST default `statement_timeout` is ~8s; GROUP BY over 2.2M JSONB rows takes ~2 min | Added `SET statement_timeout = '120s'` to function definition + created index `idx_raw_asset_tasks_run_id` on `run_id` column |

### Pipeline Run Results

| Pipeline | Status | Records | Duration |
|----------|--------|---------|----------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,111 projects, 10,000 user priorities | ~8 min |
| Timer Activities | SUCCESS | 2,252 rows | ~20 sec |
| QA Forms | SUCCESS | 346,208 rows (6 forms) | ~15 min |
| Asset Tasks (extraction) | SUCCESS | 2,225,981 rows (6 projects) | ~34 min |
| Assets (transform via RPC) | SUCCESS | 28,969 unique assets | ~2 min |
| Asset Tasks (transform) | SUCCESS | 2,225,981 rows | ~9 min |

All validations passed `[OK]`.

**Total runtime: ~52 min** (down from **1h 49m baseline** = **52% reduction**)

Note: Initial 503 rate limiting on asset tasks added ~5 min of retries. Stagger delays in `main.py` (forms +10s, timer +5s) help but 18 concurrent API connections (6 asset + 6 form + 6 timer) still overwhelm the Swift API briefly at start.

### Verified End-to-End Run (2026-02-10 05:08 - 06:07)

Full pipeline re-run after all bug fixes — all 4 pipelines passed with SUCCESS:

| Pipeline | Status | Records | Validation |
|----------|--------|---------|------------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,111 projects, 10,000 user priorities | [OK] |
| Timer Activities | SUCCESS | 2,252 rows | [OK] |
| QA Forms | SUCCESS | 346,252 rows (6 forms) | [OK] |
| Assets (RPC aggregation) | SUCCESS | 28,973 unique assets | [OK] |
| Asset Tasks | SUCCESS | 2,226,293 rows | [OK] |

**Total runtime: ~52 min** (baseline: 1h 49m = **53% reduction**)

Breakdown:
- Phase 1 (Orgs/Projects): 05:08 → 05:16 (~8 min)
- Phase 2 (parallel): 05:16 → 06:07 (~51 min)
  - Asset Tasks extraction: ~33 min (6 workers, some 503 retries at start)
  - Asset Tasks transforms: ~18 min (assets RPC: 5 min, asset_tasks: 13 min)
  - QA Forms: ~16 min (ran in parallel, no added time)
  - Timer: ~11 sec (ran in parallel, no added time)

### Git
- Commit: `46b8b34` — "Optimize pipeline performance: parallel execution, SQL aggregation, larger batches"
- Pushed to `origin/main`
- 9 files changed, 261 insertions, 117 deletions

### Updated Architecture Notes
| Table | ~Rows | Description |
|-------|-------|-------------|
| `stg_asset_tasks` | 2,226,000 | Individual tasks per cell tower |
| `stg_qa_form` | 346,000 | QA checklist responses |
| `stg_assets` | 29,000 | Aggregated assets (via SQL RPC) |
| `stg_timer_activities` | 278,900 | Time tracking (append mode) — 266,579 historical + API data |

---

## Session 5 — 2026-02-11: Split User Priorities Pipeline & Fix 10K Row Cap

### Problem
User priorities extraction was capped at exactly 10,000 rows due to an API limit. The actual count is ~10,282. Additionally, user priorities was bundled with orgs/projects in a single pipeline (`pipeline.py`), making them inseparable.

### Changes Made

1. **Fixed 10K row cap** — Extract by status (pending, in_progress) instead of one combined query. Each status excludes the other 5 via `filterOptions`. Results combined.
2. **Split into separate pipeline** — User priorities now has its own extract (`run_user_priorities_extract`), transform (`run_user_priorities_transform`), and CLI (`--pipeline user_priorities`).
3. **Parallel project extraction** — Added `ThreadPoolExecutor(max_workers=10)` to `extract_all_projects()`. Project extraction dropped from ~6 min to ~44 sec.
4. **User priorities in Phase 2** — Runs in parallel with asset tasks, forms, and timer.

### Files Modified
- `extract.py` — Status-based extraction, parallel project extraction (10 workers)
- `pipeline.py` — Split into `run_orgs_projects_extract()` and `run_user_priorities_extract()`
- `transform.py` — Split into `run_orgs_projects_transform()` and `run_user_priorities_transform()`
- `main.py` — Added `run_user_priorities_pipeline()`, updated CLI choices, Phase 2 now has 4 parallel pipelines

### Test Results
| Pipeline | Status | Records | Validation |
|----------|--------|---------|------------|
| Orgs/Projects (standalone) | SUCCESS | 300 orgs, 1,111 projects | [OK] |
| User Priorities (standalone) | SUCCESS | 10,282 (8,279 pending + 2,003 in_progress) | [OK] |

### Pipeline Architecture (final)
```
Phase 1 (sequential):  Organizations & Projects  (~1 min with 10 workers)
                              │
Phase 2 (parallel):    ┌──────┼──────────┬────────────────┐
                       │      │          │                │
                  Asset Tasks  User      QA Forms         Timer
                  (~51 min)   Priorities (~16 min)        (~11 sec)
                              (~1 min)
```

### Git
- Commit: `716199f` — "Split user priorities into separate pipeline and fix 10K row cap"
- Pushed to `origin/main`
- 4 files changed, 225 insertions, 82 deletions

### Updated Architecture Notes
| Table | ~Rows | Description |
|-------|-------|-------------|
| `stg_asset_tasks` | 2,226,000 | Individual tasks per cell tower |
| `stg_qa_form` | 346,000 | QA checklist responses |
| `stg_assets` | 29,000 | Aggregated assets (via SQL RPC) |
| `stg_user_priorities` | 10,300 | Pending + in_progress tasks (per-status extraction) |
| `stg_timer_activities` | 278,900 | Time tracking (append mode) — 266,579 historical + API data |
| `raw_timer_activities_historical` | 266,579 | Historical timer data from Excel (2023-01 to 2026-02) |

---

## Session: 2026-02-11 — Historical Timer Bulk Load

### Changes Made

1. **New migration** `015_raw_timer_historical.sql` — creates `data_raw.raw_timer_activities_historical` table for Excel-sourced timer data
2. **New script** `load_historical_timer.py` — standalone bulk loader: reads Excel, normalizes dates, localizes timestamps to America/New_York, loads raw, transforms to staging
3. **Modified** `transform.py` — added `transform_historical_timer_activities()` function for historical raw-to-staging transformation

### Results
- **266,579 rows** loaded from `timer_activities_data.xlsx` (14 projects: TS5-TS18, date range 2023-01 to 2026-02-10)
- Raw + staging row counts match (verified)
- All project_did values populated (0 nulls)
- Timestamps correctly localized (ET -> UTC)
- Total elapsed: 42 seconds

---

## Session 7 — 2026-02-11: AR Aging Pipeline (Gmail → Supabase)

### What Was Done
Built a new pipeline to extract AR Aging Detail reports from "Daily Revenue Report" Gmail emails, parse the QuickBooks Excel format, and load into Supabase.

### Files Created

| File | Description |
|------|-------------|
| `migrations/016_ar_aging_tables.sql` | Raw (`data_raw.raw_ar_aging`) + staging (`data_staging.stg_ar_aging`) tables |
| `gmail_client.py` | Gmail API OAuth2 authentication, message search, attachment download |
| `parse_aging.py` | QuickBooks AR Aging Detail Excel parser — extracts as_of_date, aging buckets, ~1,400 rows/file |
| `extract_aging.py` | Pipeline orchestrator: Gmail → parse → load raw → transform staging, with dedup by as_of_date |

### Files Modified

| File | Changes |
|------|---------|
| `transform.py` | Added `transform_ar_aging()` + `run_ar_aging_transform()`, added `ar_aging` to CLI |
| `main.py` | Added `run_aging_pipeline_full()`, registered `--pipeline aging` CLI option |
| `.gitignore` | Added `gmail_credentials/` and `token.pickle` |

### Architecture Notes
- **Not** added to `run_all_pipelines()` parallel pool — this is a separate data source (Gmail, not Swift API)
- Does **not** inherit from `BaseExtractor` — uses simpler standalone pattern since Gmail doesn't need Swift API auth/token refresh
- Each email's `as_of_date` is deduplicated — re-runs skip already-loaded dates
- Each file gets its own `run_id` and pipeline_runs tracking entry
- Transform runs inline per-file (not batched across all files)

### Setup Required (one-time)
1. Google Cloud Console → enable Gmail API
2. OAuth 2.0 Client ID (Desktop app) → download `credentials.json`
3. Save to `swift_api_pipeline/gmail_credentials/credentials.json`
4. First run opens browser for Google consent → saves `token.pickle`

### Dependencies Installed
- `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`

---

## Session 8 — 2026-02-11: Sales by Product/Service Pipeline (Gmail → Supabase)

### What Was Done
Built a new pipeline to extract Sales by Product/Service Detail reports from the same "Daily Revenue Report" Gmail emails (second attachment), parse the QuickBooks Excel format, and load into Supabase. Reuses existing `gmail_client.py`.

### Files Created

| File | Description |
|------|-------------|
| `migrations/017_sales_detail_tables.sql` | Raw (`data_raw.raw_sales_detail`) + staging (`data_staging.stg_sales_detail`) tables |
| `parse_sales.py` | QuickBooks Sales by Product/Service Detail Excel parser — extracts as_of_date, 11 flat data columns (no group column per user preference) |
| `extract_sales.py` | Pipeline orchestrator: Gmail → download `Sales+by++ProductService` attachment → parse → load raw → transform staging, with dedup by as_of_date |

### Files Modified

| File | Changes |
|------|---------|
| `transform.py` | Added `transform_sales_detail()` + `run_sales_detail_transform()`, added `sales` to CLI |
| `main.py` | Added `run_sales_pipeline_full()`, registered `--pipeline sales` CLI option |

### Parsing Edge Cases Fixed
QuickBooks date headers had 4 different formats across files:

| Format | Example | Parser Behavior |
|--------|---------|-----------------|
| Single date | `February 9, 2026` | Parsed directly |
| Same-month range | `February 6-8, 2026` | Takes last day (8th) |
| Cross-month range | `October 31 - November 2, 2025` | Takes last date (Nov 2) |
| Month-only | `December 2025` | Uses last day of month (Dec 31) |

### Data Quality Notes
- **`service_date` is proper `date` type** — 4,590 valid dates, 56 nulls (unparseable values return NULL instead of raw text)
- **No group column** — product/service category headers are skipped per user preference. Only the 11 flat data columns are captured.
- **1 known source data quirk**: `po_number='01/08/2026'` for 1 row (KCI Technologies, as_of_date=2026-01-08) — this is genuine QuickBooks source data where someone entered a date as the P.O. Number, not a parsing bug

### Architecture Notes
- **Not** added to `run_all_pipelines()` — separate data source (Gmail, not Swift API)
- Same standalone pattern as `extract_aging.py` — no `BaseExtractor` dependency
- Shares `gmail_client.py` and same Gmail search query as aging pipeline
- Each email's `as_of_date` is deduplicated — re-runs skip already-loaded dates
- Transform runs inline per-file

### Pipeline Run Results

| Metric | Value |
|--------|-------|
| Total rows loaded | 4,646 |
| Unique dates | 96 |
| Date range | 2025-09-23 to 2026-02-09 |
| Raw/staging match | MATCH (4,646 = 4,646) |
| service_date valid | 4,590 dates, 56 nulls |
| Runtime | ~4 minutes |
| Parse errors | 0 (all formats handled) |
| Dedup verified | Re-run skips all dates |

### Full Pipeline Run (background)
Concurrent full pipeline run completed during this session:

| Pipeline | Status | Records |
|----------|--------|---------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,111 projects |
| User Priorities | SUCCESS | 10,287 |
| Timer Activities | SUCCESS | 3,280 |
| QA Forms | SUCCESS | 346,637 |
| Asset Tasks | SUCCESS | 2,228,945 |

### Updated Architecture Notes
| Table | ~Rows | Description |
|-------|-------|-------------|
| `stg_sales_detail` | 4,646 | Daily invoiced sales by product/service (append mode, dedup by as_of_date) |
| `stg_ar_aging` | ~1,400/day | AR aging detail by customer (append mode, dedup by as_of_date) |
| `stg_asset_tasks` | 2,229,000 | Individual tasks per cell tower |
| `stg_qa_form` | 346,600 | QA checklist responses |
| `stg_assets` | 29,000 | Aggregated assets (via SQL RPC) |
| `stg_user_priorities` | 10,300 | Pending + in_progress tasks |
| `stg_timer_activities` | 270,000+ | Time tracking (append mode) |

---

## Session 9 — 2026-02-11: Sales Pipeline Finalization & Full Pipeline Run

### What Was Done

#### Sales Pipeline — Column Mapping Fix Verified
- Confirmed the header-based column mapping (`parse_sales.py`) correctly handles all 5 QuickBooks column layouts
- Investigated the 1 suspicious row with `po_number='01/08/2026'` — confirmed it's **genuine source data** in QuickBooks (someone entered a date as a P.O. Number), not a column swap bug
- Raw JSONB for that row shows `po_number: "01/08/2026"` alongside valid `service_date: "2026-01-08"`, proving the parser mapped columns correctly

#### Full Pipeline Run (background)
All 5 Swift API pipelines ran successfully:

| Pipeline | Status | Records | Notes |
|----------|--------|---------|-------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,111 projects | ~1 min |
| User Priorities | SUCCESS | 10,287 | ~2 min |
| Timer Activities | SUCCESS | 3,280 | ~22 sec (Feb 1-10 range) |
| QA Forms | SUCCESS | 346,637 | ~12 min |
| Asset Tasks | SUCCESS | 2,228,945 | ~39 min extraction + ~18 min transform |

**Total runtime**: ~57 min 39 sec (00:25:08 → 01:22:47)

All row count validations passed. Some 503 retries on Swift API (asset tasks hit up to 5 retries, forms hit 1-2) — all recovered via retry logic.

| Transform | Records | Validation |
|-----------|---------|------------|
| Assets (RPC aggregation) | 29,007 | OK |
| Asset Tasks | 2,228,945 | OK |
| QA Forms | 346,637 | OK |
| Timer Activities | 3,280 | OK |
| User Priorities | 10,287 | OK |
| Organizations | 300 | OK |
| Projects | 1,111 | OK |

---

## Session 7 — 2026-02-11: Pipeline Email Notifications

### What Was Done
Added email notifications to the pipeline so that after each run (individual or full), a summary email is sent via Gmail API with status, duration, per-pipeline details, and the full log as a `.txt` attachment.

### Files Created

| File | Purpose |
|------|---------|
| `pipeline_notifier.py` | Self-contained notification module: `PipelineResult` dataclass, `LogCaptureHandler`, `capture_logs()` context manager, `send_pipeline_email()`, HTML email builder |

### Files Modified

| File | Change |
|------|--------|
| `gmail_client.py` | Added `gmail.send` scope; added scope validation in `authenticate()` to force re-auth if stored token lacks new scopes |
| `main.py` | Integrated email notifications: `run_pipeline_with_notification()` wrapper for individual pipelines, per-pipeline timing in `run_all_pipelines/extractions/transformations`, `--no-email` flag, `send_email` parameter threaded through all run modes |

### Architecture
- `LogCaptureHandler` (deque-backed `logging.Handler`) added alongside existing stdout handler — console output unaffected
- `capture_logs()` context manager attaches/removes handler from `"pipeline"` root logger
- `send_pipeline_email()` wrapped in try/except so email failures never crash the pipeline
- HTML email uses inline CSS: green/red header bar, summary table, per-pipeline details table
- Full log attached as `pipeline_log_YYYYMMDD_HHMMSS.txt`

### First Run Note
First run after this change will open a browser for Google OAuth consent to grant the new `gmail.send` scope. Token is then cached in `gmail_credentials/token.pickle`.

### Verification Steps
1. `python main.py --pipeline timer` — fastest pipeline (~11s), verify email arrives
2. `python main.py --pipeline timer --no-email` — verify no email sent
3. Check that a failed pipeline still sends email with FAILED status

---

## Session 10 — 2026-02-11: Cloud Migration & Pipeline Automation

### What Was Done
Migrated all data from local Supabase (port 54322) to cloud Supabase, updated all configs to point to cloud, and set up Windows Task Scheduler for automated pipeline runs.

### Data Migration — Local to Cloud Supabase

Created `migrate_data_to_cloud.py` — asyncpg COPY protocol (binary format) migration script that streams data table-by-table from local to cloud via temp files on disk.

#### Migration Results

| Table | Rows | Size | Time |
|-------|------|------|------|
| `pipeline.pipeline_runs` | 250 | 0.1 MB | 0.4s |
| `data_raw.raw_organizations` | 3,900 | 3.5 MB | 7.2s |
| `data_raw.raw_projects` | 14,438 | 26.8 MB | 33.7s |
| `data_raw.raw_user_priorities` | 110,847 | 98.8 MB | 2m 10s |
| `data_raw.raw_asset_tasks` | 2,228,945 | 2,374 MB | 61m 07s |
| `data_raw.raw_form_qa_ts13-ts18` | 346,637 | 576 MB | 14m 37s |
| `data_raw.raw_timer_activities` | 6,560 | 2.8 MB | 3.8s |
| `data_raw.raw_timer_activities_historical` | 266,579 | 116 MB | 2m 14s |
| `data_raw.raw_ar_aging` | 153,414 | 55.4 MB | 1m 25s |
| `data_raw.raw_sales_detail` | 4,692 | 2.0 MB | 5.1s |
| `data_staging.stg_organizations` | 300 | 0.1 MB | 0.4s |
| `data_staging.stg_projects` | 1,111 | 0.4 MB | 1.2s |
| `data_staging.stg_user_priorities` | 101,847 | 47.7 MB | 1m 58s |
| `data_staging.stg_assets` | 29,007 | 6.8 MB | 16.5s |
| `data_staging.stg_asset_tasks` | 2,228,945 | 896 MB | 32m 25s |
| `data_staging.stg_qa_form` | 346,637 | 201 MB | 7m 07s |
| `data_staging.stg_timer_activities` | 273,139 | 90.7 MB | 3m 43s |
| `data_staging.stg_ar_aging` | 153,414 | 26.8 MB | 53.2s |
| `data_staging.stg_sales_detail` | 4,692 | 1.1 MB | 2.4s |
| `agent.schema_metadata` | 30 | 0.0 MB | 0.4s |
| **TOTAL** | **6,275,384** | **~4.5 GB** | **128m 57s** |

- **25 tables migrated**, 3 skipped (empty/not in local)
- **All row counts verified** — zero mismatches, zero failures
- Upload throughput: ~0.76 MB/s (network-bound to AWS us-east-1)

#### Post-Migration Cleanup
- Dropped `data_raw.raw_asset_task_requirements` and `data_staging.stg_asset_task_requirements` from cloud (were already removed from local, created by migrations but unused)

### .env Config Updates — Switched to Cloud

| File | Changes |
|------|---------|
| `local-pipeline/swift_api_pipeline/.env` | `SUPABASE_URL` → cloud URL, `SUPABASE_SERVICE_KEY` → cloud service role key |
| `local-ai-agent/backend/.env` | `SUPABASE_HOST` → `db.voqfjfngdpcvevbkikud.supabase.co`, port 5432, user `postgres`, cloud password |
| `local-pipeline/.env` | Added cloud URL/key + direct Postgres connection vars |
| `local-ai-agent/.env` | Added cloud URL/key + direct Postgres connection vars |

### Pipeline Automation — Windows Task Scheduler

#### Task 1: `SwiftPipeline-Nightly`
- **Schedule**: Daily at 12:01 AM
- **Runs**: `python main.py` (full pipeline — all extractions + transforms)
- **Sends**: Email notification on completion/failure
- **Logon mode**: Interactive/Background (runs whether user is logged on or not)

#### Task 2: `SwiftPipeline-Gmail-Hourly`
- **Schedule**: Every 1 hour from 1:00 AM to 10:00 AM daily
- **Runs**: `run_gmail_pipelines.py` — smart polling script that:
  1. Checks if today's `as_of_date` exists in `raw_ar_aging` and `raw_sales_detail`
  2. If both already loaded — exits immediately (no-op, <1 second)
  3. If missing — runs aging and/or sales pipeline for whichever is needed
- **Early stop**: Once both emails are processed, subsequent hourly runs are no-ops
- **Logon mode**: Interactive/Background (runs whether user is logged on or not)

Both tasks run as the `admin` user with cached credentials — no need to be logged in or have the screen unlocked.

#### Files Created

| File | Purpose |
|------|---------|
| `migrate_data_to_cloud.py` | asyncpg COPY migration script (local → cloud) |
| `run_gmail_pipelines.py` | Smart Gmail polling with early-exit logic |
| `scheduled_main_pipeline.bat` | Batch wrapper for nightly Task Scheduler job |
| `scheduled_gmail_pipeline.bat` | Batch wrapper for hourly Gmail Task Scheduler job |
| `task_gmail_pipeline.xml` | XML task definition for hourly-with-time-window schedule |

#### Task Scheduler Management
```bash
# View / run / disable / delete
schtasks /query /tn "SwiftPipeline-Nightly"
schtasks /run /tn "SwiftPipeline-Nightly"
schtasks /change /tn "SwiftPipeline-Nightly" /disable
schtasks /delete /tn "SwiftPipeline-Nightly" /f
```

### Git
- Commit: `1d09940` — "Add Gmail pipelines, cloud migration, historical timer, and Task Scheduler automation"
- Pushed to `origin/main`
- 16 files changed, 2,386 insertions

---

## Session 11 — 2026-02-11: Fix Task Name Cleaning Regex

### Problem
Task names in `stg_qa_form.task_clean` still had number+letter prefixes like `4B. COP Punch Items`, `10B. COP Revision Complete`, `5C. COP Revision Complete`, etc. The existing regex `^\d+\.\s*` only handled plain number prefixes (e.g., `4.`) but not number+letter prefixes (e.g., `4B.`, `10B.`, `2E.`).

### What Was Done

#### 1. Updated Regex in `transform.py`
- **Old:** `r'^\d+\.\s*'` — only matches `4.`, `10.`
- **New:** `r'^(\d+[a-zA-Z]?\.\s*)+'` — matches `4B.`, `10B.`, `5C.`, `2E.`, `1.2.`, etc.
- The `+` repeating group also handles double-prefix patterns like `1.2.`

#### 2. Fixed Existing Cloud Data
- `stg_asset_tasks.task_name_clean` — already clean (0 affected)
- `stg_qa_form.task_clean` — **80 rows updated** across 19 distinct unclean values
- SQL UPDATE applied directly to cloud DB via asyncpg
- Verified: 0 unclean values remaining after fix

### Affected Values (19 distinct patterns fixed)
`1B.`, `2A.`, `2B.`, `2E.`, `4B.`, `4C.`, `4D.`, `5B.`, `5C.`, `5D.`, `5E.`, `9B.`, `10B.`, `13B.`

#### 3. Fixed Nightly Task Scheduler Working Directory
- `SwiftPipeline-Nightly` was missing `Start In` (working directory) — could cause `.env` loading failures
- Created `task_nightly_pipeline.xml` with proper `<WorkingDirectory>` set
- Re-registered task via XML import with user credentials
- Both tasks now confirmed: `Start In: swift_api_pipeline\`

#### 4. Enabled WakeToRun on Both Scheduled Tasks
- PC will now wake from sleep to run tasks at their scheduled times
- Added `<WakeToRun>true</WakeToRun>` to both XML task definitions
- Re-registered both tasks via XML import

#### 5. Fixed Cloud PostgREST Schema Exposure (Critical)
- **Problem:** Cloud Supabase PostgREST only exposed `public` and `graphql_public` — our custom schemas (`data_raw`, `data_staging`, `pipeline`, `agent`) were not accessible via the API
- **Impact:** Pipeline would have failed on every Supabase read/write tonight
- **Fix:** Set `pgrst.db_schemas` on the `authenticator` role via SQL:
  ```sql
  ALTER ROLE authenticator SET pgrst.db_schemas = 'public, data_raw, data_staging, pipeline, agent';
  NOTIFY pgrst, 'reload config';
  ```
- Also granted `agent` schema permissions to `anon`, `authenticated`, `service_role`
- Verified all 4 schemas accessible via Supabase Python client

#### 6. Pre-flight Readiness Check
All components verified working:
- Task Scheduler: both tasks Ready, Enabled, WakeToRun, correct working directory
- .env pointing to cloud Supabase
- PostgREST: all 4 custom schemas accessible
- Swift API authentication: OK
- Venv Python + batch files: exist

#### 7. Dropped Unused `pipeline.requirements_extraction_progress` Table
- Table was empty (0 rows) and not used by any runtime code
- Only referenced in one-time migration script and migration SQL
- Dropped from cloud via `DROP TABLE`

### Git
- Commit: `ea00bf8` — "Fix task name cleaning regex to strip number+letter prefixes (4B., 10B., etc.)"
- Commit: `9d1f4fb` — "Add XML task definition for nightly pipeline with working directory"
- Commit: `4b012bf` — "Enable WakeToRun on both scheduled tasks so pipeline runs during sleep"
- All pushed to `origin/main`
- PostgREST fix + table drop were SQL changes (no code commits needed)

---

## Session 12 — 2026-02-12

### What Was Done

#### 1. Staging Table Relationships — Drop FKs & Add Indexes
- **Dropped 2 FK constraints** that would block pipeline truncate+reload:
  - `fk_stg_asset_tasks_project` (stg_asset_tasks → stg_projects)
  - `fk_timer_project` (stg_timer_activities → stg_projects)
- **Added 2 missing indexes** on join columns:
  - `idx_stg_qa_form_site_id` — enables QA form ↔ asset joins
  - `idx_stg_user_priorities_asset_did` — enables user priorities ↔ asset joins
- Verified `site_id = asset_id` join: 66.4% match for QA forms, 86.2% for timer (unmatched are free-text entries)

#### 2. Updated `agent.schema_metadata` with Relationships
- Inserted 4 missing table-level rows: `stg_assets`, `stg_user_priorities`, `stg_ar_aging`, `stg_sales_detail`
- Updated 5 existing rows with correct `related_tables` arrays
- All 9 staging tables now have relationship metadata with join columns documented

#### Relationship Hierarchy
```
stg_organizations (300)
  └── stg_projects (1,111)              via org_did
        ├── stg_assets (29,007)          via project_did
        │     ├── stg_asset_tasks (2.2M)   via asset_did
        │     ├── stg_qa_form (346K)       via site_id = asset_id
        │     ├── stg_timer_activities (273K) via site_id = asset_id
        │     └── stg_user_priorities (101K)  via asset_did
        ├── stg_timer_activities (273K)  via project_did (also)
        └── stg_user_priorities (101K)   via project_did (also)

stg_ar_aging (153K)      — standalone (financial)
stg_sales_detail (4.6K)  — standalone (financial)
```

#### 3. Created Migration File
- `migrations/018_staging_relationships.sql` — documents FK drops, index additions, and metadata updates

### Future Plan
- Create database views that pre-join common relationships (e.g., `v_asset_tasks_full` joining tasks → assets → projects → orgs)

### Git
- Commit: `5e868ec` — "Add migration for staging table relationships: drop FKs, add indexes, update metadata"
- Pushed to `origin/main`

---

## Session 8 — 2026-02-11: Add asset_did to Timer & QA Form Tables

### Problem
`stg_timer_activities` and `stg_qa_form` only had `site_id` (= `asset_id`) for linking to assets. Since `asset_id` can change over time (it's the human-readable site ID like "ATL001"), old rows in append-mode tables like timer would lose their link to the asset. `asset_did` is the immutable Swift API identifier that never changes.

### What Was Done

#### 1. Created Migration `019_asset_did_backfill.sql`
- Added `asset_did TEXT` column to both `stg_timer_activities` and `stg_qa_form`
- Added indexes: `idx_stg_timer_activities_asset_did`, `idx_stg_qa_form_asset_did`
- One-time backfill via UPDATE...FROM subquery for existing data
- Created persistent lookup table `data_staging.qa_form_asset_did_lookup` (keyed on `site_id`)
  - Preserves `(site_id -> asset_did)` mappings across QA form truncate+reload cycles
  - Seeded with 25,912 initial mappings; grows cumulatively each run
- Created RPC function `data_staging.backfill_asset_did()` with multi-pass matching:
  - **Timer Passes 1-3**: fill NULLs from stg_assets (append mode, no lookup needed)
  - **QA Form Pass 0**: restore from lookup table (recovers asset_did lost during truncate+reload, even if site_id/site_name changed in stg_assets)
  - **QA Form Pass 0b**: fallback match by `site_name` from lookup table
  - **Pass 1**: `site_id = asset_id` (exact match, `DISTINCT ON` for determinism)
  - **Pass 2**: `site_name = asset_name` fallback
  - **Pass 3**: Embedded FA number extraction via `regexp_match(site_id, '/([0-9]{7,8})/')` fuzzy match
  - **QA Form Save**: UPSERT current mappings into lookup table for next run
  - All passes are NULL-only — once `asset_did` is set, never overwritten (write-once immutable)
  - Timer passes use `project_did` constraint; QA Form passes don't (no project_did available)
  - Excludes junk FA number `00000000`
  - `SET statement_timeout = '120s'`, returns `(timer_updated, qa_form_updated)` row counts
- Schema metadata entries for new columns
- Updated `related_tables` on both table-level metadata rows

#### 2. Modified `transform.py`
- Added `backfill_asset_did(client=None)` function (~line 1389)
- Verifies `stg_assets` has data before calling RPC
- Calls `data_staging.backfill_asset_did()` RPC via Supabase client
- Logs timer/QA form rows updated

#### 3. Modified `main.py`
- `run_all_pipelines()`: Added backfill call after Phase 2 parallel block, before Summary
  - Wrapped in try/except so backfill failure doesn't block overall pipeline
  - Tracked as PipelineResult for email reporting
- `run_all_transformations()`: Added `("Asset DID Backfill", backfill_asset_did)` as final transform step

### Pipeline Behavior
- Backfill runs **after all Phase 2 pipelines complete** (stg_assets guaranteed fresh)
- **Timer (append mode)**: Old rows keep their `asset_did` (write-once, never overwritten). New rows appended with `asset_did=NULL`, then backfill Passes 1-3 fill them in.
- **QA Form (truncate+reload)**: All rows wiped and reloaded with `asset_did=NULL` each run. Pass 0 restores from persistent lookup table first, then Passes 1-3 fill remaining NULLs from stg_assets. Save step at the end persists any new mappings for the next run. Lookup table is cumulative — grows over time, never loses established mappings.

### Issues Found & Fixed

#### Non-deterministic JOIN oscillation
Multiple `stg_assets` rows can share the same `(project_did, asset_id)` with different `asset_did` values (e.g., long path-style asset_ids with 62 duplicates). Initial RPC used a direct join which was non-deterministic, causing ~15K timer rows to oscillate between values on each run. Fixed by using `DISTINCT ON ... ORDER BY asset_did` for deterministic selection.

#### UTF-8 BOM corruption in site_id
Found 13 `stg_qa_form` rows with double-encoded BOM (`C3 AF C2 BB C2 BF`, 6 bytes) and 1 `stg_assets` row with single-encoded BOM (`EF BB BF`, 3 bytes). The different encoding meant the strings didn't match despite containing the same text. Stripped BOMs from both tables, which unlocked 26,737 additional QA form matches.

### Coverage Results (Final)
| Pass | Timer | QA Form |
|------|-------|---------|
| 0: Lookup table restore | n/a | recovers all prior mappings |
| 1: site_id = asset_id | 181,208 | 330,528 |
| 2: site_name = asset_name | +4,544 | +403 |
| 3: FA number match | +722 | +744 |
| BOM cleanup bonus | — | +26,737 (from Pass 1) |
| **Total matched** | **186,474 (68.3%)** | **331,987 (95.8%)** |
| Still missing | 86,665 | 14,650 |

#### Remaining unmatched rows (not recoverable)
- **Timer 74,880** — TECH-OPS admin/overhead time (NULL site_id AND NULL site_name, genuinely not asset-linked)
- **Timer ~12K** — stale project paths with no current asset record in stg_assets or stg_asset_tasks
- **QA Form ~15K** — old/renamed site_ids with no matching asset anywhere
- RPC is idempotent: returns 0/0 on re-run

### Files Changed
- `migrations/019_asset_did_backfill.sql` (NEW) — DDL + RPC + metadata
- `swift_api_pipeline/transform.py` — added `backfill_asset_did()` function
- `swift_api_pipeline/main.py` — added backfill in `run_all_pipelines()` and `run_all_transformations()`

### Git
- Commit: `e18781a` — "Add asset_did backfill to stg_timer_activities and stg_qa_form"
- Commit: `28ed3ca` — "Add DBML schema diagram for staging tables"
- All pushed to `origin/main`

---

## Session 13 — 2026-02-12: DBML Diagram, Schema Metadata Export & Column-Level Metadata

### What Was Done

#### 1. DBML Schema Diagram (committed in Session 8)
- Created `docs/staging_schema.dbml` — ER diagram source for dbdiagram.io
- Covers all 10 staging tables with `ref:` annotations for full relationship hierarchy
- Paste into https://dbdiagram.io to visualize

#### 2. Schema Metadata Export to Files
- Exported `agent.schema_metadata` table to local files for easy editing:
  - `docs/schema_metadata.json` — full JSON export
  - `docs/schema_metadata.md` — human-readable markdown, organized by table with descriptions and business context

#### 3. Column-Level Metadata for All Staging Tables
- Created `migrations/020_column_metadata.sql` — 142 new column-level metadata entries
- Covers every user-facing column across all 9 staging tables
- **Skipped pipeline internals**: `id`, `run_id`, `loaded_at`, `created_by_id`, `*_by_did`, `*_collection`, `poc_id`
- Each entry includes description + business context with user synonyms

| Table | Existing Columns | New Columns | Total |
|-------|-----------------|-------------|-------|
| stg_organizations | 5 | 2 | 7 |
| stg_projects | 6 | 13 | 19 |
| stg_assets | 0 | 12 | 12 |
| stg_asset_tasks | 6 | 12 | 18 |
| stg_user_priorities | 0 | 22 | 22 |
| stg_timer_activities | 5 | 13 | 18 |
| stg_qa_form | 5 | 17 | 22 |
| stg_ar_aging | 0 | 13 | 13 |
| stg_sales_detail | 0 | 13 | 13 |
| **Total** | **27** | **142** | **169** |

- Re-exported both `schema_metadata.json` and `schema_metadata.md` with full 178 rows (9 table-level + 169 column-level)

### Files Created/Updated

| File | Description |
|------|-------------|
| `migrations/020_column_metadata.sql` (NEW) | 142 column metadata INSERT statements |
| `docs/schema_metadata.json` (NEW) | Full JSON export of all 178 metadata rows |
| `docs/schema_metadata.md` (NEW) | Human-readable markdown export |

### Git
- Commit: `21a7bb1` — "Add column-level metadata for all staging tables"
- Pushed to `origin/main`

---

## Session 14 — 2026-02-12: Analytics Schema (Pre-joined Views & Summary MVs)

### Goal
Create an `analytics` schema layer so the AI agent can query pre-joined views instead of figuring out multi-table JOINs for every query. Summary materialized views avoid scanning 2.2M+ rows for dashboard metrics.

### Schema Layer Separation
```
data_raw       -> raw API responses (pipeline writes)
data_staging   -> cleaned/transformed data (pipeline writes)
analytics      -> views + MVs (pipeline refreshes, AI agent reads)
```

### What Was Created

#### Regular Views (4)
| View | Joins | Rows |
|------|-------|------|
| `analytics.v_asset_tasks` | tasks + assets + projects + orgs | 2,228,945 |
| `analytics.v_timer_activities` | timer + assets + projects | 283,224 |
| `analytics.v_qa_forms` | QA form + assets + projects | 382,685 |
| `analytics.v_user_priorities` | priorities + assets + projects + orgs | 103,785 |

#### Materialized Views (3) — refreshed by pipeline
| MV | Description | Rows | Refresh Time |
|----|-------------|------|-------------|
| `analytics.mv_project_summary` | Per-project task counts, completion %, hours, QA stats | 1,111 | ~12s |
| `analytics.mv_technician_stats` | Per-technician task counts, completion rate, sites | 31 | ~34s |
| `analytics.mv_daily_completion` | Daily task completions by project/task_type | 46,551 | ~23s |

#### RPC Functions
- `analytics.refresh_materialized_views()` — refreshes all 3 MVs (for direct DB use)
- `analytics.refresh_one_mv(p_view_name)` — refreshes one MV at a time (used by pipeline via PostgREST, avoids Cloudflare 60s timeout)

### Files Changed

| File | Change |
|------|--------|
| `migrations/021_analytics_schema.sql` (NEW) | Schema, views, MVs, indexes, RPC functions, metadata |
| `swift_api_pipeline/transform.py` | Added `refresh_analytics()` — calls `refresh_one_mv` 3x sequentially |
| `swift_api_pipeline/main.py` | Added analytics refresh after backfill_asset_did in both `run_all_pipelines()` and `run_all_transformations()` |
| `local-ai-agent/backend/src/database/schema_cache.py` | Added 'analytics' schema, VIEW type, MV introspection via pg_catalog |

### Issues Encountered & Fixed
1. **QA requirement_status values**: Schema metadata said 'Pass'/'Fail' but actual data uses workflow statuses ('pending', 'approved', etc.). Fixed MV to use 'approved' = pass, 'cancelled' = fail.
2. **PostgREST timeout**: Combined MV refresh (~70s) exceeded Cloudflare proxy timeout. Created `refresh_one_mv()` to refresh one at a time (~12-34s each).
3. **Dollar quoting in asyncpg**: `$$` in inline Python strings caused syntax errors. Fixed by writing SQL to file and reading with `Path.read_text()`.
4. **mv_daily_completion uniqueness**: Changed from project-level to site-level granularity per request. `(completion_date, asset_did, task_type)` had duplicates — added `project_did` to unique index.

### Pipeline Integration
```
Phase 1 (sequential):  Organizations & Projects
Phase 2 (parallel):    Asset Tasks | User Priorities | QA Forms | Timer
Post-Phase 2:          1. Backfill asset_did  →  2. Refresh analytics MVs
```

### Git
- `local-pipeline` — `aea0fcd` (analytics schema + pipeline refresh)
- `local-ai-agent` — `4fa1847` (schema cache introspection for analytics)
- Both pushed to `origin/main`

---

## Session 15 — 2026-02-11: Fix AI Agent Token Overflow & Status Metadata

### Problem
After adding analytics schema (7 tables) to the schema cache, the AI agent's prompt grew to **213,609 tokens** (over the 200K limit) because it was sending ALL 156 tables (14K columns) from 6 schemas (`public`, `data_staging`, `analytics`, `reference`, `staging`, `pipeline`) to Claude Haiku. The agent only needs `analytics` and `data_staging`.

### What Was Done

#### 1. Filtered Schema Context for AI Planner
Added `get_agent_schema_context()` to `SchemaCache`:
- **Analytics tables** (7): Full descriptions with semantic metadata — these are pre-joined views the agent should prefer
- **Data_staging tables** (~15): Compact format as fallback for specific queries
- **Other schemas excluded**: `public` (Supabase system tables), `pipeline`, `staging`, `reference`
- Full cache still maintained for SQLGuard validation (all 156 tables)

Updated `planner.py` to use `get_agent_schema_context()` in both `plan()` and `_recover_from_error()`.

#### 2. Updated Few-Shot Examples
Rewrote all 8 few-shot examples in `prompts.py` to use analytics views:
- `analytics.v_asset_tasks` for task queries (no JOINs needed)
- `analytics.v_timer_activities` for time tracking
- `analytics.v_qa_forms` for QA pass rates
- `analytics.mv_project_summary` for project dashboards
- `analytics.mv_technician_stats` for technician rankings
- `analytics.mv_daily_completion` for trend charts

Updated system prompt to emphasize "prefer analytics views over data_staging tables."

#### 3. Fixed Status Metadata (Two Issues Found During Testing)

**Issue A — QA `requirement_status`**: Agent used `'Pass'` but actual values are `pending, submitted, approved, cancelled, in_progress`. Migration `022_fix_status_metadata.sql` updated metadata. Now agent correctly uses `requirement_status = 'approved'` for pass rates.

**Issue B — `project_status`**: Agent filtered by `'active'` but actual values are `in_progress, complete, pending`. Added metadata for `mv_project_summary.project_status` mapping "active" → `in_progress`. Now agent correctly uses `project_status = 'in_progress'`.

**Root cause**: `get_agent_schema_context()` was outputting `col.description` but not `col.business_context` (where actual values are documented). Fixed to include business_context in prompt output.

Also added metadata for `v_asset_tasks.task_status` with full value documentation.

### Test Results (6 queries tested)

| # | Question Type | View Used | Rows | Time | Result |
|---|--------------|-----------|------|------|--------|
| 1 | Project summary | `mv_project_summary` | 200 | 14.9s | Correct (`project_status = 'in_progress'`) |
| 2 | Top technicians | `mv_technician_stats` | 15 | 127s | Correct |
| 3 | Daily completion trends | `mv_daily_completion` | 84 | 16.8s | Correct |
| 4 | Avg time on site | `v_timer_activities` | 14 | 17.9s | Correct |
| 5 | QA pass rate by crew lead | `v_qa_forms` | 200 | 15.2s | Correct (`requirement_status = 'approved'`) |
| 6 | Task status breakdown | `v_asset_tasks` | 6 | 22.1s | Correct |

All 7 analytics objects exercised. Agent correctly prefers analytics views over data_staging tables.

### Files Changed

| Repo | File | Change |
|------|------|--------|
| local-ai-agent | `backend/src/database/schema_cache.py` | Added `get_agent_schema_context()`, output `business_context` for analytics columns |
| local-ai-agent | `backend/src/agent/planner.py` | Use `get_agent_schema_context()` in `plan()` and `_recover_from_error()` |
| local-ai-agent | `backend/src/agent/prompts.py` | Rewrote 8 few-shot examples for analytics views, added QA/project status domain knowledge |
| local-pipeline | `migrations/022_fix_status_metadata.sql` | Fix `requirement_status`, `project_status` metadata, add `task_status` metadata |

#### 4. Restricted Schema Cache to Our Schemas Only
Schema cache was loading 156 tables from 6 schemas including Supabase system schemas (`public`, `reference`, `staging`). Restricted to only our 5 schemas: `data_raw`, `data_staging`, `analytics`, `pipeline`, `agent`.

**Project rule established**: Never read, write, query, or reference schemas we didn't create. Saved to persistent memory.

#### 5. Frontend End-to-End Verification
Tested the full stack from the Next.js frontend (http://localhost:3000) through the FastAPI backend to the database. Login, query submission, SQL generation via analytics views, chart rendering, and analysis all working correctly.

### Git
- `local-ai-agent` — `df02e45` (schema filter + few-shot), `d0769b3` (status metadata fix), `4ffa01c` (restrict to our schemas only)
- `local-pipeline` — `17c24e4` (migration 022)
- All pushed to `origin/main`

---

## Session 16 — 2026-02-12: Fix Nightly Pipeline Failures

### Problem
The nightly pipeline (12:01 AM) failed with exit code 1 and produced **no log files**. Manual test run revealed **3 distinct issues**:

1. **PGRST106 — `reference` schema not in PostgREST allowed list**: Migration 021 set `pgrst.db_schemas` to `'public, data_raw, data_staging, pipeline, agent, analytics'` but excluded `reference`. Three extractors (`extract_asset_tasks.py`, `extract_timer.py`, `extract_requirements.py`) query `reference.ref_ontel_techops_projects` for project DIDs — all failed immediately.

2. **Missing table — `ref_ontel_techops_projects` never migrated to cloud**: The `migrate_data_to_cloud.py` script didn't include the `reference` schema tables. The `reference` schema existed but was empty on cloud.

3. **WinError 10035 — Windows socket exhaustion**: `[WinError 10035] A non-blocking socket operation could not be completed immediately` during Supabase writes. Compounded by the shared singleton Supabase client across parallel pipelines (thread safety violation) and only 3 retries by default.

4. **No log files — broken timestamp in batch files**: `%date%` on this machine returns `MM/DD/YYYY` (no day name prefix), but the batch files assumed `DDD MM/DD/YYYY` format. The resulting timestamp contained `/` characters, creating invalid Windows filenames. Writes to the log file silently failed.

### Fixes Applied

#### Migration 023 — PostgREST schemas + reference view
- Added `reference` back to `pgrst.db_schemas`: `'public, data_raw, data_staging, pipeline, agent, analytics, reference'`
- Created `reference.ref_ontel_techops_projects` as a **VIEW** on `data_staging.stg_projects` (instead of a static table) — derives `project_number` from `REGEXP_REPLACE(project_name, '^TECH-OPS: TS', '')`. Stays in sync automatically after each pipeline run.
- Granted proper permissions on reference schema
- Applied to cloud DB via `run_migration.py` (psycopg2)

#### Thread-safe Supabase clients
- `base_extractor.py`: Changed `get_supabase_client()` (singleton) → `create_supabase_client()` (per-instance)
- `load.py`: Same change — `SupabaseLoader` now creates its own client
- This prevents `.schema()` state mutation across parallel threads (known pitfall documented in MEMORY.md)

#### Increased retry resilience
- `config.py`: `retry_supabase` default `max_retries` 3 → 5
- Gives more headroom for transient WinError 10035 socket errors (seen even on single pipeline runs)
- Retry waits: 1s → 2s → 4s → 8s → raise (15s total wait)

#### Fixed batch file timestamps
- Both `scheduled_main_pipeline.bat` and `scheduled_gmail_pipeline.bat`:
  - **Old**: `%date:~-4%%date:~4,2%%date:~7,2%` — locale-dependent, broke on `MM/DD/YYYY` format
  - **New**: `wmic os get localdatetime /value` — always returns `YYYYMMDDHHMMSS` format regardless of locale
- Log files now correctly named: `main_20260212_0035.log`

### Verification

| Test | Result |
|------|--------|
| Reference schema query via PostgREST | 6 projects (TS13-TS18) returned correctly |
| Asset tasks extractor `get_project_dids()` | 6 projects OK |
| Timer pipeline end-to-end (`--pipeline timer --no-email`) | SUCCESS — 3,312 rows extracted and transformed |
| WinError 10035 during test | Occurred 4x during transform, all recovered via retry (attempt 2/5) |
| Batch file timestamp | `20260212_0035` — clean, valid filename |

### Files Changed

| Repo | File | Change |
|------|------|--------|
| local-pipeline | `migrations/023_fix_postgrest_schemas.sql` (NEW) | PostgREST fix + reference view creation |
| local-pipeline | `swift_api_pipeline/base_extractor.py` | `get_supabase_client()` → `create_supabase_client()` |
| local-pipeline | `swift_api_pipeline/load.py` | Same singleton → per-instance fix |
| local-pipeline | `swift_api_pipeline/config.py` | `retry_supabase` default 3 → 5 |
| local-pipeline | `swift_api_pipeline/scheduled_main_pipeline.bat` | WMIC timestamp |
| local-pipeline | `swift_api_pipeline/scheduled_gmail_pipeline.bat` | WMIC timestamp |

### Git
- `local-pipeline` — `0a7a61c` — "Fix pipeline failures: PostgREST reference schema, socket retries, log timestamps"
- Pushed to `origin/main`

---

## Session 7 — 2026-02-12: Migrate Pipeline from Supabase PostgREST to asyncpg

### Goal
Replace the Supabase Python SDK (PostgREST HTTP API) with direct asyncpg connections to eliminate:
- 1000-row hard cap per response (pagination loops everywhere)
- Cloudflare ~60s proxy timeout (breaking MV refresh, backfill RPCs)
- ~50-100ms HTTP overhead per round-trip
- PostgREST 8s statement_timeout workarounds
- Thread-safety bugs from stateful `.schema()` calls

### Architecture
- One asyncio event loop in a daemon thread
- asyncpg pool (min=4, max=20, command_timeout=300s) with SSL
- Sync callers (ThreadPoolExecutor workers) use `run_coroutine_threadsafe()`
- JSONB codecs registered for both text (execute/fetch) and binary (COPY) protocols
- Singleton pattern: `get_db()` / `close_db()`

### What Was Done

#### 1. Created `db.py` (NEW)
- `PipelineDB` class: asyncpg pool + background event loop thread
- Sync API: `execute()`, `fetch()`, `fetchval()`, `fetchrow()`, `executemany()`, `copy_records()`
- Binary JSONB codec (version byte + UTF-8 JSON) for COPY protocol
- Text JSONB codec for execute/fetch
- `retry_db()` with exponential backoff replaces `retry_supabase()`
- SSL context for Supabase cloud (CERT_NONE)

#### 2. Updated `config.py`
- Removed: `supabase` SDK imports, `get_supabase_client()`, `create_supabase_client()`, `retry_supabase()`
- Added: `from db import get_db, close_db, retry_db` re-exports

#### 3. Updated `base_extractor.py`
- `self.db = get_db()` instead of `create_supabase_client()`
- Pipeline tracking via parameterized SQL

#### 4. Updated `load.py`
- COPY protocol for bulk raw inserts (raw dicts, not json.dumps)
- Single-query DELETEs (no batched ID-range loops)

#### 5. Updated extractors (`extract_timer.py`, `extract_asset_tasks.py`, `extract_forms.py`)
- Direct SQL for reference lookups (no pagination)
- COPY protocol for raw data loading
- `datetime.date` objects for date columns (COPY binary protocol requirement)
- Raw dicts for JSONB columns (codec handles serialization)

#### 6. Updated `transform.py` (largest change)
- Added `parse_date()` and `parse_timestamp()` helpers for asyncpg type conversion
- Fixed `epoch_to_datetime()` to return datetime objects (was returning ISO strings)
- Fixed local `parse_date()` functions in user_priorities and asset_tasks transforms
- All reads: single `db.fetch()` (no pagination loops)
- All deletes: single `db.execute()` (no batched ID-range deletes)
- All inserts: `db.executemany()` in 5000-row batches
- RPC calls: direct `db.fetch/fetchrow("SELECT * FROM schema.function($1)")`
- Removed ThreadPoolExecutor for writes (unnecessary with COPY/executemany)

#### 7. Updated Gmail pipelines (`extract_aging.py`, `extract_sales.py`, `run_gmail_pipelines.py`)
- `get_db()` singleton instead of multiple `create_supabase_client()` calls
- Direct SQL for dedup checks and pipeline tracking
- Raw dicts for JSONB columns

#### 8. Updated `main.py`
- Removed all `create_supabase_client()` calls and `client=` parameter passing
- Added `close_db()` in `finally` block

#### 9. Updated `.env` and `requirements.txt`
- Added direct DB credentials to inner `.env` (SUPABASE_HOST, PORT, DB, USER, PASSWORD)
- Replaced `supabase==2.10.0` with `asyncpg>=0.29.0`

### Issues Found & Fixed During Testing
1. **Circular import**: `db.py` ↔ `config.py` — fixed by using `logging.getLogger()` directly in db.py
2. **SSL required**: Supabase cloud rejects unencrypted connections — added SSL context
3. **Missing DB credentials**: Inner `.env` only had PostgREST URL/key — added direct DB vars
4. **JSONB binary codec**: COPY protocol needs binary JSONB encoder (version byte 0x01 + JSON bytes)
5. **Date type mismatch**: COPY and executemany require `datetime.date`/`datetime` objects, not strings
6. **Double JSONB encoding**: `json.dumps()` + codec encoder = double-encoded — pass raw dicts instead

### Validation & Debugging (continued session)

**Full pipeline run** (extract+transform, all phases):
- Orgs/Projects: SUCCESS (300 orgs, 1,114 projects)
- User Priorities: SUCCESS (10,222 rows)
- Timer: SUCCESS (3,312 rows extracted + transformed)
- QA Forms extraction: SUCCESS (347,012 rows across 6 forms)
- Asset Tasks extraction: SUCCESS (2,231,519 rows)

**Issues discovered during validation:**

1. **QA Forms INSERT column mismatch** (`INSERT has more target columns than expressions`)
   - Root cause: 80 columns but `range(1, 79)` only generated 78 placeholders
   - Fix: Changed to `range(1, 81)` at transform.py line 768

2. **UUID type mismatch** (`expected str, got UUID`)
   - Root cause: asyncpg returns `asyncpg.pgproto.pgproto.UUID` objects, not strings
   - Fix: `str()` wrapper on all 7 `run_id = row["run_id"]` in transform.py + `run_id = str(run_id)` in `run_assets_transform`

3. **Statement timeout** (`canceling statement due to statement timeout`)
   - Root cause: Supabase default `statement_timeout` is 2 minutes
   - Fix: Added `SET statement_timeout = '300s'` in `_init_connection` callback in db.py

4. **LOAD_BATCH_SIZE too small** (1000 rows = ~26K rows/min for 2.2M asset tasks)
   - Fix: Increased to 10,000 in `extract_asset_tasks.py` and `extract_forms.py`

5. **raw_asset_tasks bloated to 5.3M rows** (3 incomplete run_ids from killed/failed pipeline runs)
   - Fix: TRUNCATED raw_asset_tasks (stg_asset_tasks with 2.2M rows is intact)
   - Tonight's nightly pipeline will refill raw_asset_tasks

6. **Sequence mismatch** (stg_timer_activities_id_seq at 29,666 but max(id) = 302,804)
   - Root cause: Cloud migration COPY'd rows with explicit ids but didn't advance sequences
   - Fix: `setval()` on both `stg_timer_activities_id_seq` and `raw_timer_activities_id_seq`

**Cleanup:**
- Deleted temp scripts: `check_db.py`, `kill_orphans.py`
- Verified all 12 pipeline modules import cleanly
- DB connection pool initializes and closes correctly

**Current DB state (post-fixes):**
| Table | Rows | Status |
|-------|------|--------|
| `raw_asset_tasks` | 0 | Truncated; nightly will refill |
| `stg_asset_tasks` | 2,231,129 | Intact |
| `stg_qa_form` | 347,012 | Intact |
| `stg_timer_activities` | 294,323 | Intact |
| `stg_assets` | 0 | Depends on raw_asset_tasks (nightly will fix) |
| All others | Normal | OK |

### Full Pipeline Test Run (14:48 – 16:08, business hours)

**Results:**
| Pipeline | Status | Details |
|----------|--------|---------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,114 projects |
| Timer Activities | SUCCESS | 3,313 rows (no duplicate key errors after sequence fix) |
| User Priorities | SUCCESS | 10,270 rows |
| QA Forms | SUCCESS | 347,224 rows extracted + transformed |
| Asset Tasks | **FAILED** | 2,233,001 rows extracted + loaded OK, but Assets RPC timed out |
| Asset DID Backfill | SUCCESS | Skipped (stg_assets empty) |
| Analytics MV Refresh | SUCCESS | 3 MVs refreshed in ~50s |

**Total: 80 min** (~30 min was 503 API retries during business hours)

**Issues discovered:**

7. **Assets RPC timeout at exactly 120s** (not our 300s setting)
   - Root cause: asyncpg pool runs `RESET ALL` when connections are returned, which resets `statement_timeout` back to Supabase default (120s). Our `_init_connection` only runs once per new connection, not on re-acquire.
   - Fix: Added `SET statement_timeout = '300s'` to every public method in `db.py` (execute, fetch, fetchrow, fetchval, executemany, copy_records). Each method now sets the timeout on every connection acquire. Adds ~1-2ms per call (~1.2s total across full pipeline), negligible.
   - Verified: `SHOW statement_timeout` returns `5min` on every call.

8. **Asset tasks loading too slow** (40 min for 2.2M rows with 10K batch size)
   - Root cause: 232 individual COPY operations via `run_coroutine_threadsafe()`, each with per-call overhead
   - Fix: Increased LOAD_BATCH_SIZE from 10,000 to 50,000 in `extract_asset_tasks.py`. Reduces COPY operations from 232 to ~46, expected ~75% load time reduction.

### Gmail Pipeline Dedup Fix

**Problem:** The `as_of_date` in the QuickBooks report can be the same for two consecutive days (e.g., today's email and yesterday's email both have `as_of_date = 2026-02-11`). The old dedup logic skipped the second email thinking it was already loaded.

**Fix:** Changed dedup key from `as_of_date` to `email_received_date` (cast to date) in:
- `extract_aging.py` — `get_existing_received_dates()` queries `DISTINCT email_received_date::date`
- `extract_sales.py` — Same change
- `run_gmail_pipelines.py` — `has_todays_data()` now checks `WHERE email_received_date::date = $1::date`

Each email is now uniquely identified by when it was received, not by the report's as-of date. Log messages show both dates for traceability.

### Files Changed

| File | Action | Key Changes |
|------|--------|-------------|
| `db.py` | **NEW** | asyncpg pool, sync bridge, JSONB codecs, retry_db, SET statement_timeout on every acquire |
| `config.py` | MODIFY | Remove Supabase SDK, re-export db functions |
| `base_extractor.py` | MODIFY | `get_db()`, parameterized SQL for pipeline tracking |
| `load.py` | MODIFY | COPY protocol, raw dicts for JSONB, single-query deletes |
| `extract_timer.py` | MODIFY | Direct SQL, COPY writes, datetime.date for date columns |
| `extract_asset_tasks.py` | MODIFY | Direct SQL, COPY writes, raw dicts for JSONB, LOAD_BATCH_SIZE 50K |
| `extract_forms.py` | MODIFY | COPY writes, raw dicts for JSONB, LOAD_BATCH_SIZE 10K |
| `extract_aging.py` | MODIFY | get_db() singleton, direct SQL, dedup by email_received_date |
| `extract_sales.py` | MODIFY | get_db() singleton, direct SQL, dedup by email_received_date |
| `run_gmail_pipelines.py` | MODIFY | get_db(), dedup check by email_received_date |
| `transform.py` | MODIFY | parse_date/parse_timestamp helpers, epoch_to_datetime returns datetime, QA forms column count fix, UUID str() wrappers |
| `main.py` | MODIFY | Remove create_supabase_client, add close_db() in finally |
| `.env` | MODIFY | Added SUPABASE_HOST/PORT/DB/USER/PASSWORD |
| `requirements.txt` | MODIFY | supabase → asyncpg |

---

## Session 8 — 2026-02-13: Pipeline Validation, RPC Timeout Fixes & SQL Transform Optimization

### Goal
Validate the asyncpg migration from Session 7, fix remaining timeout issues discovered during testing, and optimize the asset tasks transform performance.

### Nightly Pipeline Run Analysis (Feb 12, 5:42 AM — pre-fix code)

The scheduled nightly pipeline ran at 5:42 AM (delayed from 12:01 AM, machine likely asleep). This ran **before** Session 7's afternoon fixes were applied, confirming the bugs.

| Pipeline | Status | Details |
|----------|--------|---------|
| Orgs/Projects | SUCCESS | 300 orgs, 1,114 projects |
| Timer Extract | SUCCESS | 3,312 rows loaded to raw |
| Timer Transform | **FAILED** | `statement timeout` (RESET ALL bug) |
| User Priorities | SUCCESS | Full extract + transform |
| QA Forms Extract | SUCCESS | ~347K rows |
| QA Forms Transform | **FAILED** | `statement timeout` |
| Asset Tasks Extract | SUCCESS | 2,233,001 rows from API |
| Asset Tasks Load | **PARTIAL FAIL** | COPY ops timing out 8:01-8:42 AM (process died) |
| Assets RPC / Backfill / Analytics | Never reached | |

Pipeline stuck in infinite timeout retry loop for ~3 hours before dying. Confirms the RESET ALL bug was the root cause.

### Issue #9: RPC function-level SET overrides session timeout

**Problem:** Even with the db.py session-level `SET statement_timeout = '300s'` fix (Issue #7), the `aggregate_assets_from_raw()` and `backfill_asset_did()` RPC functions had their own `SET statement_timeout = '120s'` — as function-level attributes or inside the function body. PostgreSQL function-level SET creates a local GUC context that **overrides** the session setting. So the RPCs were still limited to 120s.

**Root cause:** These timeouts were originally set to override PostgREST's 8s default upward. With asyncpg, they now override our 300s session setting **downward**.

**Fix:** Migration `022_fix_rpc_timeouts.sql`:
- `ALTER FUNCTION data_raw.aggregate_assets_from_raw(text) SET statement_timeout = '300s'`
- `CREATE OR REPLACE FUNCTION data_staging.backfill_asset_did()` with `SET statement_timeout = '300s'` in body

**Verified:** All 4 RPC functions now have 300s:
- `aggregate_assets_from_raw` — 120s → **300s**
- `backfill_asset_did` — 120s → **300s**
- `refresh_one_mv` — 300s (unchanged)
- `refresh_materialized_views` — 300s (unchanged)

### Issue #10: stg_assets column name mismatch (task_pending vs tasks_pending)

**Problem:** `transform_assets()` used `task_pending`, `task_in_progress`, etc. (singular) but the actual table columns and RPC return columns use `tasks_pending`, `tasks_in_progress`, etc. (plural). Also referenced non-existent `milestone_count` column.

**Fix:** Updated column names and dict key access in `transform.py:transform_assets()` to use `tasks_*` (plural). Removed `milestone_count`. Reduced from 14 to 13 values.

### Issue #11: Asset tasks transform too slow (44 min for 2.2M rows)

**Problem:** The `transform_asset_tasks()` function fetched all 2.2M JSONB rows from raw to Python, parsed each one, then inserted back — requiring ~22 fetch round-trips + ~440 insert round-trips. Total: ~44 minutes every pipeline run (full refresh).

**Fix:** Replaced Python fetch+parse+insert with a single server-side `INSERT INTO ... SELECT` SQL statement. All JSONB extraction (`data->>'Field'`), regex cleaning (`REGEXP_REPLACE` for `clean_task_name`), and epoch-to-date conversion (`TO_TIMESTAMP` with timezone) run entirely in PostgreSQL. Zero data transfer.

**Result:** 2,233,625 rows in **2 minutes 7 seconds** (was ~44 minutes). **95% reduction.**

### Pipeline Test Run (16:32 – 17:55, business hours)

| Pipeline | Status | Details |
|----------|--------|---------|
| Organizations & Projects | SUCCESS | 300 orgs, 1,114 projects |
| Timer Activities | SUCCESS | 3,313 rows extracted + transformed |
| User Priorities | SUCCESS | 10,298 rows |
| QA Forms | SUCCESS | 347,290 rows extracted + transformed |
| Asset Tasks Extraction | SUCCESS | 2,233,625 rows loaded |
| **Assets RPC** | **SUCCESS** | **29,067 unique assets in ~3 min** (was timing out at 120s) |
| Asset Tasks Transform | FAILED | Column name mismatch (fixed after run) |
| Asset DID Backfill | Skipped | stg_assets empty due to above |
| Analytics MV Refresh | SUCCESS | 3 MVs in ~60s |

**Post-run manual fixes verified:**
- `transform_assets()` → 29,067 assets inserted (column name fix)
- `backfill_asset_did()` → Timer: 12,982, QA: 332,640 rows updated
- `transform_asset_tasks()` → 2,233,625 rows in 2 min 7 sec (SQL-based transform)

### Current DB State (post-fixes)

| Table | Rows | Status |
|-------|------|--------|
| `raw_asset_tasks` | 2,233,625 | From test run |
| `stg_asset_tasks` | 2,233,625 | Freshly transformed (SQL-based) |
| `stg_qa_form` | 347,290 | Freshly transformed |
| `stg_timer_activities` | 297,636 | OK |
| `stg_assets` | 29,067 | Populated (was 0 before this session) |
| `stg_user_priorities` | 10,298 | OK |

### Gmail Pipeline Dedup Fix — Full Timestamp Comparison

**Problem:** Gmail pipelines (`extract_aging.py`, `extract_sales.py`) compared emails by **date only** (`YYYY-MM-DD`). If 2 "Daily Revenue Report" emails arrive on the same day (common at start of month), the second email was skipped.

**Fix:** Changed dedup to compare by **full timestamp** (`YYYY-MM-DD HH:MM:SS`):
- `get_existing_received_dates()` renamed to `get_existing_received_timestamps()`
- SQL query: removed `::date` cast — returns full `timestamptz` values
- Comparison: `strftime("%Y-%m-%d %H:%M:%S")` instead of `strftime("%Y-%m-%d")`
- `run_gmail_pipelines.py` scheduler check unchanged (date-level is correct for "do we have any data today?")

### Email Notification Enhancements

**Problem:** (1) Log attachment filename used UTC, not Eastern Time. (2) No row count comparison in email — just pipeline status and duration. (3) Pipeline log already in ET (confirmed — Python asctime uses local machine time).

**Fixes:**
1. **Attachment filename** → converted `started_at` to Eastern Time before formatting: `pipeline_log_20260213_000100.txt` (was UTC)
2. **Row Counts table** → new `snapshot_row_counts()` function captures all 9 staging table counts before/after pipeline run. HTML email now includes Before / After / Change columns with color coding (green for increases, red for decreases)
3. Added `row_counts_before` and `row_counts_after` optional params to `send_pipeline_email` and `_build_html_email`
4. All 3 email paths updated: `run_all_pipelines`, `run_all_extractions`, `run_all_transformations`, and `run_pipeline_with_notification`

### Analytics MV Refresh

Manually refreshed all 3 materialized views (were stale because earlier test pipeline failed before reaching `refresh_analytics()`):
- `mv_project_summary`: 1,114 rows (13s)
- `mv_technician_stats`: 40 rows (30s)
- `mv_daily_completion`: 395,041 rows (48s) — was 0 rows before refresh

### Scheduled Task Verification

Confirmed both scheduled tasks are properly configured for tonight's run:
- `WakeToRun = true` — computer wakes from sleep to run
- `StartWhenAvailable = true` — if missed, runs ASAP
- `ExecutionTimeLimit = PT3H` (nightly) / `PT1H` (Gmail)
- Machine clock is Eastern Standard Time

### Files Changed

| File | Action | Key Changes |
|------|--------|-------------|
| `migrations/022_fix_rpc_timeouts.sql` | **NEW** | ALTER aggregate_assets 120s->300s, REPLACE backfill_asset_did 120s->300s |
| `db.py` | **NEW** | asyncpg pool manager — replaces all Supabase SDK usage |
| `transform.py` | MODIFY | Server-side SQL `INSERT INTO...SELECT` for asset tasks (95% faster), `tasks_*` plural column names for stg_assets, removed milestone_count |
| `config.py` | MODIFY | Removed Supabase SDK, re-exports from db.py |
| `base_extractor.py` | MODIFY | `get_db()` instead of `create_supabase_client()` |
| `load.py` | MODIFY | COPY protocol for raw inserts, single-query deletes |
| `extract_asset_tasks.py` | MODIFY | Direct SQL reads, COPY writes, simplified deletes |
| `extract_timer.py` | MODIFY | Direct SQL reads, COPY writes |
| `extract_forms.py` | MODIFY | COPY writes, simplified deletes |
| `extract_aging.py` | MODIFY | Direct SQL, COPY writes, timestamp dedup (was date-only) |
| `extract_sales.py` | MODIFY | Direct SQL, COPY writes, timestamp dedup (was date-only) |
| `run_gmail_pipelines.py` | MODIFY | Direct SQL count query |
| `main.py` | MODIFY | Remove client passing, add close_db(), row count snapshots for email |
| `pipeline_notifier.py` | MODIFY | ET attachment filename, row counts before/after table in email |
| `requirements.txt` | MODIFY | Removed supabase, added asyncpg |

### Git

Committed and pushed as: `Migrate pipeline from Supabase PostgREST to asyncpg with SQL transforms`

---

## Session 9 — 2026-02-13: First Successful Nightly Run & Speed Improvement Planning

### Nightly Pipeline Run (Feb 13, 00:01 – 01:53)

First fully successful nightly run after the asyncpg migration. All 7 steps completed with SUCCESS status. Email notification sent to jamil.mendez@ontel.co.

| Step | Pipeline | Status | Duration | Details |
|------|----------|--------|----------|---------|
| 1 | Orgs & Projects | SUCCESS | 1m 7s | 300 orgs, 1,114 projects |
| 2 | Timer Activities | SUCCESS | 32s | 3,312 rows |
| 2 | User Priorities | SUCCESS | ~1m 28s | 10,310 rows |
| 2 | QA Forms | SUCCESS | ~38m | Extraction ~24m, Transform ~14m |
| 2 | Asset Tasks | SUCCESS | **1h 49m** | See breakdown below |
| 3 | Backfill asset_did | SUCCESS | 35s | Timer + QA form backfill |
| 4 | Analytics MV Refresh | SUCCESS | 1m 46s | 3 MVs refreshed |
| **Total** | | **SUCCESS** | **1h 52m 23s** | Exit code 0 |

### Asset Tasks Timing Breakdown (the bottleneck)

| Phase | Duration | Notes |
|-------|----------|-------|
| API Extraction | 33 min | 6 workers, 2,233,703 rows from Swift API |
| **Loader (COPY to DB)** | **69 min** | **1 thread, 50K batch COPY, 2.2M JSONB rows** |
| Old Raw Cleanup | 2 min | DELETE old run_id rows |
| Assets RPC | 4 min | aggregate_assets_from_raw → 28,900 assets |
| SQL Transform | 3 min | INSERT INTO...SELECT for stg_asset_tasks |
| **Total Asset Tasks** | **1h 49m** | Loader is 63% of total |

### Row Counts After Run

| Table | Rows |
|-------|------|
| `stg_asset_tasks` | 2,233,703 |
| `stg_assets` | 28,900 |
| `stg_qa_form` | 347,327 |
| `stg_timer_activities` | 305,278 |
| `stg_user_priorities` | 10,310 |

### Performance Bottlenecks Identified

1. **Asset Tasks Loader: 69 min** — Single loader thread doing sequential COPY of 50K batches for 2.2M JSONB rows. The 6 extraction workers finish in 33 min but the loader takes another 69 min to flush all queued data.
2. **QA Forms: 38 min total** — Extraction takes 24 min (OK), but transform takes 14 min (Python-side parsing of 347K JSONB rows, same pattern that was already fixed for asset tasks).

### Speed Improvements Planned (next task)

1. **Multiple loader workers** — 3 concurrent threads reading from the same Queue (currently 1). Expected ~3x improvement on the 69-min loader phase.
2. **Increase batch size** — From 50K to 100-200K rows per COPY call. Fewer round-trips, better throughput.
3. **Server-side SQL transform for QA Forms** — Replace Python fetch+parse+insert with `INSERT INTO...SELECT` (same pattern used for asset tasks). Expected: 14 min → ~1-2 min.

---

## Session 10 — 2026-02-13: Speed Optimizations + Gmail Pipeline Fixes

### Speed Improvements Completed

#### 1. QA Forms Transform — Server-Side SQL (14 min → 48 sec)
- **File**: `transform.py` — `transform_qa_forms()`
- Replaced Python-side fetch+parse+insert with server-side `INSERT INTO...SELECT` with UNION ALL across all 6 raw form tables
- Field name alternatives handled with `COALESCE(NULLIF(data->>'Key1', ''), data->>'Key2')`
- Project number extraction via `REGEXP_MATCH`, task name cleaning via `REGEXP_REPLACE`
- **Verified**: 347,327 rows in 48s (exact match), backfill_asset_did coverage restored to 95.8%

#### 2. Asset Tasks Loader — Direct Write + UNLOGGED + Index Drop (69 min → TBD tonight)
- **File**: `extract_asset_tasks.py` — Complete rewrite
- Eliminated Queue + separate loader threads entirely
- Each of 6 extraction workers writes directly to DB after each API page (100K batch flush)
- `prepare_table_for_bulk_load()`: `ALTER TABLE SET UNLOGGED` + drop 3 btree indexes
- `restore_table_after_load()`: recreate indexes + `SET LOGGED`
- **Permanently dropped GIN index** on `data` column (3.2 GB, never used by pipeline or agent)
- Table size: 5,834 MB → 2,594 MB (saved 3.2 GB)
- Error handling: restore table state even on failure

### Gmail Pipeline Fixes (4 cascading bugs)

Gmail pipelines (AR Aging + Sales Detail) were broken since the asyncpg migration (Session 8). All hourly runs from 1-4 AM failed.

| # | Bug | Root Cause | Fix |
|---|-----|-----------|-----|
| 1 | `'str' object has no attribute 'toordinal'` in scheduler | `run_gmail_pipelines.py` — `get_today_date()` returned string, asyncpg needs `datetime.date` | Return native `date` object |
| 2 | Same error in extractors | `extract_aging.py` / `extract_sales.py` — `as_of_date` and `email_received_date` passed as strings | Pass native `date`/`datetime` objects |
| 3 | `duplicate key violates raw_ar_aging_pkey` | `raw_ar_aging_id_seq` at 50, max ID was 153,414 (stale from COPY migration) | `setval()` on raw sequences |
| 4 | `duplicate key violates stg_ar_aging_pkey` | `stg_ar_aging_id_seq` at 10 (same root cause — identity columns not advanced by COPY) | `setval()` on staging sequences |

**Files modified**: `run_gmail_pipelines.py`, `extract_aging.py`, `extract_sales.py`
**Sequences fixed**: `raw_ar_aging_id_seq`, `raw_sales_detail_id_seq`, `stg_ar_aging_id_seq`, `stg_sales_detail_id_seq`

### Gmail Pipeline Results After Fix

- **AR Aging**: 10 new emails transformed successfully (1,367-1,772 rows each), stg_ar_aging now at 169,277 rows through 2026-02-13
- **Sales Detail**: Already loaded through 2026-02-12, today's email not yet received
- 28 older failed run records cleaned up (had no raw data — extraction failures from before)

### Key Lesson: asyncpg + COPY + Identity Columns

When data is loaded via COPY with explicit ID values (or `OVERRIDING SYSTEM VALUE`), PostgreSQL identity sequences are NOT advanced. After any bulk data migration, all sequences must be synced with `setval('schema.seq_name', max_id)`. This affected 4 sequences across raw and staging schemas.

---

## Session 11 — 2026-02-13: Deep Validation, Timer Dedup & Data Restoration

### Deep Pipeline Validation

Ran comprehensive validation across all pipeline components after the asyncpg migration.

#### Code Audit (13 files)
- **No leftover Supabase SDK references** — migration complete
- **DST bug found**: `run_gmail_pipelines.py` used hardcoded `timezone(timedelta(hours=-5))` instead of DST-aware timezone
- **Dead parameters**: 10 transform functions still had `client=None` parameter from pre-migration
- **False positives**: `extract_aging.py`/`extract_sales.py` type flags were incorrect — types already native

#### DB Schema Validation (82/84 passed)
- All tables, columns, types verified across `data_raw`, `data_staging`, `pipeline`, `analytics`
- All RPCs verified: `aggregate_assets_from_raw`, `backfill_asset_did`, `refresh_one_mv`
- All views + materialized views verified
- **2 stale sequences found and fixed**:
  - `raw_sales_detail_id_seq`: was 0, max=15,442
  - `raw_timer_activities_historical_id_seq`: was 0, max=533,158

#### End-to-End Pipeline Tests (ALL PASSED)
- Timer pipeline: 4,329 rows extracted+transformed in 33s
- Full transform mode: all 8 transforms passed (9m 25s)
- Gmail scheduler dedup: correctly detected today's data
- Import validation: 51/51 checks passed

### Fixes Applied

| # | Fix | File |
|---|-----|------|
| 1 | DST-aware timezone: `ZoneInfo("America/New_York")` replacing hardcoded `-5h` | `run_gmail_pipelines.py` |
| 2 | Removed dead `client=None` from 10 function signatures | `transform.py` |
| 3 | Synced 2 stale sequences with `setval()` | DB migration |

### Timer Duplicate Cleanup

Multiple test runs on Feb 12 caused 32,843 duplicate rows in `stg_timer_activities` (309,607 → 276,764 rows).

- **Root cause**: Timer uses append mode — overlapping date ranges from 8 test runs created duplicate activities
- **Dedup key**: `(start_time, end_time, user_email, site_id, task, project_did, run_date)`
- **Method**: Kept latest row per dedup key, deleted older duplicates
- **Result**: 0 true duplicates remaining after cleanup

### AR Aging / Sales Detail Data Restoration

Incorrectly deleted 14,387 AR aging rows and 105 sales detail rows based on duplicate `as_of_date` grouping.

- **User correction**: Same `as_of_date` from different `email_received_date` timestamps is **normal** (e.g., Friday report re-sent Monday)
- **Only duplicate `email_received_date` timestamps would be abnormal** (same email processed twice) — and 0 existed
- **Fix**: Ran `--reprocess` to re-download all 99 emails from Gmail and reload both pipelines
- **Result**: All data restored — AR Aging: 320,897 rows, Sales Detail: 9,485 rows (99 dates each)

### Key Lessons

1. **Same `as_of_date` ≠ duplicate**: Reports for the same business day can arrive on different calendar days. Dedup must use `email_received_date` timestamp, not `as_of_date`.
2. **Timer append mode creates duplicates on re-runs**: Unlike truncate+reload tables, timer data accumulates. Multiple runs for overlapping date ranges will create duplicate activity rows.
3. **`--reprocess` is a one-time recovery tool**: Normal pipeline runs use dedup on `email_received_date` and don't need reprocessing.

### Final Verified Row Counts

| Table | Rows | Status |
|-------|------|--------|
| `raw_ar_aging` | 320,897 | 99 dates, restored |
| `raw_sales_detail` | 9,485 | 99 dates, restored |
| `stg_ar_aging` | 320,897 | Matches raw |
| `stg_sales_detail` | 9,485 | Matches raw |
| `stg_timer_activities` | 276,764 | Deduped, clean |

---

## Session 9 — 2026-02-13: Asset Tasks Excel Export + Google Drive Integration

### What Was Done

Built an automated Excel export script that generates a multi-tab `.xlsx` file from `data_staging.stg_asset_tasks`, uploads it to Google Drive, and emails a shareable link.

### New Files

| File | Purpose |
|------|---------|
| `scripts-reference/export_asset_tasks_excel.py` | Main export script — query, Excel write, Drive upload, email |

### Modified Files

| File | Change |
|------|--------|
| `swift_api_pipeline/gmail_client.py` | Added `drive.file` OAuth scope + `authenticate_drive()` function |
| `swift_api_pipeline/scheduled_main_pipeline.bat` | Added post-pipeline step to run Excel export automatically |

### Export Details

- **Format**: One tab per TECH-OPS project (TS18 → TS13, descending), 28 columns matching original `20260109.xlsx`
- **Column mapping**: `stg_asset_tasks` joined with `stg_projects`, `loaded_at` → `retrieved_at`
- **Date formats**: Task dates use `mm-dd-yy`, `retrieved_at` uses `m/d/yy h:mm` (matching original)
- **Timezone**: All datetimes converted to America/New_York (Eastern Time)
- **Output**: `scripts-reference/data_sample/YYYYMMDD.xlsx` (named by date)
- **Row count**: 2,233,703 rows across 6 tabs (~149 MB)

### Performance Optimizations

| Optimization | Impact |
|---|---|
| Overlap DB fetch + Excel write (asyncio + ThreadPoolExecutor) | DB time: 166s → 22s (only first fetch blocks) |
| Skip None values | ~60% of cells are NULL — millions of writes eliminated |
| Local variable caching in write loop | Avoids attribute lookups in hot loop |
| Pre-computed column type sets | No per-cell `isinstance` checks |
| **Total runtime** | **~4.5 min** (down from ~8 min) |

### Google Drive Integration

- Added `drive.file` scope to existing OAuth2 credentials (alongside `gmail.readonly` + `gmail.send`)
- Export uploads to "Asset Tasks Exports" folder in Google Drive
- Subsequent runs update the same file (by name) rather than creating duplicates
- Sets "anyone with link can view" permission
- Sends HTML email to `jamil.mendez@ontel.co` with file info + download button
- `--no-upload` flag available to skip Drive upload and just generate the file locally

### Pipeline Automation

- `scheduled_main_pipeline.bat` now runs the export after the main pipeline + email notification complete
- Export logs go to the same `pipeline_logs/main_YYYYMMDD_HHMM.log` file
- Nightly flow: Pipeline → Status Email → Excel Export → Drive Upload → Export Email

### Dependencies

- `xlsxwriter` (newly installed) — streaming Excel writer, handles 400K+ rows per sheet efficiently
- `google-api-python-client` — already installed (used for Gmail), now also used for Drive API

---

## Session 10 — 2026-02-15: Pipeline Fix — Index Timeout + Venv xlsxwriter

### Issue: Nightly Pipeline Failed (2026-02-15 00:01 AM run)

**Root Cause 1: Asset Tasks index creation exceeded statement_timeout**
- `idx_raw_asset_tasks_project_did` CREATE INDEX on 2.2M rows took 338s
- Server-side `statement_timeout` was 300s → connection killed at 00:51:57
- Error: `connection was closed in the middle of operation`
- Pipeline reported FAILED for Asset Tasks (all other pipelines succeeded)

**Root Cause 2: Excel export script failed with `ModuleNotFoundError: No module named 'xlsxwriter'`**
- `xlsxwriter` was installed in global Python but not in the pipeline venv
- Batch file uses `venv\Scripts\python.exe`

### Fixes Applied

| File | Change |
|------|--------|
| `swift_api_pipeline/db.py` | Added `statement_timeout` parameter to `execute()` for per-call override of the default 300s |
| `swift_api_pipeline/extract_asset_tasks.py` | `restore_table_after_load()` now uses `statement_timeout=600` (10 min) for index creation + SET LOGGED |
| Pipeline venv | Installed `xlsxwriter` in `swift_api_pipeline/venv/` |

### Timeline from Log

| Time | Event |
|------|-------|
| 00:01 | Pipeline started |
| 00:02:22 | Asset Tasks extraction began (6 projects in parallel) |
| 00:02:55 | First 503 retries from Swift API |
| 00:44:05 | All 6 projects extracted (2.2M rows) |
| 00:44:09 | Index recreation started |
| 00:45:14 | `idx_raw_asset_tasks_loaded_at` created (65s) |
| 00:46:19 | `idx_raw_asset_tasks_run_id` created (65s) |
| 00:51:57 | `idx_raw_asset_tasks_project_did` **FAILED** at 338s (>300s timeout) |
| 00:54:49 | Pipeline finished, email sent |
| 00:54:49 | Excel export failed — `xlsxwriter` not in venv |

### Additional Debugging (manual re-run during business hours)

Even with `statement_timeout=600`, index creation still failed at exactly 6 minutes. Two additional layers discovered:

**Layer 2: asyncpg client-side timeout (`command_timeout=300`)**
- The pool's `command_timeout=300` is a **client-side** asyncio timeout, separate from PostgreSQL's `statement_timeout`
- Even if the server allows 600s, asyncpg cancels the coroutine at 300s
- **Fix**: `db.execute()` now passes `effective_timeout` (derived from `statement_timeout`) to asyncpg's `timeout` parameter

**Layer 3: Supabase connection proxy (~5-6 min hard limit)**
- Supabase's infrastructure proxy (Supavisor/PgBouncer) kills connections exceeding ~5-6 min
- Cannot be overridden by any client or server setting
- `ALTER TABLE SET LOGGED` on 2.2M rows takes >6 min → always killed
- Even `TRUNCATE` of the UNLOGGED table was killed (table was left in bad state)

**Recovery**: Dropped and recreated `raw_asset_tasks` as a LOGGED table (DDL is metadata-only, instant). Table is empty pending tonight's nightly run.

**Fix**: Removed UNLOGGED/LOGGED entirely from pipeline. Index drop/recreate provides the main bulk-load speed benefit anyway.

### Three-Layer Timeout Model (Supabase + asyncpg)

| Layer | Setting | Default | Where |
|-------|---------|---------|-------|
| 1. Server | `statement_timeout` | 120s (Supabase) → we SET 300s | PostgreSQL GUC |
| 2. Client | `command_timeout` / `timeout` param | 300s (pool) | asyncpg |
| 3. Infrastructure | Connection proxy | ~5-6 min (hard) | Supabase proxy |

All three must be within limits for long-running operations to succeed.

### Commits

| Commit | Description |
|--------|-------------|
| `7ebf9d4` | Add `statement_timeout` param to `db.execute()`, set 600s for index creation, install xlsxwriter in venv |
| `b56e049` | Remove UNLOGGED/LOGGED, fix client-side timeout passthrough via `effective_timeout` |
