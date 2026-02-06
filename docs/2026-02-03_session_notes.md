# Session Notes - February 3-4, 2026

## Overview
Extended the Swift API data pipeline with two new datasets (QA Forms and Timer Activities), reorganized tables into PostgreSQL schemas, and standardized timezone handling.

---

## 1. QA Forms Pipeline

### What was built
- **Extract script**: `swift_api_pipeline/extract_forms.py`
- **Discovery script**: `swift_api_pipeline/discover_forms.py` (Playwright-based form ID discovery)
- **Migrations**:
  - `004_forms_tables.sql` - Raw tables for TS13-TS17
  - `005_forms_ts18.sql` - Raw table for TS18
  - `006_stg_qa_form_all_columns.sql` - Added all 81 columns

### Form IDs (QA Forms TS13-TS18)
```python
QA_FORMS = {
    "qa_ts13": {"form_id": "-NH1hUPkaKtPdd7BK9cb", "table_name": "raw_form_qa_ts13"},
    "qa_ts14": {"form_id": "-NXCg4vTDNVykN8ioMYp", "table_name": "raw_form_qa_ts14"},
    "qa_ts15": {"form_id": "-Np6o9OCL4RWIJq68HJe", "table_name": "raw_form_qa_ts15"},
    "qa_ts16": {"form_id": "-O9ACLN3je1w7oEoG5hY", "table_name": "raw_form_qa_ts16"},
    "qa_ts17": {"form_id": "-ONMD-cGBq-_3r9ybaAq", "table_name": "raw_form_qa_ts17"},
    "qa_ts18": {"form_id": "-O_J2hPlryTezP9RhujA", "table_name": "raw_form_qa_ts18"},
}
```

### Data Loaded
| Form | Records |
|------|---------|
| QA Form TS13 | 69,232 |
| QA Form TS14 | 51,921 |
| QA Form TS15 | 52,025 |
| QA Form TS16 | 57,799 |
| QA Form TS17 | 63,875 |
| QA Form TS18 | 48,268 |
| **Total** | **343,120** |

### Key Features
- Parallel extraction with ThreadPoolExecutor (3 workers)
- CSV response parsing (Forms API returns CSV, not JSON)
- 81 columns capturing ALL form fields including:
  - Construction Manager, Subcontractor
  - RCM approval, Completeness of files
  - Sector photos, Powershift photos
  - RET values, RET visibility
  - Serials, Smart level, Calibration details
  - And many more...
- Handles field name case variations (e.g., "As-Builts (Other issues)" vs "AS-Builts (Other issues)")

### Usage
```bash
# Extract all QA forms
python extract_forms.py --workers 3

# Transform to staging
python transform.py qa_forms [run_id]
```

---

## 2. Timer Activities Pipeline

### What was built
- **Extract script**: `swift_api_pipeline/extract_timer.py`
- **Migration**: `007_timer_tables.sql`
- **Transform function**: Added to `transform.py`

### Project IDs (from ref_ontel_techops_projects)
| Project | DID |
|---------|-----|
| TS13 | -NFkG865XjMXlwqZ1AqU |
| TS14 | -NV5j_QcTmdwoaGklFvf |
| TS15 | -Np5nDzlfJrK_nt5Ro7e |
| TS16 | -O99xSQdLiGywc6KRVw- |
| TS17 | -ONLJdAstPfeGwVNgpYH |
| TS18 | -O_IpQNpLVwhdVC3QYIm |

### Date Range Logic
```python
# If today is NOT the 1st of month:
#   start_date = 1st of current month
#   end_date = yesterday

# If today IS the 1st of month:
#   start_date = 1st of previous month
#   end_date = last day of previous month
```

### Data Loaded
| Project | Records | Date Range |
|---------|---------|------------|
| TS15 | 41 | Jan 6-27 |
| TS16 | 86 | Jan 2 - Feb 2 |
| TS17 | 1,048 | Jan 2 - Feb 3 |
| TS18 | 10,508 | Jan 6 - Feb 3 |
| **Total** | **11,683** | |

### Key Features
- **Appends data** (never replaces) - each run adds new records
- Tracks `run_date`, `start_date`, `end_date` for each extraction
- Parallel extraction for all projects TS13-TS18
- Foreign key to `stg_projects` via `project_did`

### Usage
```bash
# Auto date calculation
python extract_timer.py

# Manual date range
python extract_timer.py --start-date 2026-01-01 --end-date 2026-01-31

# Transform to staging
python transform.py timer [run_id]
```

---

## 3. Schema Reorganization

