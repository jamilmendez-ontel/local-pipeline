# Schema Metadata - analytics

This file mirrors `agent.schema_metadata` in the database.
Edit here for reference, then apply changes via SQL in Supabase SQL Editor.

---

## mv_daily_completion

**Description:** Pre-computed daily task completions per site and task type. Refreshed by pipeline.

**Business Context:** One row per date/site/task_type with project as a column. Use for site-level trend charts and daily progress tracking. User may say: "daily completions", "completion trend", "tasks per day", "site progress over time".

**Related Tables:** (standalone)

### `completion_date`
- **Description:** Date tasks were approved
- **Business Context:** Use for x-axis in trend charts. Filter by date range for performance.

### `tasks_completed`
- **Description:** Number of tasks approved on this date for this project/task_type
- **Business Context:** Aggregated count. Sum across task_types for daily total.

### `task_type`
- **Description:** Cleaned task name (AAT, RET, Sweeps, etc.)
- **Business Context:** From task_name_clean. Use for grouping/coloring in charts.

## mv_project_summary

**Description:** Pre-computed per-project metrics: task counts, completion %, hours, QA stats. Refreshed by pipeline.

**Business Context:** One row per project. Use for dashboards, project comparisons, executive summaries. No need to scan 2.2M task rows. User may say: "project summary", "project stats", "how is the project doing", "completion rate".

**Related Tables:** (standalone)

### `completion_pct`
- **Description:** Percentage of tasks approved out of total tasks
- **Business Context:** ROUND(100 * tasks_approved / total_tasks, 1). 0 if no tasks.

### `project_status`
- **Description:** Project lifecycle status
- **Business Context:** Values: in_progress, complete, pending. Filter by in_progress for "active" projects. From stg_projects.status.

### `qa_pass_rate`
- **Description:** Percentage of QA checks that passed
- **Business Context:** ROUND(100 * qa_pass_count / total_qa_checks, 1). NULL if no QA data.

### `total_hours_logged`
- **Description:** Total hours from timer entries for this project
- **Business Context:** Sum of duration_min / 60. From stg_timer_activities.

## mv_technician_stats

**Description:** Pre-computed per-technician metrics: task counts, completion rate, sites worked. Refreshed by pipeline.

**Business Context:** One row per technician. Use for performance reports, workload analysis. User may say: "technician stats", "tech performance", "who completed the most", "worker productivity".

**Related Tables:** (standalone)

### `completion_rate`
- **Description:** Percentage of assigned tasks that are approved
- **Business Context:** ROUND(100 * tasks_approved / total_tasks, 1).

### `unique_sites`
- **Description:** Number of distinct sites this technician has worked at
- **Business Context:** Count of unique asset_did values.

## v_asset_tasks

**Description:** Pre-joined view: tasks + assets + projects + orgs. Use instead of joining stg_asset_tasks manually.

**Business Context:** Most common query pattern. ~2.2M rows. Filter by project_name, task_status, task_name_clean for performance. User may say: "tasks", "work items", "what tasks are assigned". This table normaly called snapshot data

**Related Tables:** (standalone)

### `asset_did`
- **Description:** Unique asset identifier from Swift API
- **Business Context:** Links task to its parent asset. Join key to stg_assets.
- **Data Notes:** TEXT. Sourced from stg_asset_tasks.asset_did.

### `asset_id`
- **Description:** Human-readable asset/site ID (e.g., FA number)
- **Business Context:** The site identifier used in the field. Often an FA number.
- **Data Notes:** TEXT. Sourced from stg_assets.asset_id.

### `asset_name`
- **Description:** Human-readable asset/site name
- **Business Context:** The site name as displayed in Swift. Use for user-facing labels.
- **Example Values:** SOUTH RADNOR - C-Band, SXL01481, PHI SPORTS COMPLEX 15 SC - A - 5G L-Sub6 - Carrier Add
- **Data Notes:** TEXT. Sourced from stg_assets.asset_name.

### `carrier_group`
- **Description:** Normalized carrier group name
- **Business Context:** Carrier brand grouping (e.g., T-Mobile, AT&T). Use for carrier-level aggregation.
- **Example Values:** Verizon, AT&T/DISH, TMO/USCC
- **Data Notes:** TEXT. Resolved via carrier_group_lookup table.

