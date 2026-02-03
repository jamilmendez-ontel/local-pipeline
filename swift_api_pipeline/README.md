# Swift API to Supabase Pipeline

Production ETL pipeline that extracts data from Swift Projects API and loads it into Supabase as raw JSONB.

## Architecture

**Raw JSONB Layer (Medallion Bronze)**
- Full API response preservation
- Historical tracking via run_id
- Full refresh strategy
- Foundation for downstream transformation

## Setup

### 1. Prerequisites
- Python 3.9+
- Supabase project
- Swift Projects API credentials

### 2. Installation

```bash
# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

```bash
# Copy environment template
copy .env.example .env

# Edit .env with your credentials
```

Required environment variables:
- `SWIFT_EMAIL` - Swift Projects login email
- `SWIFT_PASSWORD` - Swift Projects password
- `SUPABASE_URL` - Your Supabase project URL
- `SUPABASE_SERVICE_KEY` - Service role key (not anon key)

### 4. Database Setup

Execute the migration in Supabase SQL Editor:
```sql
-- Copy and run migrations/001_raw_tables.sql
```

This creates:
- `raw_user_priorities` - User priority/task data
- `raw_organizations` - Organization data
- `raw_projects` - Project data with metrics
- `pipeline_runs` - Execution metadata

## Usage

### Run Pipeline

```bash
python pipeline.py
```

### Query Latest Data

```sql
-- Get latest successful run ID
SELECT run_id
FROM pipeline_runs
WHERE status = 'success'
ORDER BY started_at DESC
LIMIT 1;

-- Query latest user priorities
SELECT
    jsonb_array_elements(data) as priority
FROM raw_user_priorities
WHERE run_id = 'your-run-id-here'
ORDER BY page_number, id;

-- Query latest organizations
SELECT
    jsonb_array_elements(data) as org
FROM raw_organizations
WHERE run_id = 'your-run-id-here';

-- Query latest projects with organization context
SELECT
    jsonb_array_elements(data) as project
FROM raw_projects
WHERE run_id = 'your-run-id-here';
```

## Monitoring

Check pipeline execution status:

```sql
SELECT
    run_id,
    pipeline_name,
    status,
    started_at,
    completed_at,
    records_extracted,
    error_message,
    EXTRACT(EPOCH FROM (completed_at - started_at)) as duration_seconds
FROM pipeline_runs
ORDER BY started_at DESC
LIMIT 10;
```

## Troubleshooting

### Authentication Failures
- Verify credentials in .env
- Check if password contains special characters (may need escaping)
- Ensure API access is not blocked

### Supabase Connection Issues
- Verify SUPABASE_SERVICE_KEY (not anon key)
- Check IP allowlist in Supabase settings
- Confirm URL format: `https://xxx.supabase.co`

### Rate Limiting
- Pipeline includes exponential backoff
- Reduce PAGE_SIZE in config.py if needed
- Add delays between organization/project calls
