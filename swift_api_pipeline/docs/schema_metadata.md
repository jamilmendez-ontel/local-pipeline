# Schema Metadata - data_staging

This file mirrors `agent.schema_metadata` in the database.
Edit here for reference, then apply changes via SQL in Supabase SQL Editor.

---

## stg_ar_aging

**Description:** Accounts receivable aging report from QuickBooks via daily email

**Business Context:** Standalone financial table. Daily snapshot of outstanding invoices with aging buckets, amounts, and open balances by customer.

**Related Tables:** (standalone)

### `aging_bucket`
- **Description:** Aging category (Current, 1-30, 31-60, 61-90, 91+)
- **Business Context:** How overdue the invoice is. User may say: "aging bucket", "how old", "overdue category", "days past due".

### `amount`
- **Description:** Original transaction amount in dollars
- **Business Context:** Full invoice amount. User may say: "amount", "invoice amount", "how much".

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
- **Business Context:** When the automated email arrived. User may say: "email date", "received date".

### `location`
- **Description:** QuickBooks location/class for the transaction
- **Business Context:** User may say: "location", "class", "department".

### `num`
- **Description:** Invoice or transaction number
- **Business Context:** QuickBooks reference number. User may say: "invoice number", "invoice #", "transaction number".

### `open_balance`
- **Description:** Remaining unpaid balance in dollars
- **Business Context:** Amount still owed. User may say: "open balance", "outstanding", "unpaid", "balance due", "how much is owed".

### `past_due`
- **Description:** Amount past due in dollars
- **Business Context:** Portion of open_balance that is overdue. User may say: "past due", "overdue amount", "late balance".

### `po_number`
- **Description:** Purchase order number
- **Business Context:** Customer PO reference. User may say: "PO", "PO number", "purchase order".

### `transaction_type`
- **Description:** Type of transaction (Invoice, Payment, Credit Memo, etc.)
- **Business Context:** QuickBooks transaction type. User may say: "type", "transaction type", "invoice or payment".

## stg_assets

**Description:** Aggregated site/cell tower data with task status counts per asset

**Business Context:** Central hub table linking projects to tasks, QA forms, timer activities, and user priorities. Each row is a unique site within a project.

**Related Tables:**
- stg_projects (via project_did)
- stg_asset_tasks (via asset_did)
- stg_qa_form (via asset_did)
- stg_timer_activities (via asset_did)
- stg_user_priorities (via asset_did)

### `asset_did`
- **Description:** Immutable Swift API asset identifier
- **Business Context:** Never changes even if asset_id or asset_name change. Primary join key for timer, QA form, user priorities. User may say: "asset ID", "site identifier".

### `asset_id`
- **Description:** Human-readable site code (can change over time)
- **Business Context:** Carrier-assigned code like "ATL001". IMPORTANT: Can be renamed — use asset_did for stable joins. User may say: "site ID", "site code", "FA code", "tower ID".

### `asset_name`
- **Description:** Site name or address (can change over time)
- **Business Context:** Usually includes address or landmark. IMPORTANT: Can be renamed — use asset_did for stable joins. User may say: "site name", "tower name", "location".

### `project_did`
- **Description:** Project this asset belongs to (foreign key)
- **Business Context:** Join to stg_projects.project_did. Each asset belongs to exactly one project.

### `requirement_count`
- **Description:** Total QA requirements across all tasks at this site
- **Business Context:** Number of QA checklist items. User may say: "how many requirements", "QA items".

### `task_count`
- **Description:** Total number of tasks at this site
- **Business Context:** Sum of all task statuses. User may say: "how many tasks", "total tasks at this site".

### `tasks_approved`
- **Description:** Number of approved/completed tasks at this site
- **Business Context:** Passed QA review. User may say: "completed", "done", "finished", "approved".

### `tasks_cancelled`
- **Description:** Number of cancelled tasks at this site
- **Business Context:** Terminal status. User may say: "cancelled", "removed".

### `tasks_in_progress`
- **Description:** Number of in-progress tasks at this site
- **Business Context:** Currently being worked on. User may say: "active", "in progress", "working on".

### `tasks_pending`
- **Description:** Number of pending tasks at this site
- **Business Context:** Not yet started. User may say: "pending", "not started", "backlog".