### `org_name`
- **Description:** Organization name
- **Business Context:** The carrier/organization that owns this project. Top-level grouping.
- **Example Values:** Ontel
- **Data Notes:** TEXT. Resolved from stg_organizations.org_name.

### `project_did`
- **Description:** Unique project identifier from Swift API
- **Business Context:** Links task to its parent project. Join key to stg_projects.
- **Data Notes:** TEXT. Sourced from stg_asset_tasks.project_did.

### `project_name`
- **Description:** Human-readable project name
- **Business Context:** The project this task belongs to. Use for filtering by project.
- **Example Values:** TECH-OPS: TS17, TECH-OPS: TS18
- **Data Notes:** TEXT. Resolved from stg_projects.project_name.

### `task_approved_by_name`
- **Description:** Name of the person who approved the task
- **Business Context:** Who reviewed and approved the task.
- **Data Notes:** TEXT. Resolved from stg_asset_tasks.task_approved_by_name.

### `task_approved_on`
- **Description:** Date the task was approved
- **Business Context:** When a reviewer approved the completed task. NULL if not yet approved.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_asset_tasks.task_approved_on.

### `task_assigned_to_email`
- **Description:** Email of the person assigned to the task
- **Business Context:** Contact email for the assigned technician.
- **Data Notes:** TEXT. Resolved from stg_asset_tasks.task_assigned_to_email.

### `task_assigned_to_name`
- **Description:** Name of the person assigned to the task
- **Business Context:** Field technician or team member responsible for completing the task.
- **Data Notes:** TEXT. Resolved from stg_asset_tasks.task_assigned_to_name.

### `task_cancelled_by_name`
- **Description:** Name of the person who cancelled the task
- **Business Context:** Who cancelled the task. NULL if not cancelled.
- **Data Notes:** TEXT. Resolved from stg_asset_tasks.task_cancelled_by_name.

### `task_cancelled_on`
- **Description:** Date the task was cancelled
- **Business Context:** When the task was cancelled. NULL if not cancelled.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_asset_tasks.task_cancelled_on.

### `task_did`
- **Description:** Unique task identifier from Swift API
- **Business Context:** Primary key for joining tasks. Use this to uniquely identify an asset task.
- **Data Notes:** TEXT. Sourced from stg_asset_tasks.task_did.

### `task_name_clean`
- **Description:** Cleaned task type name
- **Business Context:** Standardized task name with trailing numbers removed. Use for grouping by task type.
- **Example Values:** Punch Item Live Review Complete, COP Punch Items Received, 3rd Party COP Rejections Reviewed
- **Data Notes:** TEXT. Derived from task_name via regex cleanup.

### `task_scheduled`
- **Description:** Scheduled date for the task
- **Business Context:** When the task is planned to occur. Use for scheduling and timeline analysis.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_asset_tasks.task_scheduled.

### `task_status`
- **Description:** Current workflow status of the task
- **Business Context:** Filter or group by status. Values: Pending, In Progress, Submitted, Approved, Rejected, Cancelled.
- **Data Notes:** TEXT. Sourced from stg_asset_tasks.task_status.

### `task_submitted_by_name`
- **Description:** Name of the person who submitted the task
- **Business Context:** Who submitted the completed task for review.
- **Data Notes:** TEXT. Resolved from stg_asset_tasks.task_submitted_by_name.

### `task_submitted_on`
- **Description:** Date the task was submitted for review
- **Business Context:** When the field technician submitted the task. NULL if not yet submitted.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_asset_tasks.task_submitted_on.

## v_qa_forms

**Description:** Pre-joined view: QA forms + assets + projects. Use instead of joining stg_qa_form manually.

**Business Context:** QA checklist items with resolved site info. ~346K rows. resolved_site_id/resolved_site_name come from stg_assets (canonical). User may say: "QA checks", "inspections", "quality".

**Related Tables:** (standalone)

### `aat`
- **Description:** AAT (Antenna Alignment Test) discipline status
- **Business Context:** Pass/Fail/N-A result for antenna alignment test QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.aat.

### `as_builts`
- **Description:** As-builts discipline status
- **Business Context:** Pass/Fail/N-A result for as-built documentation QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.as_builts.

