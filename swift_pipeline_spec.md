# Swift API to Supabase Pipeline - Production Specification

## Project Overview

Build a production-grade ETL pipeline that extracts data from Swift Projects API and loads it into Supabase as raw JSONB. This is a medallion-style architecture with a raw data layer that preserves full API response fidelity.

## Architecture Design

**Approach: Raw JSONB staging layer with full refresh**

Benefits:
- Complete audit trail of API responses
- Flexibility to re-process without re-fetching
- Simplified initial ETL (extract-load, transform later)
- Easy schema evolution as API changes
- Full historical tracking via run_id

## Project Structure

```
swift_api_pipeline/
├── config.py
├── extract.py
├── load.py
├── pipeline.py
├── requirements.txt
├── .env.example
├── README.md
└── migrations/
    └── 001_raw_tables.sql
```

## 1. Database Schema (migrations/001_raw_tables.sql)

```sql
-- migrations/001_raw_tables.sql

-- Raw API responses stored as JSONB
CREATE TABLE IF NOT EXISTS raw_user_priorities (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    page_number INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    data JSONB NOT NULL,
    CONSTRAINT unique_run_page UNIQUE (run_id, page_number)
);

CREATE TABLE IF NOT EXISTS raw_organizations (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    user_id TEXT NOT NULL,
    data JSONB NOT NULL,
    CONSTRAINT unique_run_orgs UNIQUE (run_id, user_id)
);

CREATE TABLE IF NOT EXISTS raw_projects (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    organization_id TEXT NOT NULL,
    data JSONB NOT NULL,
    CONSTRAINT unique_run_org_projects UNIQUE (run_id, organization_id)
);

-- Pipeline execution metadata
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    records_extracted INTEGER,
    error_message TEXT,
    metadata JSONB
);

-- Indexes for querying latest data
CREATE INDEX idx_raw_user_priorities_loaded_at ON raw_user_priorities(loaded_at DESC);
CREATE INDEX idx_raw_user_priorities_run_id ON raw_user_priorities(run_id);
CREATE INDEX idx_raw_organizations_loaded_at ON raw_organizations(loaded_at DESC);
CREATE INDEX idx_raw_projects_loaded_at ON raw_projects(loaded_at DESC);
CREATE INDEX idx_pipeline_runs_started_at ON pipeline_runs(started_at DESC);

-- GIN indexes for JSONB querying
CREATE INDEX idx_raw_user_priorities_data ON raw_user_priorities USING GIN(data);
CREATE INDEX idx_raw_organizations_data ON raw_organizations USING GIN(data);
CREATE INDEX idx_raw_projects_data ON raw_projects USING GIN(data);
```

## 2. Configuration Module (config.py)

```python
import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# API Configuration
SWIFT_BASE_URL = "https://prod.api.swiftprojects.io"
SWIFT_USERNAME = os.getenv("SWIFT_EMAIL")
SWIFT_PASSWORD = os.getenv("SWIFT_PASSWORD")

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # Use service role key for backend

# Pipeline Configuration
PAGE_SIZE = 2000
MAX_RETRIES = 5
TIMEZONE = "America/New_York"

def get_supabase_client() -> Client:
    """Initialize Supabase client with service role key"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_KEY)
```

## 3. Extraction Module (extract.py)