### `tasks_rejected`
- **Description:** Number of rejected tasks at this site
- **Business Context:** Failed QA review, needs rework. User may say: "rejected", "failed".

### `tasks_submitted`
- **Description:** Number of submitted tasks awaiting review at this site
- **Business Context:** Turned in but not yet approved. User may say: "submitted", "pending review".

## stg_asset_tasks

**Description:** Individual work tasks at each cell tower site

**Business Context:** Largest table (~2.2M rows). Each row is one task (e.g., AAT test) at one site. Filter by project_did and task_status for performance. User may say: "tasks", "work items", "jobs", "assignments".

**Related Tables:**
- stg_assets (via asset_did)
- stg_projects (via project_did)

### `asset_did`
- **Description:** Immutable asset identifier (foreign key)
- **Business Context:** Join to stg_assets.asset_did. More stable than asset_id which can change.

### `asset_id`
- **Description:** Short identifier for the site
- **Business Context:** Carrier-assigned site code. User may say: "site ID", "site code", "tower ID", "FA code", "location ID".

### `asset_name`
- **Description:** Name/location of the cell tower site
- **Business Context:** Usually includes address or landmark. User may say: "site name", "tower name", "location", "cell site", "tower location", "site".

### `asset_requirement_count`
- **Description:** Number of QA requirements for this asset
- **Business Context:** QA checklist items that must pass. User may say: "requirements", "QA items", "checklist count".

### `project_did`
- **Description:** Project this task belongs to (foreign key)
- **Business Context:** Join to stg_projects.project_did. Filter by this for project-level queries — much faster on 2.2M rows.

### `project_status`
- **Description:** Status of the parent project at time of extraction
- **Business Context:** Denormalized from stg_projects. User may say: "is the project active".

### `task_approved_by_email`
- **Description:** Email of the person who approved the task
- **Business Context:** User may say: "approver email".

### `task_approved_by_name`
- **Description:** Name of the person who approved the task
- **Business Context:** Usually a manager or QA reviewer. User may say: "who approved", "approved by", "reviewer".

### `task_approved_on`
- **Description:** Date when the task was approved/completed
- **Business Context:** NULL if not yet approved. IMPORTANT: This is the "completion date". User may say: "completed date", "finish date", "done date", "completion date", "when was it finished", "when was it done".

### `task_assigned_to_email`
- **Description:** Email of the assigned technician
- **Business Context:** User may say: "tech email", "assigned email", "worker email".

### `task_assigned_to_name`
- **Description:** Name of the technician assigned to this task
- **Business Context:** Can be NULL if unassigned. User may say: "technician", "tech", "worker", "crew member", "field tech", "assigned to", "who worked on".

### `task_cancelled_by_email`
- **Description:** Email of the person who cancelled the task
- **Business Context:** User may say: "canceller email".

### `task_cancelled_by_name`
- **Description:** Name of the person who cancelled the task
- **Business Context:** User may say: "who cancelled", "cancelled by".

### `task_cancelled_on`
- **Description:** Date when the task was cancelled
- **Business Context:** NULL if not cancelled. User may say: "cancellation date", "when was it cancelled".

### `task_did`
- **Description:** Immutable task identifier (unique per task)
- **Business Context:** Firebase-style ID. Uniquely identifies this specific task instance.

### `task_name`
- **Description:** Type of work being performed
- **Business Context:** Standard task types in telecom construction. Synonyms: "antenna alignment" or "antenna test" = AAT, "electrical tilt" or "tilt" = RET, "fiber work" or "fiber optic" = Fiber, "PIM test" or "passive intermodulation" = PIM, "sweep test" or "line sweep" = Sweeps, "photos" or "site photos" = Pictures, "as-built drawings" or "as-built documentation" = As-Builts.

### `task_name_clean`
- **Description:** Normalized/cleaned version of task_name
- **Business Context:** Standardized task type name. Use this for grouping and aggregation instead of task_name.

### `task_scheduled`
- **Description:** Date the task is scheduled to be performed
- **Business Context:** NULL if not yet scheduled. User may say: "scheduled date", "when is it planned", "work date".

### `task_status`
- **Description:** Current status of the task
- **Business Context:** Workflow: pending -> in_progress -> submitted -> approved/rejected. Cancelled is terminal. IMPORTANT SYNONYMS: "completed"/"done"/"finished"/"closed" = approved. "active"/"ongoing"/"started"/"working on" = in_progress. "waiting"/"not started"/"queued"/"backlog" = pending. "sent"/"turned in"/"submitted for review" = submitted. "failed"/"denied" = rejected.

