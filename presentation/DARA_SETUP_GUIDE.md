# DARA Setup Guide — Claude Project Update

## Steps to Update the DARA Project in Claude

### 1. Open the Existing DARA Project

Go to [claude.ai](https://claude.ai) → Projects → DARA (or create a new one if starting fresh)

### 2. Update Project Instructions

1. Click the **project settings** (gear icon or "Edit project")
2. Replace the **Custom Instructions** with the contents of `DARA_PROJECT_INSTRUCTIONS.md`
3. This is the system prompt that tells Claude what DARA is, what tables exist, rules, and examples

### 3. Verify Supabase Connection

1. In the project, check that the **Supabase MCP integration** is connected
2. It should show connected to project `voqfjfngdpcvevbkikud`
3. If not connected: go to Project Settings → Integrations → Add Supabase
4. The connection is **read-only** — DARA can only query, never modify data

### 4. Test the Updated DARA

Try these test questions to verify everything works:

**Basic data check:**
> "How many employees do we have by role?"

Expected: Should query `reference.ref_employees` and show TA: 57, TAS: 10, PAS: 9, Support: 32

**Daily reports check:**
> "Who worked yesterday and what did they do?"

Expected: Should query `analytics.v_daily_reports` and show employee names, hours, and work descriptions

**Timer check:**
> "Show me the top 5 technicians by hours logged this month"

Expected: Should query `analytics.v_timer_activities` or `stg_timer_activities_clean`

**Employee details check:**
> "Show me all TAS employees with their clusters"

Expected: Should query `reference.ref_employees WHERE role2 = 'TAS'`

### 5. Share with Team

Once verified:
1. Click **Share** on the project
2. Add team members (they must be in the Claude Team org)
3. They get the same instructions + Supabase connection

## What Changed (vs. Previous DARA)

| Area | Before | After |
|------|--------|-------|
| Employee data | No employee reference | Full ref_employees table (108 active, role2, cluster, carrier, schedules) |
| Daily reports | Not available | analytics.v_daily_reports with hours, descriptions, clock-in/out |
| Role filtering | No TA/TAS distinction | role2 column: TA, TAS, PAS, Support |
| Revenue targets | Not available | target_daily computed in v_daily_reports |
| Timer data | Used raw stg_timer_activities | Now uses clean table (corrections + dedup applied) |
| Schema metadata | 226 columns documented | 889 columns documented |
| Reference tables | None | ref_employees, ref_nni_directory, ref_ontel_techops_projects |

## Troubleshooting

**"DARA can't find the table"**
- Make sure the query uses the correct schema prefix (e.g., `analytics.v_daily_reports`, not just `v_daily_reports`)

**"Permission denied"**
- DARA should only query `analytics`, `data_staging`, and `reference` schemas
- If querying `data_raw` or `pipeline` → those are internal, redirect to staging/analytics

**"Timestamps look wrong"**
- All Supabase timestamps are stored in UTC
- DARA should always convert: `column AT TIME ZONE 'America/New_York'`

**"Empty results for daily reports"**
- Daily reports data starts from January 2026
- Timer data starts from 2023
- Asset tasks from 2022 (TS13+)