```python
import requests
import time
import jwt
from typing import Dict, List, Optional
from datetime import datetime
from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, PAGE_SIZE, MAX_RETRIES

class SwiftAPIExtractor:
    def __init__(self):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None
    
    def authenticate(self) -> str:
        """Obtain authentication token"""
        url = f"{self.base_url}/api/auth/token"
        payload = {
            "grantType": "password",
            "include": ["profile", "firebaseToken"],
            "username": SWIFT_USERNAME,
            "password": SWIFT_PASSWORD,
            "scope": "openid"
        }
        
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload
        )
        response.raise_for_status()
        
        self.token = response.json()["idToken"]
        
        # Extract user_id from token
        decoded = jwt.decode(self.token.encode(), options={"verify_signature": False})
        self.user_id = decoded.get("sub").replace("|", ":")
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Authenticated as user: {self.user_id}")
        return self.token
    
    def extract_user_priorities(self) -> List[Dict]:
        """Extract all user priorities with pagination"""
        if not self.token:
            self.authenticate()
        
        all_records = []
        page = 0
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }
        
        while True:
            url = (
                f"{self.base_url}/api/next/user-priorities/_report"
                f"?pageSize={PAGE_SIZE}&page={page}"
                f"&filterOptions=%7B%22status%22%3A%7B%22approved%22%3Afalse%2C%22cancelled%22%3Afalse%7D%7D"
                f"&tz=America/New_York&dateFormat=yyyy-MM-dd%27T%27HH%3Amm%3AssZ"
            )
            
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = requests.get(url, headers=headers)
                    
                    if response.status_code == 200:
                        data = response.json().get("list", [])
                        
                        if not data:
                            print(f"[{datetime.now():%H:%M:%S}] ✅ User priorities extraction complete. Total: {len(all_records)} records")
                            return all_records
                        
                        all_records.extend(data)
                        print(f"[{datetime.now():%H:%M:%S}] 📄 Page {page}: {len(data)} records (Total: {len(all_records)})")
                        break
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] ⚠️ Status {response.status_code} on page {page}")
                        
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] ❌ Attempt {attempt}/{MAX_RETRIES} failed: {e}")
                    
                    if attempt < MAX_RETRIES:
                        wait = 2 ** (attempt - 1)
                        print(f"[{datetime.now():%H:%M:%S}] ⏳ Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] ❌ Max retries reached on page {page}")
                        return all_records
            
            page += 1
    
    def extract_organizations(self) -> List[Dict]:
        """Extract all organizations for the authenticated user"""
        if not self.token or not self.user_id:
            self.authenticate()
        
        url = f"{self.base_url}/api/users/{self.user_id}/organizations?page=0&pageSize=500"
        headers = {"Authorization": f"Bearer {self.token}"}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        orgs = response.json().get("list", [])
        print(f"[{datetime.now():%H:%M:%S}] ✅ Retrieved {len(orgs)} organizations")
        
        return orgs
    
    def extract_projects(self, org_id: str) -> List[Dict]:
        """Extract all projects for a given organization"""
        if not self.token:
            self.authenticate()
        
        url = f"{self.base_url}/api/organizations/{org_id}/projects?page=0&pageSize=100"
        headers = {"Authorization": f"Bearer {self.token}"}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        return response.json().get("list", [])
    
    def extract_all_projects(self) -> List[Dict]:
        """Extract projects for all organizations"""
        orgs = self.extract_organizations()
        all_projects = []
        
        for org in orgs:
            org_id = org["id"]
            org_name = org.get("name", "Unknown")
            
            print(f"[{datetime.now():%H:%M:%S}] ➡️ Extracting projects for: {org_name}")
            
            try:
                projects = self.extract_projects(org_id)
                
                # Enrich projects with org context
                for proj in projects:
                    proj["_org_id"] = org_id
                    proj["_org_name"] = org_name
                
                all_projects.extend(projects)
                print(f"[{datetime.now():%H:%M:%S}]    Retrieved {len(projects)} projects")
                
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] ⚠️ Failed to extract projects for {org_name}: {e}")
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Total projects extracted: {len(all_projects)}")
        return all_projects
```

## 4. Load Module (load.py)