### `asset_did`
- **Description:** Resolved asset identifier
- **Business Context:** Links QA form to stg_assets. Backfilled via asset_did matching process.
- **Data Notes:** TEXT. NULL if asset could not be matched (~4% of forms).

### `construction_manager`
- **Description:** Construction manager name
- **Business Context:** Construction manager overseeing the project/site.
- **Data Notes:** TEXT. Sourced from stg_qa_form.construction_manager.

### `crew_lead`
- **Description:** Crew lead name
- **Business Context:** Name of the crew lead responsible for the work being inspected.
- **Data Notes:** TEXT. Sourced from stg_qa_form.crew_lead.

### `fiber`
- **Description:** Fiber discipline status
- **Business Context:** Pass/Fail/N-A result for fiber inspection QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.fiber.

### `form_id`
- **Description:** Unique form instance identifier
- **Business Context:** Identifies a specific filled-out QA form instance.
- **Data Notes:** TEXT. Sourced from stg_qa_form.form_id.

### `form_name`
- **Description:** QA form template name
- **Business Context:** Name of the QA form template used. Identifies the type of QA inspection.
- **Data Notes:** TEXT. Sourced from stg_qa_form.form_name.

### `pictures`
- **Description:** Pictures discipline status
- **Business Context:** Pass/Fail/N-A result for photographic documentation QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.pictures.

### `pim`
- **Description:** PIM (Passive Intermodulation) discipline status
- **Business Context:** Pass/Fail/N-A result for PIM test QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.pim.

### `project_name`
- **Description:** Human-readable project name
- **Business Context:** The project this QA form belongs to.
- **Data Notes:** TEXT. Resolved from stg_projects.project_name.

### `requirement`
- **Description:** QA requirement name
- **Business Context:** The specific QA requirement being evaluated in this form row.
- **Data Notes:** TEXT. Sourced from stg_qa_form.requirement.

### `requirement_status`
- **Description:** QA requirement workflow status
- **Business Context:** Values: pending, submitted, approved, cancelled, in_progress. Use approved as "pass" and cancelled as "fail" for pass-rate calculations.

### `resolved_site_id`
- **Description:** Canonical site code from stg_assets (resolved via asset_did)
- **Business Context:** More reliable than site_id which comes from form submission text.

### `resolved_site_name`
- **Description:** Canonical site name from stg_assets (resolved via asset_did)
- **Business Context:** More reliable than site_name which comes from form submission text.

### `ret`
- **Description:** RET (Remote Electrical Tilt) discipline status
- **Business Context:** Pass/Fail/N-A result for RET QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.ret.

### `site_id`
- **Description:** Original site ID from QA form
- **Business Context:** Site identifier as entered on the QA form. May not match stg_assets exactly.
- **Data Notes:** TEXT. Sourced from stg_qa_form.site_id.

### `site_name`
- **Description:** Original site name from QA form
- **Business Context:** Site name as entered on the QA form.
- **Data Notes:** TEXT. Sourced from stg_qa_form.site_name.

### `subcontractor`
- **Description:** Subcontractor company name
- **Business Context:** Subcontractor performing the work being inspected.
- **Data Notes:** TEXT. Sourced from stg_qa_form.subcontractor.

### `sweeps`
- **Description:** Sweeps discipline status
- **Business Context:** Pass/Fail/N-A result for RF sweep test QA discipline.
- **Data Notes:** TEXT. Sourced from stg_qa_form.sweeps.

### `task_clean`
- **Description:** Cleaned task type name
- **Business Context:** Standardized task name associated with this QA form. Use for grouping.
- **Data Notes:** TEXT. Sourced from stg_qa_form.task_clean.

## v_timer_activities

**Description:** Pre-joined view: timer entries + assets + projects. Use instead of joining stg_timer_activities manually.

**Business Context:** GPS-tracked time logs with resolved asset info. ~273K rows. Filter by project_name, user_name, start_date. User may say: "time logs", "hours worked", "labor hours".

**Related Tables:** (standalone)

### `asset_did`
- **Description:** Unique asset identifier from Swift API
- **Business Context:** Links timer entry to its parent asset. Backfilled via asset_did process.
- **Data Notes:** TEXT. Resolved via stg_assets join. NULL for admin/overhead time with no site.