### `task_submitted_by_email`
- **Description:** Email of the person who submitted the task
- **Business Context:** User may say: "submitter email".

### `task_submitted_by_name`
- **Description:** Name of the person who submitted the task
- **Business Context:** Usually the technician. User may say: "who submitted", "submitted by".

### `task_submitted_on`
- **Description:** Date when the task was submitted for review
- **Business Context:** NULL if not yet submitted. User may say: "submission date", "when was it turned in", "submitted date".

## stg_organizations

**Description:** Client organizations that own construction projects

**Business Context:** Each org has projects (stg_projects). Join on org_did. User may say: "clients", "companies", "organizations", "customers", "accounts".

**Related Tables:**
- stg_projects (via org_did)
- stg_user_priorities (via org_did)

### `avc`
- **Description:** AVC code for the organization
- **Business Context:** Internal classification code. User may say: "AVC", "org code", "classification".

### `date_created`
- **Description:** When the organization was created in Swift API
- **Business Context:** Timestamp from the source system. User may say: "when was the client added", "org creation date".

### `last_updated`
- **Description:** Last time organization data was modified in Swift API
- **Business Context:** Source system timestamp. User may say: "last changed", "last modified", "when was it updated".

### `org_did`
- **Description:** Unique organization identifier (primary key)
- **Business Context:** Join to stg_projects.org_did. User may say: "org ID", "organization ID", "client ID".

### `org_name`
- **Description:** Organization name
- **Business Context:** User may say: "client name", "company name", "organization", "customer".

### `poc_email`
- **Description:** Point of contact email
- **Business Context:** User may say: "contact email", "POC email".

### `poc_name`
- **Description:** Point of contact name for this organization
- **Business Context:** Primary contact person. User may say: "contact", "point of contact", "POC", "representative".

## stg_projects

**Description:** Master list of all TECH-OPS construction projects with aggregate metrics

**Business Context:** Projects are contract periods (TS13-TS18) tracking cell tower construction work. Each project contains multiple sites/assets. User may say: "contracts", "programs", "phases", "work orders".

**Related Tables:**
- stg_organizations (via org_did)
- stg_assets (via project_did)
- stg_asset_tasks (via project_did)
- stg_timer_activities (via project_did)
- stg_user_priorities (via project_did)

### `asset_milestone_count`
- **Description:** Number of milestones in this project
- **Business Context:** Project milestones from Swift API.

### `asset_project_count`
- **Description:** Number of sites (cell towers) in this project
- **Business Context:** Each site can have multiple tasks. User may say: "sites", "towers", "locations", "how many sites", "site count".

### `asset_task_approved`
- **Description:** Number of completed/approved tasks in this project
- **Business Context:** Tasks that passed QA review. IMPORTANT: "approved" = completed/done/finished. User may say: "completed tasks", "done tasks", "finished tasks", "closed tasks".

### `asset_task_cancelled`
- **Description:** Number of cancelled tasks in this project
- **Business Context:** Terminal status. User may say: "cancelled tasks", "how many cancelled".

### `asset_task_count`
- **Description:** Total number of tasks across all sites in this project
- **Business Context:** Sum of all task statuses. User may say: "total tasks", "task count", "all tasks", "how many tasks".

### `asset_task_in_progress`
- **Description:** Number of tasks currently being worked on
- **Business Context:** Active work in the field. User may say: "active tasks", "in progress", "currently working on".

### `asset_task_pending`
- **Description:** Number of tasks not yet started
- **Business Context:** Work that has been assigned but not begun. User may say: "backlog", "remaining tasks", "outstanding tasks", "open tasks", "not started".

### `asset_task_rejected`
- **Description:** Number of rejected tasks in this project
- **Business Context:** Tasks that failed QA review. User may say: "rejected tasks", "failed tasks", "how many rejected".

### `asset_task_submitted`
- **Description:** Number of submitted tasks awaiting review
- **Business Context:** Tasks turned in but not yet approved/rejected. User may say: "submitted tasks", "pending review", "awaiting approval".

