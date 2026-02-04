# Swift Pipeline Data Dictionary

This document describes the staging data available for analysis. All data is sourced from the Swift Projects API and stored in a local Supabase PostgreSQL database.

---

## Overview

### Data Source
- **Swift Projects API** (https://prod.api.swiftprojects.io)
- Data extracted via Python ETL pipelines
- All timestamps are in **America/New_York** timezone

### Schema
All staging tables are in the `data_staging` schema:
```sql
-- Access tables using schema prefix
SELECT * FROM data_staging.stg_projects;
```

### Data Freshness
- Data is extracted periodically via manual pipeline runs
- Check `pipeline.pipeline_runs` for last extraction timestamps
- Timer activities append new data; other tables are full refreshes

---

## Business Context

This data tracks **telecommunications construction projects** (cell tower installations, upgrades, etc.) managed by Ontel. Projects are named "TECH-OPS: TS1" through "TECH-OPS: TS18" representing different contract periods.

Key concepts:
- **Project**: A contract period (TS13, TS14, etc.)
- **Asset/Site**: A physical cell tower location
- **Task**: Work to be performed at a site (e.g., "Install Antenna", "Run Fiber")
- **QA Form**: Quality assurance checklist completed after work

---

## Tables

### 1. stg_projects
**Description**: Master list of all TECH-OPS projects with metrics.

| Column | Type | Description |
|--------|------|-------------|
| project_did | text | Unique project identifier (PK) |
| project_name | text | Display name (e.g., "TECH-OPS: TS13") |
| org_did | text | Organization ID (FK to stg_organizations) |
| org_name | text | Organization name |
| status | text | Project status (usually "in_progress") |
| asset_task_count | integer | Total tasks across all sites |
| asset_task_pending | integer | Tasks not yet started |
| asset_task_approved | integer | Completed and approved tasks |
| asset_task_rejected | integer | Rejected tasks |
| asset_task_submitted | integer | Tasks submitted for approval |
| asset_task_in_progress | integer | Tasks currently being worked |
| asset_task_cancelled | integer | Cancelled tasks |
| asset_project_count | integer | Number of sites in project |
| date_created | timestamptz | When project was created |
| last_updated | timestamptz | Last modification time |

**Row Count**: ~1,100 projects

**Example Query**:
```sql
-- Get active TECH-OPS projects with task metrics
SELECT project_name,
       asset_project_count as sites,
       asset_task_approved as completed,
       asset_task_pending as pending,
       ROUND(100.0 * asset_task_approved / NULLIF(asset_task_count, 0), 1) as completion_pct
FROM data_staging.stg_projects
WHERE project_name LIKE 'TECH-OPS: TS%'
ORDER BY project_name;
```

---

### 2. stg_asset_tasks
**Description**: Individual tasks at each site. This is the largest table with detailed work item tracking.

| Column | Type | Description |
|--------|------|-------------|
| project_did | text | Project identifier (FK to stg_projects) |
| asset_did | text | Site/asset identifier |
| task_did | text | Unique task identifier |
| asset_id | text | Human-readable site ID |
| asset_name | text | Site name/location |
| task_name | text | Type of work (e.g., "AAT", "RET", "Fiber") |
| task_status | text | Status: pending, in_progress, submitted, approved, rejected, cancelled |
| task_scheduled | date | Scheduled work date |
| task_assigned_to_name | text | Technician assigned |
| task_assigned_to_email | text | Technician email |
| task_submitted_on | date | When work was submitted |
| task_submitted_by_name | text | Who submitted |
| task_approved_on | date | When approved |
| task_approved_by_name | text | Who approved |
| task_cancelled_on | date | When cancelled (if applicable) |

**Row Count**: ~2.2 million tasks

**Common Task Names**:
- AAT (Antenna Alignment Test)
- RET (Remote Electrical Tilt)
- Fiber
- PIM (Passive Intermodulation)
- Sweeps
- Pictures
- As-Builts

**Example Queries**:
```sql
-- Tasks completed per project this month
SELECT p.project_name,
       COUNT(*) as tasks_approved
FROM data_staging.stg_asset_tasks t
JOIN data_staging.stg_projects p ON t.project_did = p.project_did
WHERE t.task_status = 'approved'
  AND t.task_approved_on >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY p.project_name
ORDER BY tasks_approved DESC;

-- Top technicians by approved tasks
SELECT task_assigned_to_name as technician,
       COUNT(*) as tasks_completed
FROM data_staging.stg_asset_tasks
WHERE task_status = 'approved'
  AND task_approved_on >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY task_assigned_to_name
ORDER BY tasks_completed DESC
LIMIT 10;
```

---

### 3. stg_qa_form
**Description**: Quality assurance form responses. Each row is a QA checklist for a completed task.

| Column | Type | Description |
|--------|------|-------------|
| form_name | text | Form identifier (qa_ts13, qa_ts14, etc.) |
| project | text | Project name |
| project_number | integer | Project number (13, 14, 15, etc.) |
| site_name | text | Site/location name |
| site_id | text | Site identifier |
| task | text | Task type reviewed |
| requirement | text | Specific requirement checked |
| requirement_status | text | Pass/Fail/N/A status |
| crew_lead | text | Crew lead name |
| construction_manager | text | CM responsible |
| subcontractor | text | Subcontractor (if applicable) |
| live_review_performed | text | Yes/No |
| aat | text | AAT result |
| aat_issues | text | AAT issues found |
| ret | text | RET result |
| ret_issues | text | RET issues found |
| sweeps | text | Sweeps result |
| pim | text | PIM result |
| fiber | text | Fiber result |
| pictures | text | Pictures result |
| as_builts | text | As-builts result |
| conditional_pass | text | Conditional pass notes |
| ... | text | (81 total columns for all QA fields) |

**Row Count**: ~343,000 QA responses

**Example Queries**:
```sql
-- QA pass rate by project
SELECT project,
       COUNT(*) as total_reviews,
       SUM(CASE WHEN requirement_status = 'Pass' THEN 1 ELSE 0 END) as passed,
       ROUND(100.0 * SUM(CASE WHEN requirement_status = 'Pass' THEN 1 ELSE 0 END) / COUNT(*), 1) as pass_rate
FROM data_staging.stg_qa_form
GROUP BY project
ORDER BY project;

-- Common QA issues
SELECT aat_issues, COUNT(*) as occurrences
FROM data_staging.stg_qa_form
WHERE aat_issues IS NOT NULL AND aat_issues != ''
GROUP BY aat_issues
ORDER BY occurrences DESC
LIMIT 10;
```

---

### 4. stg_timer_activities
**Description**: Time tracking data showing when technicians worked at sites.

| Column | Type | Description |
|--------|------|-------------|
| project | text | Project name |
| project_number | integer | Project number |
| project_did | text | Project identifier (FK to stg_projects) |
| site_name | text | Site name |
| site_id | text | Site identifier |
| task | text | Task being worked |
| start_time | timestamptz | Clock-in time |
| end_time | timestamptz | Clock-out time |
| duration_min | numeric | Duration in minutes |
| user_name | text | Technician name |
| user_email | text | Technician email |
| user_role | text | Role (e.g., "Technician", "Crew Lead") |
| site_lat | numeric | Site latitude |
| site_long | numeric | Site longitude |
| user_lat | numeric | User GPS latitude at clock-in |
| user_long | numeric | User GPS longitude at clock-in |
| user_accuracy_m | numeric | GPS accuracy in meters |
| site_vs_user_km | numeric | Distance from site (km) |
| run_date | date | When this data was extracted |

**Row Count**: ~11,600 timer entries

**Note**: This table appends data over time (incremental loads).

**Example Queries**:
```sql
-- Hours worked per project this month
SELECT project,
       ROUND(SUM(duration_min) / 60.0, 1) as total_hours,
       COUNT(DISTINCT user_email) as unique_techs
FROM data_staging.stg_timer_activities
WHERE start_time >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY project
ORDER BY total_hours DESC;

-- Average time per task type
SELECT task,
       COUNT(*) as entries,
       ROUND(AVG(duration_min), 1) as avg_minutes
FROM data_staging.stg_timer_activities
GROUP BY task
ORDER BY entries DESC;

-- Technicians with suspicious GPS distance
SELECT user_name, site_name, site_vs_user_km, start_time
FROM data_staging.stg_timer_activities
WHERE site_vs_user_km > 1.0  -- More than 1km from site
ORDER BY site_vs_user_km DESC;
```

---

### 5. stg_organizations
**Description**: Organization/company information.

| Column | Type | Description |
|--------|------|-------------|
| org_did | text | Organization identifier (PK) |
| org_name | text | Organization name |
| avc | integer | AVC code |
| poc_name | text | Point of contact name |
| poc_email | text | Point of contact email |
| date_created | timestamptz | Creation date |

**Row Count**: ~300 organizations

---

## Reference Tables

### reference.ref_ontel_techops_projects
**Description**: Quick reference for TECH-OPS projects (TS1-TS18).

| project_number | project_name | project_did | asset_project_count |
|----------------|--------------|-------------|---------------------|
| 13 | TECH-OPS: TS13 | -NFkG865XjMXlwqZ1AqU | 4,814 sites |
| 14 | TECH-OPS: TS14 | -NV5j_QcTmdwoaGklFvf | 4,981 sites |
| 15 | TECH-OPS: TS15 | -Np5nDzlfJrK_nt5Ro7e | 4,892 sites |
| 16 | TECH-OPS: TS16 | -O99xSQdLiGywc6KRVw- | 5,362 sites |
| 17 | TECH-OPS: TS17 | -ONLJdAstPfeGwVNgpYH | 5,080 sites |
| 18 | TECH-OPS: TS18 | -O_IpQNpLVwhdVC3QYIm | 4,211 sites |

**Note**: TS13-TS18 are the active projects with QA forms and timer data.

---

## Relationships

```
stg_organizations (org_did)
    └── stg_projects (org_did → org_did)
            ├── stg_asset_tasks (project_did → project_did)
            ├── stg_qa_form (project_number matches project name)
            └── stg_timer_activities (project_did → project_did, FK)
```

---

## Common Analysis Patterns

### 1. Project Progress
```sql
SELECT project_name,
       asset_project_count as total_sites,
       asset_task_approved as tasks_done,
       asset_task_pending as tasks_remaining,
       ROUND(100.0 * asset_task_approved / NULLIF(asset_task_count, 0), 1) as pct_complete
FROM data_staging.stg_projects
WHERE project_name LIKE 'TECH-OPS: TS1%'
ORDER BY project_name;
```

### 2. Technician Productivity
```sql
SELECT t.task_assigned_to_name,
       COUNT(*) as tasks_approved,
       COUNT(DISTINCT t.asset_did) as unique_sites
FROM data_staging.stg_asset_tasks t
WHERE t.task_status = 'approved'
  AND t.task_approved_on >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY t.task_assigned_to_name
ORDER BY tasks_approved DESC
LIMIT 20;
```

### 3. QA Issues Analysis
```sql
SELECT project,
       SUM(CASE WHEN aat_issues IS NOT NULL AND aat_issues != '' THEN 1 ELSE 0 END) as aat_issues,
       SUM(CASE WHEN ret_issues IS NOT NULL AND ret_issues != '' THEN 1 ELSE 0 END) as ret_issues,
       SUM(CASE WHEN pim_issues IS NOT NULL AND pim_issues != '' THEN 1 ELSE 0 END) as pim_issues
FROM data_staging.stg_qa_form
GROUP BY project
ORDER BY project;
```

### 4. Time Analysis
```sql
SELECT DATE(start_time) as work_date,
       project,
       COUNT(*) as clock_ins,
       ROUND(SUM(duration_min) / 60.0, 1) as total_hours
FROM data_staging.stg_timer_activities
WHERE start_time >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY DATE(start_time), project
ORDER BY work_date DESC, total_hours DESC;
```

---

## Notes for AI Agent

1. **Always use schema prefix**: `data_staging.table_name`
2. **Timezone**: All timestamps are America/New_York
3. **Project filtering**: For recent/active work, focus on TS13-TS18
4. **Large tables**: stg_asset_tasks has 2.2M rows - use LIMIT and filters
5. **NULL handling**: Many text fields can be NULL or empty string
6. **Date ranges**: Timer data is incremental; other tables are full snapshots
