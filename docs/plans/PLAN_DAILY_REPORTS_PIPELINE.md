# Plan: Daily Reports Pipeline

## Overview

Build a **separate pipeline** to extract employee daily report data from Swift's "TECH-OPS: Daily Reports" project. This is management-level data (attendance, hours worked, work summaries) — separate from the production timer/asset data pipelines.

## Data Source

**Swift Project**: TECH-OPS: Daily Reports (dynamically discovered)
- Multiple projects may exist (e.g., one per year period)
- Step 1 always queries `stg_projects` for `project_name = 'TECH-OPS: Daily Reports%'` excluding `x_Archive%`
- Currently: 1 active project (`-OhBj1jz53pG6SWzlnvy`, 47,318 tasks, created Dec 2025)

**Historical (pinned for later):**
- `x_Archive: TECH-OPS: Daily Reports - 2016 to 2018` (5,270 tasks)
- `x_Archive: TECH-OPS: Daily Reports - 2019` (837 tasks)
- `x_Archive: TECH-OPS: Daily Reports - 2020` (18,585 tasks)
- `x_Archive: TECH-OPS: Daily Reports - 2021` (20,370 tasks)
- `x_Archive: TECH-OPS: Daily Reports - 2022` (45,257 tasks)
- `x_Archive: TECH-OPS: Daily Reports` x3 (2023, 2024, partial 2025 — 43K, 37K, 71K tasks)
- Total archive: ~242K tasks. Same structure, use after current pipeline is verified.

- **Structure**:
  ```
  Project: TECH-OPS: Daily Reports
    └── Asset (employee): "220501" (emp_id as asset name)
         └── Task (date): "45787" (Excel serial date number)
              ├── Requirement:
              │    Title: "9" (hours worked)
              │    Description: "Worked on Site A Final COP..." (work details)
              └── Timer:
                   start_time, end_time, duration (clock-in/out)
  ```

## What We're Extracting

| Level | API | Data | Volume |
|-------|-----|------|--------|
| Assets (employees) | `/api/next/projects/{DID}/assets/_export` | Employee ID, name, asset_did | ~140 active |
| Tasks (dates) | Same export | Date (serial), status | ~47K total, ~140/day |
| Requirements (hours+work) | `/api/asset-tasks/{task_DID}/requirements` | Hours (title), work description | 1 per task |
| Timer (clock in/out) | `/api/timer-activities/_report` | Start, end, duration | 1-2 per task |

## Pipeline Architecture (Separate from main pipeline)

```
extract_daily_reports.py (new, standalone)
  ├── Step 1: Asset tasks export (bulk) → employees + dates
  ├── Step 2: Timer extract for Daily Reports project only
  └── Step 3: Requirements for new/changed tasks (incremental)
       ↓
transform_daily_reports.py
  ├── Parse serial date → actual date
  ├── Parse hours title → numeric
  ├── Compute target_daily
  └── Output to Excel for verification → then to Supabase
```

**IMPORTANT**: This is a SEPARATE pipeline. Does NOT modify any existing pipeline scripts.

## Table Structure

### `stg_employees` — Employee Reference

| Column | Type | Source | Description |
|--------|------|--------|-------------|
| emp_id | TEXT PK | Asset name | Employee ID (e.g., "220501") |
| employee_name | TEXT | Ref tab / asset metadata | Full name |
| asset_did | TEXT | Swift asset DID | For API lookups |
| email | TEXT | Ref tab | @ontel.co email |
| role | TEXT | Ref tab | Ops / Support |
| role2 | TEXT | Ref tab | TA / TAS / PAS / Support |
| cluster | TEXT | Ref tab | Alpha-Zeta / Support |
| hire_date | DATE | Ref tab | |
| is_active | BOOLEAN | Ref tab | |
| resignation_date | DATE | Ref tab | NULL if active |

### `stg_daily_reports` — Daily Work Reports

| Column | Type | Source | Description |
|--------|------|--------|-------------|
| emp_id | TEXT FK | Asset name | Links to stg_employees |
| work_date | DATE | Task name (parsed serial) | Actual date |
| task_did | TEXT | Swift task DID | For incremental extraction |
| task_status | TEXT | Task status | pending/approved/etc. |
| hours_worked | NUMERIC | Requirement title (parsed) | Reported hours |
| work_description | TEXT | Requirement description | What they did that day |
| target_daily | NUMERIC | Computed | Revenue target |
| UNIQUE | | (emp_id, work_date) | |

### `stg_daily_report_timers` — Clock In/Out

| Column | Type | Source | Description |
|--------|------|--------|-------------|
| emp_id | TEXT FK | Derived from user_email | |
| work_date | DATE | From start_time | |
| start_time | TIMESTAMPTZ | Timer start | Clock-in |
| end_time | TIMESTAMPTZ | Timer end | Clock-out |
| duration_min | NUMERIC | Timer duration | Shift minutes |

### `v_daily_reports` — Combined Analytics View

Joins all three tables for complete daily picture per employee.

## Target Daily Formula

```
Rate: $2.25 per hour
If hours_worked > 4: target = (hours - 1) * 2.25  (1 hr deducted for lunch/admin)
If hours_worked <= 4: target = hours * 2.25
If hours_worked = 0: target = 0
```

## Execution Plan

### Phase 1: Verify Data (Current Step)
1. Extract sample data from Swift API for Daily Reports project
2. Build Excel file with proposed table structure + actual data
3. User verifies structure and data quality before proceeding

### Phase 2: Build Pipeline
1. Create SQL migration for new tables (separate schema or data_staging)
2. Build extract_daily_reports.py (standalone)
3. Build transform
4. Initial full load (~47K tasks)
5. Test incremental (daily new tasks only)

### Phase 3: Automate
1. Add to GHA workflow (separate from main pipeline)
2. Schedule nightly
3. Build analytics view

### Phase 4: Historical (After Phase 1-3 verified)
1. Extract archive projects (2016-2022) using same structure
2. Structure historical data to match current tables

## Decisions Made

- Separate pipeline — does NOT touch existing scripts
- Separate tables — management-level, not production-level
- Excel verification first before Supabase
- Current active project first, archives later
- $2.25 rate — TBD if configurable or hard-coded (pending answer)

## Open Questions

1. Should these tables go in `data_staging` or a new schema (e.g., `management`)?
2. The $2.25 rate — hard-code or config value?
3. How will the pipeline be triggered — same GHA, separate workflow, or local scheduler?