### `date_created`
- **Description:** When the project was created in Swift API
- **Business Context:** Source system timestamp. User may say: "when did the project start", "project creation date".

### `is_private`
- **Description:** Whether the project is private/restricted
- **Business Context:** Boolean flag from Swift API.

### `last_updated`
- **Description:** Last time project data was modified in Swift API
- **Business Context:** Source system timestamp. User may say: "last changed", "when was it updated".

### `location_orientation`
- **Description:** Geographic region or orientation of the project
- **Business Context:** User may say: "region", "area", "location", "where is the project".

### `metrics_last_updated`
- **Description:** Last time the aggregate task counts were recalculated
- **Business Context:** May differ from last_updated. User may say: "when were the numbers updated", "metrics freshness".

### `org_did`
- **Description:** Organization this project belongs to (foreign key)
- **Business Context:** Join to stg_organizations.org_did. User may say: "which client", "which organization".

### `org_name`
- **Description:** Denormalized organization name
- **Business Context:** Copied from stg_organizations for convenience. Avoids join for simple queries.

### `project_did`
- **Description:** Unique project identifier (primary key)
- **Business Context:** Firebase-style ID, starts with dash

### `project_name`
- **Description:** Human-readable project name
- **Business Context:** Format is "TECH-OPS: TS##" where ## is the contract number (13-18 are active). User may say: "project", "contract", "TS17", "tech-ops 17".

### `project_task_approved`
- **Description:** Number of approved project-level tasks
- **Business Context:** Project management tasks that are completed.

### `project_task_count`
- **Description:** Total project-level tasks (distinct from asset-level tasks)
- **Business Context:** Project management tasks, not field work tasks. Different from asset_task_count.

### `project_task_pending`
- **Description:** Number of pending project-level tasks
- **Business Context:** Project management tasks not yet started.

### `status`
- **Description:** Project status (active/archived)
- **Business Context:** User may say: "is the project active", "project status", "open projects".

## stg_qa_form

**Description:** Quality assurance form responses for completed work

**Business Context:** Each row is a QA checklist item. Multiple rows per site/task. ~344K rows. User may say: "QA", "quality checks", "inspections", "checklists", "quality assurance", "quality control", "QC".

**Related Tables:**
- stg_assets (via asset_did)

### `aat`
- **Description:** Technician name for AAT (Antenna Alignment Test)
- **Business Context:** NULL if AAT was not part of this form. User may say: "AAT tech", "who did the antenna alignment".

### `aat_issues`
- **Description:** Issues found during AAT (Antenna Alignment Test)
- **Business Context:** NULL or empty string if no issues. Contains text description of problems. User may say: "antenna issues", "AAT problems", "alignment issues", "defects".

### `as_builts`
- **Description:** Technician name for as-built documentation
- **Business Context:** NULL if as-builts were not part of this form. User may say: "as-built tech", "who did the as-builts".

### `asset_did`
- **Description:** Immutable Swift API asset identifier, backfilled from stg_assets
- **Business Context:** Stable foreign key to stg_assets â€” unlike site_id (= asset_id) which can change over time. Populated by backfill_asset_did() RPC after each pipeline run.

### `connectivity_testing`
- **Description:** Technician name for connectivity testing
- **Business Context:** NULL if connectivity testing was not part of this form.

### `construction_manager`
- **Description:** Name of the construction manager overseeing the work
- **Business Context:** User may say: "CM", "construction manager", "manager", "who managed".

### `crew_lead`
- **Description:** Name of the crew lead for this work
- **Business Context:** Useful for performance analysis by team. User may say: "team lead", "supervisor", "foreman", "lead tech", "crew leader".

### `fiber`
- **Description:** Technician name for fiber optic work
- **Business Context:** NULL if fiber was not part of this form. User may say: "fiber tech", "who did the fiber".

### `form_id`
- **Description:** Unique identifier for this form submission
- **Business Context:** One form_id can have multiple rows (one per requirement). User may say: "form ID", "submission ID".

### `form_name`
- **Description:** Name of the QA form template
- **Business Context:** Identifies which checklist template was used. User may say: "form type", "which form", "checklist name".

### `live_review_performed`
- **Description:** Whether a live review was performed on-site
- **Business Context:** Yes/No text field. User may say: "live review", "on-site review", "was it reviewed live".