### What was built
- **Migration**: `008_create_schemas_v2.sql`
- **Config update**: Added schema constants to `config.py`
- **Updated all Python files** to use schema-qualified table names

### Schema Structure
```
data_raw          (raw JSONB tables)
├── raw_organizations
├── raw_projects
├── raw_asset_tasks
├── raw_user_priorities
├── raw_form_qa_ts13
├── raw_form_qa_ts14
├── raw_form_qa_ts15
├── raw_form_qa_ts16
├── raw_form_qa_ts17
├── raw_form_qa_ts18
└── raw_timer_activities

data_staging      (transformed tables)
├── stg_organizations
├── stg_projects
├── stg_asset_tasks
├── stg_user_priorities
├── stg_qa_form
└── stg_timer_activities

reference         (lookup tables)
└── ref_ontel_techops_projects

pipeline          (tracking tables)
└── pipeline_runs
```

### Schema Constants (config.py)
```python
SCHEMA_RAW = "data_raw"
SCHEMA_STAGING = "data_staging"
SCHEMA_REFERENCE = "reference"
SCHEMA_PIPELINE = "pipeline"
```

### Usage in Python
```python
# Before (public schema)
client.table("raw_asset_tasks").insert(rows).execute()

# After (schema-qualified)
client.schema(SCHEMA_RAW).table("raw_asset_tasks").insert(rows).execute()
```

### Why data_raw/data_staging instead of raw/staging?
The `raw` and `staging` schemas already existed and were owned by `supabase_admin`, not `postgres`. To avoid permission issues, we created new schemas (`data_raw`, `data_staging`) that `postgres` can own and manage.

### Migration Notes
Run `008_create_schemas_v2.sql` in Supabase SQL editor to:
1. Create `data_raw` and `data_staging` schemas
2. Move all raw_* tables to `data_raw`
3. Move all stg_* tables to `data_staging`
4. Grant appropriate permissions to anon, authenticated, service_role

---

## 4. Timezone Standardization

### Problem
Date conversions in `transform.py` were using `datetime.fromtimestamp()` without specifying a timezone, which meant dates would be converted using the local system timezone. If the pipeline ran on a system not in America/New_York, dates could be off by hours (potentially shifting to the wrong day).

### Solution
All datetime conversions now explicitly use America/New_York timezone:

```python
from zoneinfo import ZoneInfo

TZ_ET = ZoneInfo("America/New_York")

def epoch_to_datetime(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO datetime string in America/New_York timezone"""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=TZ_ET).isoformat()
```

### What was updated
| Transform | Change |
|-----------|--------|
| Organizations | `epoch_to_datetime()` for date_created, last_updated |
| Projects | `epoch_to_datetime()` for date_created, last_updated, metrics_last_updated |
| User Priorities | `parse_date()` converts UTC to ET with `astimezone(TZ_ET)` |
| Asset Tasks | `parse_date()` uses `tz=TZ_ET` for epoch conversions |

### Timezone Summary
| Component | Timezone |
|-----------|----------|
| API Requests | America/New_York (requested via `tz` param) |
| Raw Data | America/New_York (as received from API) |
| Staging Data | America/New_York (explicitly converted) |
| Pipeline Metadata | UTC (started_at, completed_at) |

---

## 5. CSV Parsing Fix for QA Forms

### Problem
The Swift API returns CSV headers only on the **first page** of paginated responses. Subsequent pages have no header row, causing `csv.DictReader` to use the first data row as headers, corrupting all following records.

### Impact
- ~2,000 records per form: correct (first page only)
- ~50,000-67,000 records per form: corrupted (pages 2+)
- **97% of QA form data was corrupted** with swapped keys/values

### Example of Corrupted Record
```json
// Bad - values used as keys!
{"TECH-OPS: TS12": "TECH-OPS: TS11", "pending": "pending", ...}

// Good - proper structure
{"Project": "TECH-OPS: TS12", "Requirement Status": "pending", ...}
```

### Fix
Save CSV fieldnames from the first page and reuse them for subsequent pages:

```python
csv_fieldnames = None  # Store headers from first page

# First page - let DictReader detect headers
if csv_fieldnames is None:
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    csv_fieldnames = reader.fieldnames
else:
    # Subsequent pages - use saved fieldnames
    reader = csv.DictReader(io.StringIO(resp.text), fieldnames=csv_fieldnames)
    rows = list(reader)
```