### `asset_id`
- **Description:** Human-readable asset/site ID
- **Business Context:** The site identifier (e.g., FA number) for the timer entry.
- **Data Notes:** TEXT. Resolved from stg_assets.asset_id.

### `asset_name`
- **Description:** Human-readable asset/site name
- **Business Context:** The site name as displayed in Swift.
- **Data Notes:** TEXT. Resolved from stg_assets.asset_name.

### `duration_min`
- **Description:** Duration in minutes
- **Business Context:** How long the activity lasted. Use for time tracking and productivity analysis.
- **Data Notes:** NUMERIC. Calculated from end_time - start_time.

### `end_date`
- **Description:** Date portion of end_time
- **Business Context:** Calendar date the timer ended.
- **Data Notes:** DATE. Derived from end_time.

### `end_time`
- **Description:** Timer end timestamp
- **Business Context:** When the technician stopped working on this activity.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_timer_activities.end_time.

### `project_did`
- **Description:** Unique project identifier from Swift API
- **Business Context:** Links timer entry to its parent project.
- **Data Notes:** TEXT. Resolved from stg_timer_activities.project_did.

### `project_name`
- **Description:** Human-readable project name
- **Business Context:** The project this timer entry belongs to.
- **Data Notes:** TEXT. Resolved from stg_projects.project_name.

### `site_lat`
- **Description:** Site latitude
- **Business Context:** GPS latitude of the cell site.
- **Data Notes:** NUMERIC. Sourced from stg_timer_activities.site_lat.

### `site_long`
- **Description:** Site longitude
- **Business Context:** GPS longitude of the cell site.
- **Data Notes:** NUMERIC. Sourced from stg_timer_activities.site_long.

### `site_vs_user_km`
- **Description:** Distance between site and user in kilometers
- **Business Context:** How far the technician was from the site when checking in. Use for proximity analysis.
- **Data Notes:** NUMERIC. Calculated via Haversine formula.

### `start_date`
- **Description:** Date portion of start_time
- **Business Context:** Calendar date the timer started. Use for daily aggregation.
- **Data Notes:** DATE. Derived from start_time.

### `start_time`
- **Description:** Timer start timestamp
- **Business Context:** When the technician started working on this activity.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_timer_activities.start_time.

### `task_clean`
- **Description:** Cleaned task type name
- **Business Context:** Standardized task name for grouping timer entries by work type.
- **Data Notes:** TEXT. Sourced from stg_timer_activities.task_clean.

### `user_accuracy_m`
- **Description:** GPS accuracy of user position in meters
- **Business Context:** Accuracy of the technician GPS reading. Higher values = less precise.
- **Data Notes:** NUMERIC. Sourced from stg_timer_activities.user_accuracy_m.

### `user_email`
- **Description:** Technician email
- **Business Context:** Email of the field technician.
- **Data Notes:** TEXT. Sourced from stg_timer_activities.user_email.

### `user_lat`
- **Description:** User latitude at check-in
- **Business Context:** GPS latitude of the technician when starting the timer.
- **Data Notes:** NUMERIC. Sourced from stg_timer_activities.user_lat.

### `user_long`
- **Description:** User longitude at check-in
- **Business Context:** GPS longitude of the technician when starting the timer.
- **Data Notes:** NUMERIC. Sourced from stg_timer_activities.user_long.

### `user_name`
- **Description:** Technician name
- **Business Context:** Name of the field technician who logged this timer entry.
- **Data Notes:** TEXT. Sourced from stg_timer_activities.user_name.

### `user_role`
- **Description:** Technician role
- **Business Context:** Role of the user in Swift (e.g., Technician, Lead).
- **Data Notes:** TEXT. Sourced from stg_timer_activities.user_role.

## v_user_priorities

**Description:** Pre-joined view: user priorities + assets + projects + orgs. Use instead of joining stg_user_priorities manually.

**Business Context:** Task priority queue with resolved asset/project info. User may say: "priorities", "schedule", "planned work".

**Related Tables:** (standalone)

### `approved_by`
- **Description:** Person who approved the task
- **Business Context:** Who approved the completed task.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.approved_by.

### `approved_on`
- **Description:** Date task was approved
- **Business Context:** When the task was approved.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.approved_on.

