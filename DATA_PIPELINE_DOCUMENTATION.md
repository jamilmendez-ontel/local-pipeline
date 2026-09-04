# Data Pipeline Documentation

Complete reference for how data flows through all pipelines — extraction, transformation, cleaning, deduplication, and correction logic.

---

## Table of Contents

1. [Pipeline Overview](#1-pipeline-overview)
2. [Timer Activities Pipeline](#2-timer-activities-pipeline)
3. [Timer Data Cleaning & Deduplication](#3-timer-data-cleaning--deduplication)
4. [Timer Discrepancies & Corrections](#4-timer-discrepancies--corrections)
5. [RawTimeData File vs Pipeline Comparison](#5-rawtimedata-file-vs-pipeline-comparison)
6. [Asset Tasks Pipeline](#6-asset-tasks-pipeline)
7. [QA Forms Pipeline](#7-qa-forms-pipeline)
8. [Calendar Events Pipeline](#8-calendar-events-pipeline)
9. [Gmail Package Scraper Pipeline](#9-gmail-package-scraper-pipeline)
10. [Organizations & Projects Pipeline](#10-organizations--projects-pipeline)
11. [User Priorities Pipeline](#11-user-priorities-pipeline)
12. [Analytics Layer](#12-analytics-layer)
13. [Database Architecture](#13-database-architecture)
14. [Scheduling & Automation](#14-scheduling--automation)
15. [Historical Data Import (2026-03-28)](#15-historical-data-import-2026-03-28)

---

## 1. Pipeline Overview

### Architecture

```
Swift API ─────┐
Google Calendar ┤    ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐    ┌───────────┐
Google Forms ───┤───>│  data_raw    │───>│ data_staging  │───>│ stg_*_clean     │───>│ analytics │
Gmail API ──────┘    │  (JSONB)     │    │ (parsed)      │    │ (dedup+correct) │    │ (views/MVs│
                     └─────────────┘    └──────────────┘    └─────────────────┘    └───────────┘
```

### Pipeline Phases (main.py)

1. **Phase 1 (Sequential):** Organizations & Projects — reference data, must run first
2. **Phase 2 (Parallel, 4 workers):** Asset Tasks, User Priorities, QA Forms, Timer Activities
3. **Post-Phase 2 (Sequential):** Asset DID Backfill → Analytics MV Refresh

### Separate Pipelines (not part of main)

- Gmail Aging + Sales (`gmail-pipeline.yml`)
- Gmail Package Scraper (`gmail-scraper/`)
- Calendar Events (`extract_calendar_events.py`)
- Timer Discrepancies (`extract_timer_discrepancies.py`)

---

## 2. Timer Activities Pipeline

### Source

- **API:** `GET /api/timer-activities/_report` per project per day
- **Daily chunking:** One API call per day per project to avoid the ~1K row silent truncation cap
- **Date range:** 1st of current month to yesterday (on 1st of month: previous full month)
- **Timezone:** America/New_York for all date params

### Extraction (`extract_timer.py`)

1. 6 parallel workers fetch from `ref_ontel_techops_projects`
2. Queue-based batching: API → Queue → Loader thread
3. Batch size: 1,000 records per DB flush
4. COPY protocol for binary transfer

### Raw Storage

**Table:** `data_raw.raw_timer_activities`

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Auto-increment PK |
| run_id | UUID | Pipeline run identifier |
| project_did | TEXT | Project reference |
| start_date, end_date | DATE | Extraction month range |
| run_date | DATE | Pipeline execution date |
| data | JSONB | Raw API response |

**Historical table:** `data_raw.raw_timer_activities_historical` — cumulative archive with same JSONB structure plus `source_file` column.

### Transform (`transform.py`)

1. **Delete-and-reload:** Deletes ALL staging rows for the extracted month's `start_date`, then re-inserts from raw
2. **Field extraction:** Parses JSONB → structured columns
3. **Task cleaning:** Strips leading numbers (e.g., "6. Final COP Complete" → "Final COP Complete") and trailing revision numbers
4. **Timezone:** Timestamps stored as `timestamptz` (UTC internally, displayed as ET)

### Staging Table

**Table:** `data_staging.stg_timer_activities`

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Auto-increment PK |
| project | TEXT | Project name (e.g., "TECH-OPS: TS17") |
| project_number | INT | Extracted from name (17 from "TS17") |
| project_did | TEXT | Reference key |
| site_name | TEXT | Site name |
| site_id | TEXT | Site path (e.g., "MRC Towers/VZW/Mountain Plains/Embedded/17006919/Dec 2024") |
| task | TEXT | Raw task name |
| task_clean | TEXT | Cleaned task name |
| site_lat, site_long | NUMERIC | Site GPS |
| user_lat, user_long | NUMERIC | User GPS at timer start |
| user_accuracy_m | NUMERIC | GPS accuracy |
| site_vs_user_km | NUMERIC | Distance between site and user |
| start_time, end_time | TIMESTAMPTZ | Timer start/stop |
| duration_min | NUMERIC | Timer duration in minutes |
| user_name, user_email, user_role | TEXT | Technician info |
| run_id | UUID | Pipeline run |
| run_date | DATE | Pipeline date |
| start_date, end_date | DATE | **NOTE: 1st of extraction month, NOT actual activity date** |
| asset_did | TEXT | Backfilled link to stg_assets |
| loaded_at | TIMESTAMPTZ | Row insert timestamp |

**IMPORTANT:** The `start_date` column stores the 1st of the extraction month, not the actual timer date. Always use `(start_time AT TIME ZONE 'America/New_York')::date` for actual date matching.

### Deduplication

- **Within extraction:** Monthly delete-and-reload prevents duplicates from re-extraction
- **Cross-month:** Transform deduplicates by `(project_did, user_email, start_time)` to handle boundary overlap from daily chunking
- **Duplicate review:** Separate system detects entries with same `(project_did, user_email, start_time, site_name, site_id, task)` but different `end_time`/`duration_min` — see Section 3

---

## 3. Timer Data Cleaning & Deduplication

### Clean Table

**Table:** `data_staging.stg_timer_activities_clean`

Same schema as `stg_timer_activities`. Rebuilt from scratch by RPC `data_staging.rebuild_timer_clean()`:

**Step 1:** TRUNCATE clean table

**Step 2:** INSERT from staging, with these exclusions:
- **Rejected duplicates:** Entries matching `rejected_entries` JSONB in resolved `stg_timer_duplicate_reviews`
- **Unresolved duplicates:** For pending/notified groups, keep only the entry with latest `end_time`
- **Removed entries:** Entries matching `stg_timer_entry_removals` (unless reverted or overridden by a correction)
- **DISTINCT ON** collapses exact-duplicate rows (same in all fields except id)

**Step 3:** UPDATE — Apply duration corrections from `stg_timer_corrections` where `status = 'corrected'`, matching on full natural key including `original_duration_min`

### Duplicate Detection (`timer_duplicate_review.py --notify`)

**Detection key:** `(project_did, user_email, start_time, site_name, site_id, task)`

Entries in the same group share the exact same start_time but differ in `end_time` and/or `duration_min`. This happens when:
- Tech starts timer, stops it, starts again on same task → two entries with same start
- Swift mobile/desktop sync issues creating phantom entries

**Process:**
1. SQL groups entries by the detection key
2. Filters to completed entries only (`end_time IS NOT NULL`)
3. Labels entries A, B, C... sorted by duration ascending
4. Creates 12-char MD5 `group_id` hash
5. Emails each tech with a Google Form to pick which entry is correct
6. Auto-resolves after 7 days → keeps entry with latest `end_time`

**Resolution storage:** `stg_timer_duplicate_reviews` (status, entries JSONB, rejected_entries JSONB)

### Correction System (`timer_correction_review.py`)

**Daily email (`--send`):** Sends each tech their previous day's timer entries with:
- Entry ID (12-char MD5 of full natural key)
- "Correct" link → Google Form with duration picker
- "Remove" link → Google Form for removal

**Entry ID formula:**
```
md5(project_did | user_email | start_time | site_name | site_id | task | end_time | duration_min)[:12]
```
Uses PostgreSQL `::text` timestamp format for consistency.

**Application (`--apply`):**
- Reads correction responses → `stg_timer_corrections`
- Reads removal responses → `stg_timer_entry_removals`
- Correction supersedes duplicate: if corrected entry belongs to unresolved duplicate group, auto-resolves it

**Tables:**

| Table | Purpose |
|-------|---------|
| `stg_timer_corrections` | entry_id → corrected_duration_min, corrected_end_time, reason |
| `stg_timer_entry_removals` | entry_id → removal reason, can be REVERTED |
| `stg_timer_duplicate_reviews` | group_id → status, entries JSONB, rejected_entries JSONB |

---

## 4. Timer Discrepancies & Corrections

### Source

- **Google Form:** Technicians submit timer error reports
- **Spreadsheet:** `12dBAGHlk-1qHB1p90bD5vnvQkiwkX2-TYhqwkEfEua4`
- **Access:** Google Drive API CSV export (Sheets API not enabled)

### Extraction (`extract_timer_discrepancies.py`)

1. **Incremental:** Fetches rows with `submission_timestamp > MAX(staging)`
2. **Duration parsing:** Handles free-text: "13 minutes", "2 hrs 50 mins", "0:00", plain integers, "less than 1 min" → 1
3. **Timestamps:** Google Forms records in Philippines timezone (UTC+8) → converted to UTC

### Storage

**Raw:** `data_raw.raw_timer_discrepancies` (JSONB)
**Staging:** `data_staging.stg_timer_discrepancies`

| Column | Type | Description |
|--------|------|-------------|
| submission_timestamp | TIMESTAMPTZ | When form was submitted (UTC) |
| email_address | TEXT | Google account email |
| ontel_email | TEXT | Typed @ontel.co email |
| shift_schedule | TEXT | Dayshift/Nightshift |
| discrepancy_date | DATE | When the error occurred |
| asset_name | TEXT | Full asset path (may include "TECH-OPS: TS18 \| ...") |
| task_name | TEXT | Task where error occurred |
| correct_duration_minutes | INT | What the duration should be (0 = remove) |
| description | TEXT | Free-text reason |
| row_number | INT | Spreadsheet row (PK for upsert) |

### Correction Flow

```
Tech submits discrepancy form
  → Stored in stg_timer_discrepancies
  → Reviewed manually (matched to timer entry in Excel)
  → Submitted to Correction/Removal Google Forms
  → timer_correction_review.py --apply reads responses
  → Written to stg_timer_corrections / stg_timer_entry_removals
  → rebuild_timer_clean() applies corrections
```

### Reason Classification

Discrepancy descriptions are auto-classified into form dropdown values:
- "Forgot to stop timer" — forgot to stop, left running, timer kept, etc.
- "Forgot to start timer" — forgot to start, missed to start, etc.
- "Ended early" — ended early, stopped early
- "Wrong duration logged" — wrong duration, swift error, duplicate timer, etc.
- "Manual Entry" — manual entry
- Default: "Wrong duration logged"

---

## 5. RawTimeData File vs Pipeline Comparison

### What is the RawTimeData File?

The master reporting Excel file (`RawTimeData_Combined_YYYYMMDD.xlsx`) used by the operations team for daily/gap reports. Contains clean timer data with all corrections and dedup already applied via Excel formulas.

**Key columns in RawTimeData tab:**
- Col 11: `Duration (min)_RAW` — original timer value
- Col 28: `cLookup_TimerDiscrepancies` — formula linking to TimeDiscrepancies tab
- Col 29: `Duration (min)_TimeDiscrep` — override from matched discrepancy
- Col 31: `Duration (min)` — **FINAL clean value** used in reports
- Col 25-26: `cDupCheckRef2`, `cDupCheck` — duplicate detection flags
- Col 39-40: `cDupTimer_ref`, `cDupTimer_Check` — timer-level duplicate detection

### Duplicate Handling Comparison

| Aspect | Our Pipeline | RawTimeData File |
|--------|-------------|-----------------|
| **Detection key** | `(project_did, user_email, start_time, site_name, site_id, task)` — requires exact same `start_time` | `SiteName + SiteID + Task + Date + UserName` — same **day**, not exact start_time |
| **Resolution** | Tech picks via Google Form; auto-resolve after 7 days → keep **longest duration** (updated 2026-03-28) | Automatic formula — keep entry with **longest duration**, zero the rest |
| **Scope** | Catches entries with identical start_time but different end_time/duration | Catches same person doing same task on same site on same day, even with different start times |
| **Coverage** | Narrower — misses duplicates with different start times | Broader — catches more duplicates |

**Gap:** The file's duplicate detection catches entries our system misses. For example, if a tech starts a timer at 9:00 AM, stops it, then accidentally starts again at 9:05 AM on the same site/task — our system wouldn't flag this (different start_time) but the file would (same day + site + task + user).

### Discrepancy Correction Comparison

| Aspect | Our Pipeline | RawTimeData File |
|--------|-------------|-----------------|
| **Matching** | entry_id hash → DB lookup → form submission | `cLookup_TimerDiscrepancies` formula → direct duration override |
| **Application** | `stg_timer_corrections` → `rebuild_timer_clean()` Step 3 UPDATE | `Duration (min)_TimeDiscrep` overrides in `Duration (min)` column |
| **Timing** | Corrections applied at rebuild time | Corrections baked into the clean duration column |

### Current Alignment Status (as of 2026-03-28)

**For Jan-Dec 2025 data:** Fully aligned. We imported the file's clean durations directly into `stg_timer_activities`, which means:
- Discrepancy corrections are baked in (duration already corrected)
- File-detected duplicates are excluded (dur=0 entries not imported)
- The `rebuild_timer_clean()` output matches the file's clean values

**For 2026+ data (ongoing pipeline):** Not fully aligned.
- Our duplicate detection is narrower than the file's
- Corrections flow through forms → DB → rebuild (different path than file's formula approach)
- The file may catch duplicates and apply corrections that our pipeline doesn't

### Recommendations for Full Alignment

1. **Broaden duplicate detection:** Change grouping key to `(user_email, DATE(start_time), site_name, site_id, task)` instead of requiring exact `start_time` match
2. ~~**Resolution logic:** Consider auto-keeping longest duration~~ — **Done (2026-03-28):** RPC + Python auto-resolve now both keep longest duration
3. **Periodic reconciliation:** Compare `stg_timer_activities_clean` against the latest RawTimeData file to catch drift

---

## 6. Asset Tasks Pipeline

### Source

- **API:** `GET /api/next/projects/{project_did}/assets/_export`
- **Pagination:** Keyset-based (after_ap, after_id) for efficient cursor navigation
- **Volume:** ~2.2M rows across 7 projects

### Extraction (`extract_asset_tasks.py`)

1. 6 parallel workers with per-project timeout (3600s + 5min buffer)
2. **Resume capability:** Tracks cursor in `pipeline.extraction_progress` table per `(run_id, project_did)`
3. **Pre-load optimization:** Drops non-PK indexes before bulk COPY, recreates after (600s timeout)
4. **Cleanup threshold:** New run must have >= 90% of old run's rows before deleting old data (prevents accidental data loss on partial extraction)
5. **COPY verification:** Parses "COPY N" return string to verify actual row count matches expected

### Transform (`transform.py`)

- **Server-side SQL:** All transformation in PostgreSQL (no Python round-trips)
- 2.2M rows in ~2-3 minutes (was 44 min with Python)
- **Date parsing:** Handles epoch-ms, epoch-s, and ISO date strings
- **Task name cleaning:** Regex removes prefix ("1. ", "10B. ") and suffix (" 123")

### Storage

**Raw:** `data_raw.raw_asset_tasks` (JSONB, ~2.2M rows, 2.4GB)
**Staging:** `data_staging.stg_asset_tasks` (structured, ~2.2M rows)
**Aggregated:** `data_staging.stg_assets` (one row per asset_did, ~30K rows)

### Deduplication

- Run-based: one run replaces previous run entirely
- Single-project recovery: `--project TS16` re-extracts within existing run

---

## 7. QA Forms Pipeline

### Source

- **API:** `GET /api/forms/{form_id}/requirement-responses` (CSV response)
- **Forms:** 6 different QA form tables in `config.QA_FORMS`

### Extraction (`extract_forms.py`)

- 6 parallel workers, one per form
- Queue-based: API CSV → parsed rows → queue → loader thread
- Batch size: 10,000 records per flush

### Transform

- **Server-side SQL:** UNION ALL across all 6 raw form tables
- Handles field name variations (e.g., "As-Builts (Other issues)" vs "AS-Builts")
- ~347K rows, ~30 min extraction + ~12 min transform

### Storage

**Raw:** `data_raw.raw_qa_form_*` (6 tables)
**Staging:** `data_staging.stg_qa_form` (unified, 40+ QA field columns)

### Deduplication

- Full refresh: all staging data cleared before each load

---

## 8. Calendar Events Pipeline

Restructured 2026-06-26 (was the "Calendar Leave" pipeline). One conformed table
holds ALL calendar event kinds (leave, holiday, birthday, training, other) with
an `event_kind` discriminator; per-kind serving views split them out.

### Source

- **Google Calendar API:** Shared calendar (leave / RD / holidays / birthdays / etc.)
- **Window:** `timeMin = 2024-01-01`, `timeMax = today + 12 months` (forward cap)
- **Incremental:** `updatedMin` watermark = `MAX(event 'updated')` in raw, minus a
  60s overlap. Keys off edit time, so edits to past-dated events are re-fetched.

### Extraction (`extract_calendar_events.py`)

1. Incremental by default via `updatedMin`; `--full-refresh` ignores the watermark
2. `--renormalize` re-runs normalization over existing rows (bulk, no re-fetch)

### Transform

- **Summary parsing:** validated deterministic fast-path (confidence gate 0.8) +
  Claude Haiku whole-row extraction on the low-confidence tail; persisted parse cache
- **Normalization:** `leave_type_normalized` (via `reference.ref_leave_code`),
  `team_normalized` (person-derived from `ref_employees.carrier_group`, label fallback),
  person match (deterministic + Haiku-cached in `agent.calendar_person_match`)
- **Date handling:** all-day end_date exclusive (subtract 1 day); same-day timed: days = 1

### Storage

**Raw:** `data_raw.raw_calendar_events` (JSONB, append)
**Staging:** `data_staging.stg_calendar_events` (event_id PK, ON CONFLICT upsert;
soft-delete via `is_deleted` + tombstone-on-cancel; reconcile sweeps drift)
**Serving views:** `analytics.v_calendar_leave` (+ `v_calendar_leave_daily`),
`v_calendar_holiday`, `v_calendar_birthday`, `v_calendar_training`, `v_calendar_other`

---

## 9. Gmail Package Scraper Pipeline

### Source

- **Gmail API:** Query `subject:{CG1 CG2 CG3 CG4 CG5 CG6 CG7 CG8 CG9}` (COP workflow emails)
- **Location:** Separate repo `gmail-scraper/`

### Extraction (`extractor.py`)

1. Incremental: fetches emails since `MAX(received_at) - 1 day`
2. Parses: message_id, thread_id, sender, recipients, subject, HTML body, labels

### Parsing (`parser.py`)

1. **HTML processing:** Removes hidden `font-size:0/1pt` spans (contain hash IDs)
2. **Package type detection:** Pattern matching on table headers:
   - POST MODIFICATION INSPECTION → PMI
   - LANDLORD CLOSE OUT → LL COP
   - CLOSE OUT PACKAGE REVIEW → REVIEW
   - CLOSE OUT PACKAGE REVISION → REVISION
   - 48 HOUR PACKAGE → 48HR REVIEW/REVISION
3. **Field extraction:** Label:value pairs from table rows → JSONB
4. **Dirty date handling:** Strips embedded spaces, handles 2-digit years, time-only values, placeholders

### Storage

**Raw:** `data_staging.stg_emails` (message_id PK, ON CONFLICT DO NOTHING)
**Parsed:** `data_staging.stg_package_emails` (JSONB fields, dropbox_url, swift_url)
**View:** `analytics.v_package_emails` (DISTINCT ON thread_id, flattens JSONB, ET timestamps)

---

## 10. Organizations & Projects Pipeline

### Extraction

- Sequential in Phase 1 (must complete before Phase 2)
- Extracts org metadata, project metadata with task counts

### Storage

**Raw:** `data_raw.raw_organizations`, `data_raw.raw_projects`
**Staging:** `data_staging.stg_organizations`, `data_staging.stg_projects`

### Deduplication

- ON CONFLICT upsert (reference data, updated each run)

---

## 11. User Priorities Pipeline

### Extraction

- Swift API task/priority data
- Runs in Phase 2 parallel with others

### Storage

**Staging:** `data_staging.stg_user_priorities` (full refresh each run)

---

## 12. Analytics Layer

### Schema: `analytics`

**Views:**
- `v_asset_tasks` (~2.2M rows) — Joined asset tasks with project/org info
- `v_timer_activities` (~285K rows) — Timer with ET timestamps
- `v_qa_forms` (~383K rows) — QA forms with ET timestamps
- `v_user_priorities` (~104K rows) — User priorities with ET timestamps
- `v_package_emails` — Deduplicated by thread_id, JSONB flattened, ET timestamps
- `v_timer_discrepancies` (~5K rows) — Discrepancies with ET timestamps

**Materialized Views:**
- `mv_project_summary` (~1,114 rows) — Project-level aggregates
- `mv_technician_stats` (~40 rows) — Per-technician metrics
- `mv_daily_completion` (~395K rows) — Daily task completion counts

**Refresh:** RPC `analytics.refresh_one_mv(p_view_name)` — called after backfill in pipeline

---

## 13. Database Architecture

### Schemas

| Schema | Purpose |
|--------|---------|
| `data_raw` | Raw JSONB as received from APIs |
| `data_staging` | Parsed, transformed, ready for analytics |
| `pipeline` | Run tracking metadata (pipeline_runs, extraction_progress) |
| `analytics` | Pre-joined views + materialized views |
| `agent` | AI agent metadata (schema_metadata table) |

### Connection Details

- **Cloud:** `db.voqfjfngdpcvevbkikud.supabase.co:5432` (IPv6 only)
- **Pooler (GHA):** `aws-0-ap-southeast-1.pooler.supabase.com:5432` (IPv4, session mode)
- **Driver:** asyncpg with sync bridge via `run_coroutine_threadsafe()`
- **Pool:** min=4, max=20 connections
- **Statement timeout:** 300s (overrides Supabase default 120s)

### Retry Logic (`db.py`)

- Exponential backoff: 2^attempt seconds, capped at 15s, default 5 retries
- Auto-reconnect on: `ConnectionDoesNotExistError`, `InterfaceError`, `OSError`
- Pool recreation retries 3x with 5s/10s backoff on startup

---

## 14. Scheduling & Automation

### GitHub Actions

| Workflow | Schedule | Pipeline |
|----------|----------|----------|
| `pipeline.yml` Cron 1 | 10:00 PM EST | orgs/projects only |
| `pipeline.yml` Cron 2 | 12:01 AM EST | timer, user_priorities, forms |
| `gmail-pipeline.yml` | On email (Apps Script) | aging + sales |
| `timer-duplicate-resolve.yml` | On form submit | duplicate resolution |
| `timer-correction-apply.yml` | Batched: 10 min after last form submit, 45 min cap, 5-min poller | correction + removal application (one rebuild per run) |
| `scrape.yml` (gmail-scraper) | 4:00 AM UTC | package email scraper |

### Windows Task Scheduler

| Task | Schedule | Pipeline |
|------|----------|----------|
| SwiftPipeline-Nightly | Daily 12:01 AM | asset_tasks → backfill → analytics → discrepancies → exports |
| SwiftPipeline-Calendar | Disabled (migrated to GHA, 5 AM & 5 PM ET) | calendar events sync |

---

## 15. Historical Data Import (2026-03-28)

### Context

The Swift API timer extraction had a ~1K row cap bug that silently truncated results for May-Sep 2025. The daily chunking fix (2026-02-27) resolved this for future extractions, but the historical data was permanently lost from the API.

### Solution

Imported the operations team's RawTimeData report file, which contained the complete reviewed data.

### What Was Imported

| Source | Destination | Rows | Duration Column Used |
|--------|-------------|------|---------------------|
| RawTimeData_Combined_20260120.xlsx (RawTimeData tab) | raw_timer_activities_historical + stg_timer_activities | 120,009 | Duration (min) — clean/corrected |
| Timer Discrepancies_modified_20260327.xlsx (manual entries) | raw_timer_activities_historical + stg_timer_activities | 116 | dur_manual |

### Scripts

- `_import_rawtimedata.py` — Bulk import from RawTimeData file
- `_import_manual_entries.py` — Import manual timer entries (synthetic start_time at 09:00 ET)

### Impact

- Jan-Dec 2025 data in DB now aligned with RawTimeData report file
- May-Sep 2025 gap fully resolved (236-693 rows/month → 9,500-12,000)
- Clean table rebuilt: 339,312 rows

### Implications

For Jan-Dec 2025, the staging table contains **clean durations** (corrections already applied, duplicates excluded). This means:
- `rebuild_timer_clean()` copies these directly — no additional corrections needed
- Existing correction/removal records in `stg_timer_corrections` / `stg_timer_entry_removals` are orphaned for 2025 data (they target original durations that no longer exist in staging)
- For 2026+ data, the normal pipeline flow continues (raw durations → corrections via forms → rebuild)

---

## Appendix: Deduplication Strategy Summary

| Pipeline | Dedup Method | Key |
|----------|-------------|-----|
| Timer Activities | Monthly delete-reload + duplicate review system | `(project_did, user_email, start_time)` for monthly; `(project_did, user_email, start_time, site_name, site_id, task)` for duplicates |
| Asset Tasks | Run-based replacement | One run replaces previous entirely |
| QA Forms | Full refresh | TRUNCATE + INSERT each run |
| Organizations/Projects | ON CONFLICT upsert | org_did / project_did |
| Calendar Events | ON CONFLICT upsert | event_id |
| Timer Discrepancies | ON CONFLICT upsert | row_number (spreadsheet row) |
| Gmail Emails | ON CONFLICT DO NOTHING | message_id |
| Package Emails | Re-parse from stg_emails | message_id |

---

*Last updated: 2026-03-28*