### `optical_power_testing`
- **Description:** Technician name for optical power testing
- **Business Context:** NULL if optical power testing was not part of this form.

### `pictures`
- **Description:** Technician name for site photography
- **Business Context:** NULL if pictures were not part of this form. User may say: "photo tech", "who took pictures".

### `pim`
- **Description:** Technician name for PIM (Passive Intermodulation) testing
- **Business Context:** NULL if PIM was not part of this form. User may say: "PIM tech", "who did the PIM test".

### `pmi`
- **Description:** Technician name for PMI (Preventive Maintenance Inspection)
- **Business Context:** NULL if PMI was not part of this form.

### `power_testing`
- **Description:** Technician name for power testing
- **Business Context:** NULL if power testing was not part of this form.

### `project`
- **Description:** Project name this QA form belongs to
- **Business Context:** Matches project_name in stg_projects. User may say: "project", "contract", "which project".

### `project_number`
- **Description:** TS contract number extracted from project name
- **Business Context:** Integer (e.g., 17 for TS17). User may say: "TS number", "contract number".

### `requirement`
- **Description:** Specific QA checklist requirement text
- **Business Context:** The actual inspection item being checked. User may say: "requirement", "checklist item", "what was checked".

### `requirement_status`
- **Description:** Pass/Fail status for this QA requirement
- **Business Context:** Use for calculating pass rates. Synonyms: "passed"/"good"/"ok" = Pass. "failed"/"bad"/"not passed" = Fail. User may say: "pass rate", "failure rate", "quality score", "success rate".

### `restoration`
- **Description:** Technician name for site restoration work
- **Business Context:** NULL if restoration was not part of this form.

### `ret`
- **Description:** Technician name for RET (Remote Electrical Tilt)
- **Business Context:** NULL if RET was not part of this form. User may say: "RET tech", "who did the tilt".

### `rf_mitigation`
- **Description:** Technician name for RF mitigation work
- **Business Context:** NULL if RF mitigation was not part of this form.

### `site_id`
- **Description:** Site code from the form submission
- **Business Context:** From form submission. Maps to stg_assets.asset_id. User may say: "site ID", "site code".

### `site_name`
- **Description:** Name/address of the site inspected
- **Business Context:** From form submission. Maps to stg_assets.asset_name. User may say: "site name", "location".

### `subcontractor`
- **Description:** Name of the subcontractor company performing the work
- **Business Context:** User may say: "sub", "subcontractor", "contractor", "which company".

### `sweeps`
- **Description:** Technician name for sweep testing
- **Business Context:** NULL if sweeps were not part of this form. User may say: "sweep tech", "who did the sweeps".

### `swift_used_for_photos`
- **Description:** Whether the Swift app was used for taking photos
- **Business Context:** Yes/No text field. User may say: "used Swift for photos", "photo app".

### `task`
- **Description:** Type of work being inspected
- **Business Context:** Same task types as stg_asset_tasks. User may say: "task", "work type", "what was inspected".

### `task_clean`
- **Description:** Normalized/cleaned version of task name
- **Business Context:** Use this for grouping and aggregation instead of task.

## stg_sales_detail

**Description:** Sales detail report from QuickBooks via daily email

**Business Context:** Standalone financial table. Transaction-level sales data with quantities, prices, amounts, and PO numbers by customer.

**Related Tables:** (standalone)

### `amount`
- **Description:** Total line item amount in dollars (qty x sales_price)
- **Business Context:** User may say: "amount", "total", "line total", "how much".

### `as_of_date`
- **Description:** Date of the sales report
- **Business Context:** The report date from QuickBooks. User may say: "report date", "as of date".

### `balance`
- **Description:** Running balance for this transaction
- **Business Context:** User may say: "balance", "remaining".

### `customer`
- **Description:** Customer name from QuickBooks
- **Business Context:** The company or person purchasing. User may say: "customer", "client", "buyer".

### `date`
- **Description:** Transaction date
- **Business Context:** Text field from QuickBooks. User may say: "sale date", "transaction date".

### `email_received_date`
- **Description:** Date the report email was received
- **Business Context:** When the automated email arrived.

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

### `sales_price`
- **Description:** Unit price per item in dollars
- **Business Context:** Price per unit. User may say: "price", "unit price", "rate", "how much each".