### `asset_did`
- **Description:** Unique asset identifier from Swift API
- **Business Context:** Links priority task to its parent asset.
- **Data Notes:** TEXT. Resolved via stg_assets join.

### `asset_id`
- **Description:** Human-readable asset/site ID
- **Business Context:** The site identifier (e.g., FA number).
- **Data Notes:** TEXT. Resolved from stg_assets.asset_id.

### `asset_name`
- **Description:** Human-readable asset/site name
- **Business Context:** The site name as displayed in Swift.
- **Data Notes:** TEXT. Resolved from stg_assets.asset_name.

### `assigned_to`
- **Description:** Person assigned to the task
- **Business Context:** Name of the field technician or team member assigned.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.assigned_to.

### `calendar_status`
- **Description:** Calendar scheduling status
- **Business Context:** Whether this task appears on the scheduling calendar and its status there.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.calendar_status.

### `cancelled_by`
- **Description:** Person who cancelled the task
- **Business Context:** Who cancelled this task.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.cancelled_by.

### `cancelled_on`
- **Description:** Date task was cancelled
- **Business Context:** When the task was cancelled.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.cancelled_on.

### `display_date`
- **Description:** Display date for UI
- **Business Context:** Date used for display/sorting in the priority view.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.display_date.

### `duration`
- **Description:** Expected task duration
- **Business Context:** Planned duration for this task.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.duration.

### `milestone`
- **Description:** Milestone name
- **Business Context:** Project milestone this priority task is associated with.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.milestone.

### `org_name`
- **Description:** Organization name
- **Business Context:** The carrier/organization that owns this project.
- **Data Notes:** TEXT. Resolved from stg_organizations.org_name.

### `pin_type`
- **Description:** Pin type indicator
- **Business Context:** Type of pin/marker used in the priority view (e.g., location pin type).
- **Data Notes:** TEXT. Sourced from stg_user_priorities.pin_type.

### `project_did`
- **Description:** Unique project identifier from Swift API
- **Business Context:** Links priority task to its parent project.
- **Data Notes:** TEXT. Resolved from stg_user_priorities.project_did.

### `project_name`
- **Description:** Human-readable project name
- **Business Context:** The project this priority task belongs to.
- **Data Notes:** TEXT. Resolved from stg_projects.project_name.

### `rejected_by`
- **Description:** Person who rejected the task
- **Business Context:** Who rejected the task submission.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.rejected_by.

### `rejected_on`
- **Description:** Date task was rejected
- **Business Context:** When the task was rejected.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.rejected_on.

### `scheduled`
- **Description:** Scheduled date
- **Business Context:** When this priority task is scheduled to be performed.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.scheduled.

### `scheduled_by`
- **Description:** Person who scheduled the task
- **Business Context:** Who assigned the schedule date for this task.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.scheduled_by.

### `status`
- **Description:** Current workflow status
- **Business Context:** Task status in the priority queue. Values: Pending, In Progress, Submitted, Approved, Rejected, Cancelled.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.status.

### `submitted_by`
- **Description:** Person who submitted the task
- **Business Context:** Who submitted this task for review.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.submitted_by.

### `submitted_on`
- **Description:** Date task was submitted
- **Business Context:** When the task was submitted for review.
- **Data Notes:** TIMESTAMPTZ. Sourced from stg_user_priorities.submitted_on.

### `task_did`
- **Description:** Unique task identifier from Swift API
- **Business Context:** Primary key for joining user priority tasks. Links to stg_asset_tasks.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.task_did.

### `task_name_clean`
- **Description:** Cleaned task type name
- **Business Context:** Standardized task name. Use for grouping priorities by task type.
- **Data Notes:** TEXT. Sourced from stg_user_priorities.task_name_clean.

# Schema Metadata - data_staging

---

## carrier_group_lookup

**Description:** Lookup table mapping search terms to carrier groups

**Business Context:** Maps keywords found in asset_id to carrier groups (Verizon, AT&T/DISH, TMO/USCC) for COP reporting. Used to backfill stg_assets.carrier_group.

**Data Notes:** Pattern matching uses ILIKE against asset_id. First match wins via match_order. 10 search terms map to 3 carrier groups.

**Related Tables:** stg_assets (backfills carrier_group via asset_id pattern matching)