### Results After Fix
| Project | Total | Has Project | Has Crew Lead | Has AAT |
|---------|-------|-------------|---------------|---------|
| TS13 | 49,792 | 49,792 (100%) | 3,914 | 6,929 |
| TS14 | 51,864 | 51,864 (100%) | 3,648 | 6,054 |
| TS15 | 52,059 | 52,059 (100%) | 3,969 | 7,464 |
| TS16 | 57,490 | 57,490 (100%) | 2,586 | 6,496 |
| TS17 | 65,415 | 65,415 (100%) | 2,099 | 7,221 |
| TS18 | 47,914 | 47,914 (100%) | 1,331 | 4,692 |

**Note**: Sparse fields (crew_lead, AAT, etc.) are naturally sparse - not all QA reviews fill all fields.

### Additional Fix: PostgREST Schema Exposure
The new schemas needed to be exposed to PostgREST:
```sql
ALTER ROLE authenticator SET pgrst.db_schemas TO
    'public, graphql_public, data_raw, data_staging, reference, pipeline';
NOTIFY pgrst, 'reload config';
```

---

## 6. Data Dictionary for AI Agent

Created `docs/data_dictionary.md` to support integration with a Data Analyst AI Agent project.

### Contents
- **Business Context**: Telecom construction project overview
- **Table Documentation**: All 5 staging tables with column descriptions
- **Example Queries**: Common analysis patterns (project progress, technician productivity, QA issues, time analysis)
- **Relationships**: How tables connect via foreign keys
- **AI Agent Notes**: Schema prefixes, performance tips, NULL handling

### Purpose
Allows an AI Agent to understand the data structure and write accurate SQL queries to answer user questions about:
- Project completion status
- Technician performance
- QA pass/fail rates
- Time tracking analysis
- Site-level metrics

---

## 7. Asset Task Requirements Pipeline (Feb 4)

### What was built
- **Extract script**: `swift_api_pipeline/extract_requirements.py`
- **Migrations**:
  - `009_requirements_tables.sql` - Raw and staging tables for requirements
  - `010_assets_table.sql` - Separate assets table for proper data hierarchy

### API Endpoint
```
/api/asset-tasks/{task_DID}/requirements
```

### Data Model (Hierarchical)
```
stg_projects (6 projects)
  └── stg_assets (19,862 sites)
        └── stg_asset_tasks (2.2M tasks)
              └── stg_asset_task_requirements (millions)
```

### Key Features
- **50 workers** (optimal based on benchmarking at 16.5 tasks/s)
- ThreadPoolExecutor for parallel API calls
- Fetches requirements per task (no bulk endpoint available)
- Queue-based streaming to database
- Can process specific projects: `--projects 17,18`

### Performance Benchmark
| Workers | Tasks/sec | Notes |
|---------|-----------|-------|
| 10 | 6.0 | Baseline |
| 20 | 11.5 | Good |
| 30 | 15.1 | Better |
| **50** | **16.5** | **Optimal** |
| 75 | 15.3 | Rate limiting |
| 100 | 14.8 | Rate limiting |

### Usage
```bash
# Extract requirements for all projects
python extract_requirements.py --workers 50

# Extract for specific projects only
python extract_requirements.py --projects 17,18

# Transform to staging
python transform.py requirements [run_id]
```

### Time Estimate
With ~2.2M tasks and 16.5 tasks/s:
- Per project (~350K tasks): ~6 hours
- All 6 projects: ~37 hours

Recommendation: Run per-project overnight.

---

## 8. Assets Table (Feb 4)

### What was built
- **Migration**: `010_assets_table.sql`
- **Transform function**: Added `transform_assets()` to `transform.py`

### Purpose
Separate asset-level data from task-level data for proper data hierarchy. Assets are extracted from the existing `raw_asset_tasks` bulk export.

### Schema
```sql
stg_assets (
  project_did, asset_did,
  asset_id, asset_name,
  task_count, requirement_count,
  tasks_pending, tasks_in_progress,
  tasks_submitted, tasks_approved,
  tasks_rejected, tasks_cancelled
)
```

### Data Loaded
| Project | Assets | Total Tasks |
|---------|--------|-------------|
| TS13 | 3,215 | 344,172 |
| TS14 | 3,244 | 362,634 |
| TS15 | 3,334 | 375,610 |
| TS16 | 3,681 | 408,212 |
| TS17 | 3,513 | 389,619 |
| TS18 | 2,875 | 321,788 |
| **Total** | **19,862** | **2,202,035** |

### Usage
```bash
# Transform assets from existing raw_asset_tasks
python transform.py assets [run_id]
```

---

## 9. Updated Data Model