### `service_date`
- **Description:** Date the service was performed
- **Business Context:** Text field. May differ from invoice date. User may say: "service date", "work date", "when was it done".

### `transaction_type`
- **Description:** Type of transaction (Invoice, Sales Receipt, etc.)
- **Business Context:** QuickBooks transaction type. User may say: "type", "transaction type".

## stg_timer_activities

**Description:** GPS-tracked time logs for technician site visits

**Business Context:** Incremental load - new data appends. ~11.6K entries. Use for labor analysis. User may say: "time logs", "timesheets", "work hours", "labor hours", "clock-in/clock-out", "time tracking", "hours worked", "time entries".

**Related Tables:**
- stg_assets (via asset_did)
- stg_projects (via project_did)

### `asset_did`
- **Description:** Immutable Swift API asset identifier, backfilled from stg_assets
- **Business Context:** Stable foreign key to stg_assets â€” unlike site_id (= asset_id) which can change over time. Populated by backfill_asset_did() RPC after each pipeline run.

### `duration_min`
- **Description:** Duration of work session in minutes
- **Business Context:** Divide by 60 for hours. Can be used for productivity analysis. User may say: "hours", "time spent", "duration", "how long", "work time".

### `end_date`
- **Description:** Date portion of end_time (derived)
- **Business Context:** Convenience column. May differ from start_date for overnight shifts.

### `end_time`
- **Description:** Clock-out timestamp
- **Business Context:** Timezone is America/New_York. User may say: "clock-out time", "end time", "when did they finish".

### `project`
- **Description:** Project name for this timer entry
- **Business Context:** Matches project_name in stg_projects. User may say: "project", "contract", "which project".

### `project_did`
- **Description:** Project identifier (foreign key)
- **Business Context:** Join to stg_projects.project_did.

### `project_number`
- **Description:** TS contract number extracted from project name
- **Business Context:** Integer (e.g., 17 for TS17). User may say: "TS number", "contract number".

### `run_date`
- **Description:** Date of the pipeline run that loaded this row
- **Business Context:** Used for incremental loading — only fetch rows newer than max run_date.

### `site_id`
- **Description:** Site code from the timer entry
- **Business Context:** From timer API. Maps to stg_assets.asset_id. User may say: "site ID", "site code".

### `site_lat`
- **Description:** Latitude of the cell tower site
- **Business Context:** GPS coordinate. User may say: "site latitude", "tower coordinates".

### `site_long`
- **Description:** Longitude of the cell tower site
- **Business Context:** GPS coordinate. User may say: "site longitude", "tower coordinates".

### `site_name`
- **Description:** Name/address of the site visited
- **Business Context:** From timer API. May not exactly match stg_assets.asset_name. User may say: "site name", "location", "where".

### `site_vs_user_km`
- **Description:** Distance between site GPS and user GPS in kilometers
- **Business Context:** Values > 1.0 km may indicate GPS issues or off-site clock-in. User may say: "GPS distance", "location accuracy", "how far from site", "proximity".

### `start_date`
- **Description:** Date portion of start_time (derived)
- **Business Context:** Convenience column for date-based filtering. User may say: "work date", "which day".

### `start_time`
- **Description:** Clock-in timestamp
- **Business Context:** Timezone is America/New_York. User may say: "clock-in time", "start date", "when did they start", "work date".

### `task`
- **Description:** Type of work performed during this time entry
- **Business Context:** Same task types as stg_asset_tasks. User may say: "task", "work type", "what were they doing".

### `task_clean`
- **Description:** Normalized/cleaned version of task name
- **Business Context:** Use this for grouping and aggregation instead of task.

### `user_accuracy_m`
- **Description:** GPS accuracy of the technician location in meters
- **Business Context:** Lower is better. High values (>100m) may indicate poor GPS signal. User may say: "GPS accuracy", "location precision".

### `user_email`
- **Description:** Email of the technician
- **Business Context:** User may say: "tech email", "worker email".

### `user_lat`
- **Description:** Latitude of the technician when clocking in
- **Business Context:** GPS coordinate from device. User may say: "user location", "tech coordinates", "where was the tech".

### `user_long`
- **Description:** Longitude of the technician when clocking in
- **Business Context:** GPS coordinate from device. User may say: "user location", "tech coordinates".