### `carrier_group`
- **Description:** The carrier group label assigned when this search_term matches
- **Business Context:** Three possible values. Used for COP reporting and filtering.
- **Example Values:** Verizon, AT&T/DISH, TMO/USCC
- **Data Notes:** TEXT, NOT NULL.

### `id`
- **Description:** Auto-generated row identifier
- **Business Context:** Pipeline internal.
- **Data Notes:** SERIAL PRIMARY KEY.

### `match_order`
- **Description:** Priority order for pattern matching (lower = higher priority)
- **Business Context:** When multiple search terms match the same asset_id, the one with the lowest match_order wins (via DISTINCT ON ... ORDER BY match_order).
- **Data Notes:** INT, NOT NULL. Range: 1-10. Verizon terms are 1-3, AT&T/DISH are 4-5, TMO/USCC are 6-10.

### `search_term`
- **Description:** Keyword to match against asset_id using ILIKE pattern
- **Business Context:** If asset_id contains this term (case-insensitive), the asset is assigned the corresponding carrier_group.
- **Example Values:** VZW, Verizon, DISH, AT&T, T-Mobile, US Cellular
- **Data Notes:** TEXT, NOT NULL, UNIQUE. Each term maps to exactly one carrier_group.

## qa_form_asset_did_lookup

**Description:** Persistent lookup table for QA form asset_did mappings

**Business Context:** Internal pipeline table â€” not for direct user queries. Preserves site_id-to-asset_did mappings that would be lost during stg_qa_form truncate+reload.

**Data Notes:** Cumulative: never loses established mappings. During each pipeline run, Pass 0 of backfill_asset_did() restores mappings from this table, and the Save step persists any new mappings back.

**Related Tables:** stg_qa_form (provides asset_did recovery), stg_assets (source of asset_did)

### `asset_did`
- **Description:** Immutable asset identifier mapped to this site_id
- **Business Context:** The resolved asset_did from stg_assets. Used to restore stg_qa_form.asset_did after truncate+reload.
- **Data Notes:** TEXT, NOT NULL. Once set, should not change for a given site_id.

### `site_id`
- **Description:** Site identifier used as the primary key
- **Business Context:** Matches stg_qa_form.site_id and stg_assets.asset_id. This is the lookup key.
- **Data Notes:** TEXT, NOT NULL, PRIMARY KEY. One row per unique site_id.

### `site_name`
- **Description:** Human-readable site name for reference
- **Business Context:** Matches stg_qa_form.site_name. Nullable â€” stored for debugging, not used in lookups.

### `updated_at`
- **Description:** Timestamp of last update to this mapping
- **Business Context:** Pipeline internal. Tracks when the mapping was last confirmed or updated.
- **Data Notes:** Defaults to NOW(). Updated on each UPSERT.

## stg_ar_aging

**Description:** Accounts receivable aging report from QuickBooks via daily email

**Business Context:** Standalone financial table. Daily snapshot of outstanding invoices with aging buckets, amounts, and open balances by customer.

**Data Notes:** Append-only: each pipeline run adds new rows from the latest QuickBooks AR Aging email. Rows are never updated or deleted. Source: QuickBooks desktop report emailed daily as CSV attachment.

**Related Tables:** (standalone)

### `aging_bucket`
- **Description:** Aging category (Current, 1-30, 31-60, 61-90, 91+)
- **Business Context:** How overdue the invoice is. User may say: "aging bucket", "how old", "overdue category", "days past due".
- **Example Values:** Current, 1 - 30, 31 - 60, 61 - 90, 91 and over

### `amount`
- **Description:** Original transaction amount in dollars
- **Business Context:** Full invoice amount. User may say: "amount", "invoice amount", "how much".
- **Data Notes:** Can be negative for credit memos and payments. Positive for invoices.

### `as_of_date`
- **Description:** Date of the aging report snapshot
- **Business Context:** The report date from QuickBooks. User may say: "report date", "as of", "snapshot date".

### `customer`
- **Description:** Customer name from QuickBooks
- **Business Context:** The company or person being billed. User may say: "customer", "client", "who owes", "billed to".

### `date`
- **Description:** Transaction/invoice date
- **Business Context:** Text field from QuickBooks. User may say: "invoice date", "transaction date".