```
data_staging.stg_organizations (300 rows)
    └── data_staging.stg_projects (1,108 rows) [org_did]
            ├── data_staging.stg_assets (19,862 rows) [project_did]
            │       └── data_staging.stg_asset_tasks (2,202,035 rows) [asset_did]
            │               └── data_staging.stg_asset_task_requirements (TBD) [task_did]
            ├── data_staging.stg_qa_form (344,094 rows) [project_number]
            └── data_staging.stg_timer_activities (11,683 rows) [project_did]
```

### Total Records: 2,579,082 (excluding requirements)

---

## 10. Git Commits

```
c4bba19 Fix CSV parsing for paginated form responses
9b5a1e7 Update session notes with data dictionary section
3085687 Add data dictionary for AI Agent integration
71447f6 Update docs with timezone standardization details
d5b6ce9 Fix timezone handling - use America/New_York consistently
1295695 Update session docs with schema reorganization details
4c8d5c0 Reorganize tables into schemas (data_raw, data_staging, reference, pipeline)
ca847c4 Add session documentation for Feb 3, 2026
b0e0345 Add Timer Activities pipeline for TS13-TS18
8fc733c Add QA Forms pipeline for TS13-TS18
614049b Update Claude local settings
63ee85e Add Swift API data pipeline for local Supabase
```

All commits pushed to: `https://github.com/jamilmendez-ontel/local-pipeline.git`

---

## 11. Files Created/Modified

### New Files (Feb 3-4)
- `swift_api_pipeline/extract_forms.py`
- `swift_api_pipeline/extract_timer.py`
- `swift_api_pipeline/extract_requirements.py` - Requirements extraction with 50 workers
- `swift_api_pipeline/discover_forms.py`
- `swift_api_pipeline/migrations/004_forms_tables.sql`
- `swift_api_pipeline/migrations/005_forms_ts18.sql`
- `swift_api_pipeline/migrations/006_stg_qa_form_all_columns.sql`
- `swift_api_pipeline/migrations/007_timer_tables.sql`
- `swift_api_pipeline/migrations/008_create_schemas_v2.sql`
- `swift_api_pipeline/migrations/009_requirements_tables.sql` - Raw and staging tables for requirements
- `swift_api_pipeline/migrations/010_assets_table.sql` - Separate assets table
- `docs/data_dictionary.md` - Comprehensive data documentation for AI Agent integration

### Modified Files
- `swift_api_pipeline/config.py` - Added schema constants
- `swift_api_pipeline/extract_asset_tasks.py` - Schema-qualified table names
- `swift_api_pipeline/extract_forms.py` - Schema-qualified table names, CSV pagination fix
- `swift_api_pipeline/extract_timer.py` - Schema-qualified table names
- `swift_api_pipeline/load.py` - Schema-qualified table names
- `swift_api_pipeline/transform.py` - Added assets/QA forms/timer/requirements transforms, timezone fix

---

## 12. Known Issues / TODO

1. **TS18 Timer January Data Incomplete**: Extraction stopped at ~10,000 rows due to API 500 errors. Can retry later.

2. **Form Discovery Script**: Works but requires manual navigation in browser to correct organization. Could be improved.

3. **Future Forms**: When new TS projects are created (TS19, etc.), need to:
   - Get the form ID from Swift Projects UI (Edit form → URL contains ID)
   - Add to `QA_FORMS` config in `extract_forms.py` and `transform.py`
   - Create migration for new `raw_form_qa_tsXX` table

4. ~~**Run Schema Migration**~~: ✅ Completed - tables moved to new schemas.

---

## 13. Quick Reference Commands

```bash
# Activate virtual environment
cd swift_api_pipeline
.\venv\Scripts\activate  # Windows

# Run extractions
python extract_asset_tasks.py
python extract_forms.py
python extract_timer.py
python extract_requirements.py --projects 18 --workers 50  # Start with one project

# Run transformations
python transform.py assets [run_id]       # NEW: Extract assets from bulk data
python transform.py asset_tasks [run_id]
python transform.py qa_forms [run_id]
python transform.py timer [run_id]
python transform.py requirements [run_id]  # NEW: Transform requirements

# Check data in Supabase
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "SELECT COUNT(*) FROM data_staging.stg_assets;"
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "SELECT COUNT(*) FROM data_staging.stg_asset_task_requirements;"

# List tables in each schema
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "\dt data_raw.*"
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "\dt data_staging.*"
```

---

## 14. Environment

- **Local Supabase**: Docker container `supabase_db_supabase-local`
- **Python**: 3.x with venv in `swift_api_pipeline/venv`
- **Key packages**: requests, supabase, python-dateutil, playwright