### `user_name`
- **Description:** Name of the technician
- **Business Context:** Matches task_assigned_to_name in stg_asset_tasks. User may say: "technician", "tech", "worker", "who clocked in", "employee".

### `user_role`
- **Description:** Role of the technician (e.g., field tech, crew lead)
- **Business Context:** User may say: "role", "position", "job title".

## stg_user_priorities

**Description:** Task scheduling and approval workflow data from user priority queues

**Business Context:** Tracks task assignments, scheduling, submissions, approvals, rejections, and cancellations across organizations and projects.

**Related Tables:**
- stg_organizations (via org_did)
- stg_projects (via project_did)
- stg_assets (via asset_did)

### `approved_by`
- **Description:** Name of the person who approved the task
- **Business Context:** User may say: "who approved", "approved by", "reviewer".

### `approved_on`
- **Description:** Date the task was approved
- **Business Context:** User may say: "approval date", "when was it approved", "completion date".

### `asset_did`
- **Description:** Immutable asset identifier (foreign key)
- **Business Context:** Join to stg_assets.asset_did.

### `asset_id`
- **Description:** Human-readable site code (can change)
- **Business Context:** Same as stg_assets.asset_id. User may say: "site ID", "site code".

### `asset_name`
- **Description:** Site name or address (can change)
- **Business Context:** Same as stg_assets.asset_name. User may say: "site name", "tower name", "location".

### `assigned_to`
- **Description:** Name of the technician assigned to this task
- **Business Context:** User may say: "assigned to", "technician", "tech", "worker".

### `calendar_status`
- **Description:** Scheduling status on the calendar
- **Business Context:** User may say: "calendar status", "schedule status".

### `cancelled_by`
- **Description:** Name of the person who cancelled the task
- **Business Context:** User may say: "who cancelled", "cancelled by".

### `cancelled_on`
- **Description:** Date the task was cancelled
- **Business Context:** User may say: "cancellation date", "when was it cancelled".

### `display_date`
- **Description:** Date shown in the priority queue UI
- **Business Context:** May differ from scheduled date. Used for display/sorting purposes.

### `duration`
- **Description:** Estimated task duration in minutes
- **Business Context:** User may say: "estimated time", "how long", "duration".

### `milestone`
- **Description:** Project milestone this task belongs to
- **Business Context:** User may say: "milestone", "phase", "stage".

### `organization`
- **Description:** Denormalized organization name
- **Business Context:** Copied for convenience. User may say: "client", "organization", "company".

### `org_did`
- **Description:** Organization identifier (foreign key)
- **Business Context:** Join to stg_organizations.org_did.

### `pin_type`
- **Description:** Priority pin type (e.g., pinned, unpinned)
- **Business Context:** Controls priority ordering in the queue.

### `project`
- **Description:** Denormalized project name
- **Business Context:** Copied for convenience. User may say: "project", "contract".

### `project_did`
- **Description:** Project identifier (foreign key)
- **Business Context:** Join to stg_projects.project_did.

### `rejected_by`
- **Description:** Name of the person who rejected the task
- **Business Context:** User may say: "who rejected", "rejected by".

### `rejected_on`
- **Description:** Date the task was rejected
- **Business Context:** User may say: "rejection date", "when was it rejected".

### `scheduled`
- **Description:** Date the task is scheduled for
- **Business Context:** User may say: "scheduled date", "when is it planned", "work date".

### `scheduled_by`
- **Description:** Name of the person who scheduled this task
- **Business Context:** User may say: "who scheduled", "scheduled by".

### `status`
- **Description:** Current workflow status of this priority item
- **Business Context:** Similar to task_status in stg_asset_tasks. User may say: "status", "state".

### `submitted_by`
- **Description:** Name of the person who submitted the task
- **Business Context:** User may say: "who submitted", "submitted by".

### `submitted_on`
- **Description:** Date the task was submitted for review
- **Business Context:** User may say: "submission date", "when was it submitted".

### `task_did`
- **Description:** Immutable task identifier
- **Business Context:** Links to stg_asset_tasks.task_did.

### `task_name`
- **Description:** Type of work being performed
- **Business Context:** Same task types as stg_asset_tasks (AAT, RET, Sweeps, etc.). User may say: "task type", "work type".

### `task_name_clean`
- **Description:** Normalized/cleaned version of task_name
- **Business Context:** Use this for grouping and aggregation instead of task_name.