### `due_date`
- **Description:** Payment due date for the invoice
- **Business Context:** Text field. User may say: "due date", "when is it due", "payment deadline".

### `email_received_date`
- **Description:** Date the report email was received
- **Business Context:** When the automated email arrived from QuickBooks. Used for dedup: the pipeline only processes emails newer than the max email_received_date already loaded. User may say: "email date", "received date".

### `id`
- **Description:** Auto-generated row identifier
- **Business Context:** Pipeline internal. Not useful for business queries.
- **Data Notes:** BIGINT GENERATED ALWAYS AS IDENTITY. Do not use in WHERE clauses for business logic.

### `loaded_at`
- **Description:** Timestamp when this row was loaded into the database
- **Business Context:** Pipeline internal. Use email_received_date for business time filtering instead.
- **Data Notes:** Defaults to NOW() at insert time. Always UTC.

### `location`
- **Description:** QuickBooks location/class for the transaction
- **Business Context:** User may say: "location", "class", "department".

### `num`
- **Description:** Invoice or transaction number
- **Business Context:** QuickBooks reference number. User may say: "invoice number", "invoice #", "transaction number".

### `open_balance`
- **Description:** Remaining unpaid balance in dollars
- **Business Context:** Amount still owed. User may say: "open balance", "outstanding", "unpaid", "balance due", "how much is owed".
- **Data Notes:** Zero means fully paid. NULL should not occur. Sum open_balance for total AR outstanding.

### `past_due`
- **Description:** Days past due (integer)
- **Business Context:** Number of days past the due date. 0 means not overdue. User may say: "past due", "overdue days", "days late", "how overdue".
- **Data Notes:** Integer value representing days, NOT a dollar amount. Use aging_bucket for categorical grouping.

### `po_number`
- **Description:** Purchase order number
- **Business Context:** Customer PO reference. User may say: "PO", "PO number", "purchase order".

### `run_id`
- **Description:** Pipeline run identifier
- **Business Context:** Pipeline internal. Links to pipeline.pipeline_runs for debugging.
- **Data Notes:** UUID referencing the pipeline run that loaded this row.

### `transaction_type`
- **Description:** Type of transaction (Invoice, Payment, Credit Memo, etc.)
- **Business Context:** QuickBooks transaction type. User may say: "type", "transaction type", "invoice or payment".
- **Example Values:** Invoice, Payment, Credit Memo, Journal Entry

## stg_asset_tasks

**Description:** Individual work tasks at each cell tower site

**Business Context:** Largest table (~2.2M rows). Each row is one task (e.g., AAT test) at one site. Filter by project_did and task_status for performance. User may say: "tasks", "work items", "jobs", "assignments".

**Related Tables:** stg_assets (via asset_did), stg_projects (via project_did)

## stg_assets

**Description:** Aggregated site/cell tower data with task status counts per asset

**Business Context:** Central hub table linking projects to tasks, QA forms, timer activities, and user priorities. Each row is a unique site within a project.

**Related Tables:** stg_projects (via project_did), stg_asset_tasks (via asset_did), stg_qa_form (via asset_did), stg_timer_activities (via asset_did), stg_user_priorities (via asset_did)

## stg_organizations

**Description:** Client organizations that own construction projects

**Business Context:** Each org has projects (stg_projects). Join on org_did. User may say: "clients", "companies", "organizations", "customers", "accounts".

**Related Tables:** stg_projects (via org_did), stg_user_priorities (via org_did)

## stg_projects

**Description:** Master list of all TECH-OPS construction projects with aggregate metrics

**Business Context:** Projects are contract periods (TS13-TS18) tracking cell tower construction work. Each project contains multiple sites/assets. User may say: "contracts", "programs", "phases", "work orders".

**Related Tables:** stg_organizations (via org_did), stg_assets (via project_did), stg_asset_tasks (via project_did), stg_timer_activities (via project_did), stg_user_priorities (via project_did)

## stg_qa_form

**Description:** Quality assurance form responses for completed work

**Business Context:** Each row is a QA checklist item. Multiple rows per site/task. ~344K rows. User may say: "QA", "quality checks", "inspections", "checklists", "quality assurance", "quality control", "QC".

**Related Tables:** stg_assets (via asset_did)

## stg_sales_detail

**Description:** Sales detail report from QuickBooks via daily email