```python
import uuid
from typing import List, Dict, Any
from datetime import datetime
from supabase import Client
from config import get_supabase_client

class SupabaseLoader:
    def __init__(self):
        self.client: Client = get_supabase_client()
        self.run_id: uuid.UUID = uuid.uuid4()
    
    def start_pipeline_run(self, pipeline_name: str) -> uuid.UUID:
        """Record pipeline run start"""
        result = self.client.table("pipeline_runs").insert({
            "run_id": str(self.run_id),
            "pipeline_name": pipeline_name,
            "status": "running",
            "started_at": datetime.utcnow().isoformat()
        }).execute()
        
        print(f"[{datetime.now():%H:%M:%S}] 🚀 Pipeline run started: {self.run_id}")
        return self.run_id
    
    def complete_pipeline_run(self, status: str, records_extracted: int = None, error_message: str = None):
        """Update pipeline run status"""
        update_data = {
            "status": status,
            "completed_at": datetime.utcnow().isoformat()
        }
        
        if records_extracted is not None:
            update_data["records_extracted"] = records_extracted
        
        if error_message:
            update_data["error_message"] = error_message
        
        self.client.table("pipeline_runs").update(update_data).eq("run_id", str(self.run_id)).execute()
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Pipeline run completed: {status}")
    
    def load_user_priorities_raw(self, records: List[Dict]) -> int:
        """Load user priorities as raw JSONB with pagination tracking"""
        if not records:
            print(f"[{datetime.now():%H:%M:%S}] ⚠️ No user priorities to load")
            return 0
        
        # Group records by page (simulate original pagination)
        page_size = 2000
        total_loaded = 0
        
        for page_num in range((len(records) + page_size - 1) // page_size):
            start_idx = page_num * page_size
            end_idx = min(start_idx + page_size, len(records))
            page_records = records[start_idx:end_idx]
            
            payload = {
                "run_id": str(self.run_id),
                "page_number": page_num,
                "record_count": len(page_records),
                "data": page_records  # Array of records as JSONB
            }
            
            self.client.table("raw_user_priorities").insert(payload).execute()
            total_loaded += len(page_records)
            
            print(f"[{datetime.now():%H:%M:%S}] 💾 Loaded page {page_num}: {len(page_records)} records")
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Total user priorities loaded: {total_loaded}")
        return total_loaded
    
    def load_organizations_raw(self, orgs: List[Dict], user_id: str) -> int:
        """Load organizations as raw JSONB"""
        if not orgs:
            print(f"[{datetime.now():%H:%M:%S}] ⚠️ No organizations to load")
            return 0
        
        payload = {
            "run_id": str(self.run_id),
            "user_id": user_id,
            "data": orgs  # Array of org records as JSONB
        }
        
        self.client.table("raw_organizations").insert(payload).execute()
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Loaded {len(orgs)} organizations")
        return len(orgs)
    
    def load_projects_raw(self, projects: List[Dict]) -> int:
        """Load projects as raw JSONB, grouped by organization"""
        if not projects:
            print(f"[{datetime.now():%H:%M:%S}] ⚠️ No projects to load")
            return 0
        
        # Group projects by organization
        projects_by_org = {}
        for proj in projects:
            org_id = proj.get("_org_id")
            if org_id not in projects_by_org:
                projects_by_org[org_id] = []
            projects_by_org[org_id].append(proj)
        
        total_loaded = 0
        
        for org_id, org_projects in projects_by_org.items():
            payload = {
                "run_id": str(self.run_id),
                "organization_id": org_id,
                "data": org_projects  # Array of project records as JSONB
            }
            
            self.client.table("raw_projects").insert(payload).execute()
            total_loaded += len(org_projects)
            
            print(f"[{datetime.now():%H:%M:%S}] 💾 Loaded {len(org_projects)} projects for org: {org_id}")
        
        print(f"[{datetime.now():%H:%M:%S}] ✅ Total projects loaded: {total_loaded}")
        return total_loaded
```

## 5. Main Pipeline Orchestration (pipeline.py)

```python
#!/usr/bin/env python3
"""
Swift API to Supabase Raw JSONB Pipeline
Full refresh extraction with raw data preservation
"""

import sys
from datetime import datetime
from extract import SwiftAPIExtractor
from load import SupabaseLoader

def run_pipeline():
    """Main pipeline orchestration"""
    print(f"\n{'='*60}")
    print(f"Swift API → Supabase Raw JSONB Pipeline")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")
    
    extractor = SwiftAPIExtractor()
    loader = SupabaseLoader()
    
    try:
        # Start pipeline run tracking
        loader.start_pipeline_run("swift_api_full_refresh")
        
        # Step 1: Extract user priorities
        print(f"\n[STEP 1] Extracting user priorities...")
        user_priorities = extractor.extract_user_priorities()
        
        # Step 2: Extract organizations
        print(f"\n[STEP 2] Extracting organizations...")
        organizations = extractor.extract_organizations()
        
        # Step 3: Extract all projects
        print(f"\n[STEP 3] Extracting projects...")
        projects = extractor.extract_all_projects()
        
        total_records = len(user_priorities) + len(organizations) + len(projects)
        print(f"\n{'='*60}")
        print(f"Extraction Summary:")
        print(f"  User Priorities: {len(user_priorities):,}")
        print(f"  Organizations: {len(organizations):,}")
        print(f"  Projects: {len(projects):,}")
        print(f"  Total Records: {total_records:,}")
        print(f"{'='*60}\n")
        
        # Step 4: Load to Supabase
        print(f"\n[STEP 4] Loading to Supabase...")
        
        loader.load_user_priorities_raw(user_priorities)
        loader.load_organizations_raw(organizations, extractor.user_id)
        loader.load_projects_raw(projects)
        
        # Mark pipeline as successful
        loader.complete_pipeline_run("success", total_records)
        
        print(f"\n{'='*60}")
        print(f"✅ Pipeline completed successfully")
        print(f"Run ID: {loader.run_id}")
        print(f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'='*60}\n")
        
        return 0
        
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"❌ Pipeline failed: {str(e)}")
        print(f"{'='*60}\n")
        
        loader.complete_pipeline_run("failed", error_message=str(e))
        return 1

if __name__ == "__main__":
    sys.exit(run_pipeline())
```

## 6. Dependencies (requirements.txt)

```
requests==2.31.0
python-dotenv==1.0.0
PyJWT==2.8.0
supabase==2.3.4
```

