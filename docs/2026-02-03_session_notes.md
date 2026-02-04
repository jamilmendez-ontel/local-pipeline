# Session Notes - February 3-4, 2026

## Overview
Extended the Swift API data pipeline with two new datasets (QA Forms and Timer Activities) and reorganized tables into PostgreSQL schemas.

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

## 4. Data Model

```
data_staging.stg_organizations (300 rows)
    └── data_staging.stg_projects (1,108 rows) [org_did]
            ├── data_staging.stg_asset_tasks (2,202,035 rows) [project_did]
            ├── data_staging.stg_qa_form (343,120 rows) [project_number]
            └── data_staging.stg_timer_activities (11,683 rows) [project_did, FK]
```

### Total Records: 2,558,246

---

## 5. Git Commits

```
4c8d5c0 Reorganize tables into schemas (data_raw, data_staging, reference, pipeline)
ca847c4 Add session documentation for Feb 3, 2026
b0e0345 Add Timer Activities pipeline for TS13-TS18
8fc733c Add QA Forms pipeline for TS13-TS18
614049b Update Claude local settings
63ee85e Add Swift API data pipeline for local Supabase
```

All commits pushed to: `https://github.com/jamilmendez-ontel/local-pipeline.git`

---

## 6. Files Created/Modified

### New Files
- `swift_api_pipeline/extract_forms.py`
- `swift_api_pipeline/extract_timer.py`
- `swift_api_pipeline/discover_forms.py`
- `swift_api_pipeline/migrations/004_forms_tables.sql`
- `swift_api_pipeline/migrations/005_forms_ts18.sql`
- `swift_api_pipeline/migrations/006_stg_qa_form_all_columns.sql`
- `swift_api_pipeline/migrations/007_timer_tables.sql`
- `swift_api_pipeline/migrations/008_create_schemas_v2.sql`

### Modified Files
- `swift_api_pipeline/config.py` - Added schema constants
- `swift_api_pipeline/extract_asset_tasks.py` - Schema-qualified table names
- `swift_api_pipeline/extract_forms.py` - Schema-qualified table names
- `swift_api_pipeline/extract_timer.py` - Schema-qualified table names
- `swift_api_pipeline/load.py` - Schema-qualified table names
- `swift_api_pipeline/transform.py` - Added QA forms/timer transforms + schema-qualified table names

---

## 7. Known Issues / TODO

1. **TS18 Timer January Data Incomplete**: Extraction stopped at ~10,000 rows due to API 500 errors. Can retry later.

2. **Form Discovery Script**: Works but requires manual navigation in browser to correct organization. Could be improved.

3. **Future Forms**: When new TS projects are created (TS19, etc.), need to:
   - Get the form ID from Swift Projects UI (Edit form → URL contains ID)
   - Add to `QA_FORMS` config in `extract_forms.py` and `transform.py`
   - Create migration for new `raw_form_qa_tsXX` table

4. **Run Schema Migration**: Need to run `008_create_schemas_v2.sql` in Supabase SQL editor to move tables to new schemas.

---

## 8. Quick Reference Commands

```bash
# Activate virtual environment
cd swift_api_pipeline
.\venv\Scripts\activate  # Windows

# Run extractions
python extract_asset_tasks.py
python extract_forms.py
python extract_timer.py

# Run transformations
python transform.py asset_tasks [run_id]
python transform.py qa_forms [run_id]
python transform.py timer [run_id]

# Check data in Supabase (after schema migration)
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "SELECT COUNT(*) FROM data_staging.stg_qa_form;"

# List tables in each schema
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "\dt data_raw.*"
docker exec -i supabase_db_supabase-local psql -U postgres -d postgres -c "\dt data_staging.*"
```

---

## 9. Environment

- **Local Supabase**: Docker container `supabase_db_supabase-local`
- **Python**: 3.x with venv in `swift_api_pipeline/venv`
- **Key packages**: requests, supabase, python-dateutil, playwright