**Business Context:** Standalone financial table. Transaction-level sales data with quantities, prices, amounts, and PO numbers by customer.

**Data Notes:** Append-only: each pipeline run adds new rows from the latest QuickBooks Sales Detail email. Rows are never updated or deleted. Source: QuickBooks desktop report emailed daily as CSV attachment.

**Related Tables:** (standalone)

### `amount`
- **Description:** Total line item amount in dollars (qty x sales_price)
- **Business Context:** User may say: "amount", "total", "line total", "how much".
- **Data Notes:** Equals qty * sales_price. Can be negative for credit memos. Positive for invoices and sales receipts.

### `as_of_date`
- **Description:** Date of the sales report
- **Business Context:** The report date from QuickBooks. User may say: "report date", "as of date".

### `balance`
- **Description:** Running balance for this transaction
- **Business Context:** User may say: "balance", "remaining".
- **Data Notes:** Running balance for the transaction. Decreases as payments are applied. Zero means fully paid.

### `customer`
- **Description:** Customer name from QuickBooks
- **Business Context:** The company or person purchasing. User may say: "customer", "client", "buyer".

### `date`
- **Description:** Transaction date
- **Business Context:** Text field from QuickBooks. User may say: "sale date", "transaction date".

### `email_received_date`
- **Description:** Date the report email was received
- **Business Context:** When the automated email arrived from QuickBooks. Used for dedup: the pipeline only processes emails newer than the max email_received_date already loaded. User may say: "email date", "received date".

### `id`
- **Description:** Auto-generated row identifier
- **Business Context:** Pipeline internal. Not useful for business queries.
- **Data Notes:** BIGINT GENERATED ALWAYS AS IDENTITY. Do not use in WHERE clauses for business logic.

### `loaded_at`
- **Description:** Timestamp when this row was loaded into the database
- **Business Context:** Pipeline internal. Use email_received_date for business time filtering instead.
- **Data Notes:** Defaults to NOW() at insert time. Always UTC.

### `memo_description`
- **Description:** Line item description or memo
- **Business Context:** Details about what was sold/billed. User may say: "description", "memo", "what was sold", "line item".

### `num`
- **Description:** Invoice or transaction number
- **Business Context:** QuickBooks reference number. User may say: "invoice number", "transaction number".

### `po_number`
- **Description:** Purchase order number
- **Business Context:** Customer PO reference. User may say: "PO", "PO number", "purchase order".

### `qty`
- **Description:** Quantity of items/units sold
- **Business Context:** User may say: "quantity", "how many", "units".
- **Data Notes:** Can be 0 for non-quantity items (e.g., discount lines, subtotal rows). NULL should not occur.

### `run_id`
- **Description:** Pipeline run identifier
- **Business Context:** Pipeline internal. Links to pipeline.pipeline_runs for debugging.
- **Data Notes:** UUID referencing the pipeline run that loaded this row.

### `sales_price`
- **Description:** Unit price per item in dollars
- **Business Context:** Price per unit. User may say: "price", "unit price", "rate", "how much each".
- **Data Notes:** Can be NULL for summary/subtotal rows that have no unit price. Represents price per unit in dollars.

### `service_date`
- **Description:** Date the service was performed
- **Business Context:** Text field. May differ from invoice date. User may say: "service date", "work date", "when was it done".

### `transaction_type`
- **Description:** Type of transaction (Invoice, Sales Receipt, etc.)
- **Business Context:** QuickBooks transaction type. User may say: "type", "transaction type".
- **Example Values:** Invoice, Sales Receipt, Credit Memo

## stg_timer_activities

**Description:** GPS-tracked time logs for technician site visits

**Business Context:** Incremental load - new data appends. ~273K entries. Use for labor analysis. User may say: "time logs", "timesheets", "work hours", "labor hours", "clock-in/clock-out", "time tracking", "hours worked", "time entries".

**Related Tables:** stg_assets (via asset_did), stg_projects (via project_did)

## stg_user_priorities

**Description:** Task scheduling and approval workflow data from user priority queues

**Business Context:** Tracks task assignments, scheduling, submissions, approvals, rejections, and cancellations across organizations and projects.

**Related Tables:** stg_organizations (via org_did), stg_projects (via project_did), stg_assets (via asset_did)