## 7. Environment Template (.env.example)

```bash
# Swift API Credentials
SWIFT_EMAIL=your.email@company.com
SWIFT_PASSWORD=your_password

# Supabase Configuration
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_service_role_key_here
```

## 8. README.md

```markdown
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
# Clone or create project directory
mkdir swift_api_pipeline
cd swift_api_pipeline

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your credentials
nano .env
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

## Deployment

### Option 1: Cron (Linux/Mac)

```bash
# Edit crontab
crontab -e

# Add daily execution at 2 AM
0 2 * * * cd /path/to/swift_api_pipeline && /path/to/venv/bin/python pipeline.py >> /var/log/swift_pipeline.log 2>&1
```

### Option 2: n8n Workflow

1. Create workflow with Schedule Trigger
2. Add Execute Command node
3. Command: `python /path/to/pipeline.py`
4. Add error notification (email/Slack)

### Option 3: GitHub Actions

```yaml
name: Swift API Pipeline
on:
  schedule:
    - cron: '0 2 * * *'  # Daily at 2 AM UTC
  workflow_dispatch:

jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python pipeline.py
        env:
          SWIFT_EMAIL: ${{ secrets.SWIFT_EMAIL }}
          SWIFT_PASSWORD: ${{ secrets.SWIFT_PASSWORD }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
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

## Data Retention

Raw data accumulates over time. Implement retention policy:

```sql
-- Delete runs older than 90 days
DELETE FROM raw_user_priorities 
WHERE loaded_at < NOW() - INTERVAL '90 days';

DELETE FROM raw_organizations 
WHERE loaded_at < NOW() - INTERVAL '90 days';

DELETE FROM raw_projects 
WHERE loaded_at < NOW() - INTERVAL '90 days';

DELETE FROM pipeline_runs 
WHERE started_at < NOW() - INTERVAL '90 days';
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

## Next Steps

This raw layer enables:
1. **Normalized views** - Create SQL views that flatten JSONB
2. **Materialized tables** - Build aggregated/transformed tables
3. **Power BI integration** - Connect to views for reporting
4. **Change data capture** - Compare runs to detect changes
5. **Data quality checks** - Validate completeness and consistency
```

## Deployment Instructions

### Initial Setup

```bash
# 1. Create project directory
mkdir swift_api_pipeline
cd swift_api_pipeline

# 2. Create all files from this specification
# (Use the file contents provided above)

# 3. Create virtual environment
python -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Configure environment
cp .env.example .env
# Edit .env with actual credentials

# 6. Run database migration
# - Open Supabase SQL Editor
# - Execute migrations/001_raw_tables.sql

# 7. Test pipeline
python pipeline.py
```

### Verification Queries

After first successful run:

```sql
-- Check pipeline run status
SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1;

-- Count loaded records
SELECT 
    (SELECT COUNT(*) FROM raw_user_priorities) as user_priorities,
    (SELECT COUNT(*) FROM raw_organizations) as organizations,
    (SELECT COUNT(*) FROM raw_projects) as projects;

-- Sample user priority data
SELECT 
    run_id,
    page_number,
    record_count,
    jsonb_array_length(data) as array_length
FROM raw_user_priorities
ORDER BY loaded_at DESC
LIMIT 5;
```

## Production Considerations

**Security**
- Use service role key only in backend/scheduled jobs
- Never commit .env file
- Rotate credentials periodically
- Use secrets manager for production deployments

**Performance**
- PAGE_SIZE=2000 balances memory and API calls
- GIN indexes enable fast JSONB queries
- Consider partitioning if data exceeds 10M rows

**Monitoring**
- Log all pipeline runs to pipeline_runs table
- Set up alerts for failed runs
- Track execution duration trends
- Monitor Supabase storage usage

**Data Quality**
- Validate record counts match API expectations
- Check for null/missing critical fields
- Compare run-over-run deltas for anomalies
- Implement data quality tests on JSONB structure

## Future Enhancements

Phase 2 options after raw layer is stable:

1. **Normalized Silver Layer**
   - Create dimension/fact tables
   - Implement SCD Type 2 for history tracking
   - Add data quality constraints

2. **Incremental Loading**
   - Use modified_at timestamps where available
   - Implement delta detection logic
   - Reduce full refresh frequency

3. **Transformation Layer**
   - dbt models for JSONB → normalized tables
   - Business logic transformations
   - Aggregated metrics tables

4. **Orchestration**
   - Prefect/Airflow for complex workflows
   - Dependency management between pipelines
   - Parallel extraction where possible

5. **Observability**
   - Integration with Sentry/Datadog
   - Custom metrics dashboard
   - Automated anomaly detection
