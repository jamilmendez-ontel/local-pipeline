-- Migration 020: Add column-level metadata for all staging tables
-- Skips pipeline internals: id, run_id, loaded_at, created_by_id, *_by_did, *_collection, poc_id

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES

-- ============================================================
-- stg_organizations (already has: org_did, org_name, avc, poc_name, poc_email)
-- ============================================================
('data_staging', 'stg_organizations', 'date_created',
 'When the organization was created in Swift API',
 'Timestamp from the source system. User may say: "when was the client added", "org creation date".'),

('data_staging', 'stg_organizations', 'last_updated',
 'Last time organization data was modified in Swift API',
 'Source system timestamp. User may say: "last changed", "last modified", "when was it updated".'),

-- ============================================================
-- stg_projects (already has: project_did, project_name, asset_project_count,
--   asset_task_approved, asset_task_count, asset_task_pending)
-- ============================================================
('data_staging', 'stg_projects', 'org_did',
 'Organization this project belongs to (foreign key)',
 'Join to stg_organizations.org_did. User may say: "which client", "which organization".'),

('data_staging', 'stg_projects', 'org_name',
 'Denormalized organization name',
 'Copied from stg_organizations for convenience. Avoids join for simple queries.'),

('data_staging', 'stg_projects', 'status',
 'Project status (active/archived)',
 'User may say: "is the project active", "project status", "open projects".'),

('data_staging', 'stg_projects', 'is_private',
 'Whether the project is private/restricted',
 'Boolean flag from Swift API.'),

('data_staging', 'stg_projects', 'location_orientation',
 'Geographic region or orientation of the project',
 'User may say: "region", "area", "location", "where is the project".'),

('data_staging', 'stg_projects', 'asset_task_rejected',
 'Number of rejected tasks in this project',
 'Tasks that failed QA review. User may say: "rejected tasks", "failed tasks", "how many rejected".'),

('data_staging', 'stg_projects', 'asset_task_cancelled',
 'Number of cancelled tasks in this project',
 'Terminal status. User may say: "cancelled tasks", "how many cancelled".'),

('data_staging', 'stg_projects', 'asset_task_submitted',
 'Number of submitted tasks awaiting review',
 'Tasks turned in but not yet approved/rejected. User may say: "submitted tasks", "pending review", "awaiting approval".'),

('data_staging', 'stg_projects', 'asset_task_in_progress',
 'Number of tasks currently being worked on',
 'Active work in the field. User may say: "active tasks", "in progress", "currently working on".'),

('data_staging', 'stg_projects', 'asset_milestone_count',
 'Number of milestones in this project',
 'Project milestones from Swift API.'),

('data_staging', 'stg_projects', 'project_task_count',
 'Total project-level tasks (distinct from asset-level tasks)',
 'Project management tasks, not field work tasks. Different from asset_task_count.'),

('data_staging', 'stg_projects', 'project_task_pending',
 'Number of pending project-level tasks',
 'Project management tasks not yet started.'),

('data_staging', 'stg_projects', 'project_task_approved',
 'Number of approved project-level tasks',
 'Project management tasks that are completed.'),

('data_staging', 'stg_projects', 'date_created',
 'When the project was created in Swift API',
 'Source system timestamp. User may say: "when did the project start", "project creation date".'),

('data_staging', 'stg_projects', 'last_updated',
 'Last time project data was modified in Swift API',
 'Source system timestamp. User may say: "last changed", "when was it updated".'),

('data_staging', 'stg_projects', 'metrics_last_updated',
 'Last time the aggregate task counts were recalculated',
 'May differ from last_updated. User may say: "when were the numbers updated", "metrics freshness".'),

-- ============================================================
-- stg_assets (no column-level metadata yet)
-- ============================================================
('data_staging', 'stg_assets', 'project_did',
 'Project this asset belongs to (foreign key)',
 'Join to stg_projects.project_did. Each asset belongs to exactly one project.'),

('data_staging', 'stg_assets', 'asset_did',
 'Immutable Swift API asset identifier',
 'Never changes even if asset_id or asset_name change. Primary join key for timer, QA form, user priorities. User may say: "asset ID", "site identifier".'),

('data_staging', 'stg_assets', 'asset_id',
 'Human-readable site code (can change over time)',
 'Carrier-assigned code like "ATL001". IMPORTANT: Can be renamed — use asset_did for stable joins. User may say: "site ID", "site code", "FA code", "tower ID".'),

('data_staging', 'stg_assets', 'asset_name',
 'Site name or address (can change over time)',
 'Usually includes address or landmark. IMPORTANT: Can be renamed — use asset_did for stable joins. User may say: "site name", "tower name", "location".'),

('data_staging', 'stg_assets', 'task_count',
 'Total number of tasks at this site',
 'Sum of all task statuses. User may say: "how many tasks", "total tasks at this site".'),

('data_staging', 'stg_assets', 'requirement_count',
 'Total QA requirements across all tasks at this site',
 'Number of QA checklist items. User may say: "how many requirements", "QA items".'),

('data_staging', 'stg_assets', 'tasks_pending',
 'Number of pending tasks at this site',
 'Not yet started. User may say: "pending", "not started", "backlog".'),

('data_staging', 'stg_assets', 'tasks_in_progress',
 'Number of in-progress tasks at this site',
 'Currently being worked on. User may say: "active", "in progress", "working on".'),

('data_staging', 'stg_assets', 'tasks_submitted',
 'Number of submitted tasks awaiting review at this site',
 'Turned in but not yet approved. User may say: "submitted", "pending review".'),

('data_staging', 'stg_assets', 'tasks_approved',
 'Number of approved/completed tasks at this site',
 'Passed QA review. User may say: "completed", "done", "finished", "approved".'),

('data_staging', 'stg_assets', 'tasks_rejected',
 'Number of rejected tasks at this site',
 'Failed QA review, needs rework. User may say: "rejected", "failed".'),

('data_staging', 'stg_assets', 'tasks_cancelled',
 'Number of cancelled tasks at this site',
 'Terminal status. User may say: "cancelled", "removed".'),

-- ============================================================
-- stg_asset_tasks (already has: asset_id, asset_name, task_approved_on,
--   task_assigned_to_name, task_name, task_status)
-- ============================================================
('data_staging', 'stg_asset_tasks', 'project_did',
 'Project this task belongs to (foreign key)',
 'Join to stg_projects.project_did. Filter by this for project-level queries — much faster on 2.2M rows.'),

('data_staging', 'stg_asset_tasks', 'project_status',
 'Status of the parent project at time of extraction',
 'Denormalized from stg_projects. User may say: "is the project active".'),

('data_staging', 'stg_asset_tasks', 'asset_did',
 'Immutable asset identifier (foreign key)',
 'Join to stg_assets.asset_did. More stable than asset_id which can change.'),

('data_staging', 'stg_asset_tasks', 'task_did',
 'Immutable task identifier (unique per task)',
 'Firebase-style ID. Uniquely identifies this specific task instance.'),

('data_staging', 'stg_asset_tasks', 'asset_requirement_count',
 'Number of QA requirements for this asset',
 'QA checklist items that must pass. User may say: "requirements", "QA items", "checklist count".'),

('data_staging', 'stg_asset_tasks', 'task_name_clean',
 'Normalized/cleaned version of task_name',
 'Standardized task type name. Use this for grouping and aggregation instead of task_name.'),

('data_staging', 'stg_asset_tasks', 'task_scheduled',
 'Date the task is scheduled to be performed',
 'NULL if not yet scheduled. User may say: "scheduled date", "when is it planned", "work date".'),

('data_staging', 'stg_asset_tasks', 'task_assigned_to_email',
 'Email of the assigned technician',
 'User may say: "tech email", "assigned email", "worker email".'),

('data_staging', 'stg_asset_tasks', 'task_submitted_on',
 'Date when the task was submitted for review',
 'NULL if not yet submitted. User may say: "submission date", "when was it turned in", "submitted date".'),

('data_staging', 'stg_asset_tasks', 'task_submitted_by_name',
 'Name of the person who submitted the task',
 'Usually the technician. User may say: "who submitted", "submitted by".'),

('data_staging', 'stg_asset_tasks', 'task_submitted_by_email',
 'Email of the person who submitted the task',
 'User may say: "submitter email".'),

('data_staging', 'stg_asset_tasks', 'task_approved_by_name',
 'Name of the person who approved the task',
 'Usually a manager or QA reviewer. User may say: "who approved", "approved by", "reviewer".'),

('data_staging', 'stg_asset_tasks', 'task_approved_by_email',
 'Email of the person who approved the task',
 'User may say: "approver email".'),

('data_staging', 'stg_asset_tasks', 'task_cancelled_on',
 'Date when the task was cancelled',
 'NULL if not cancelled. User may say: "cancellation date", "when was it cancelled".'),

('data_staging', 'stg_asset_tasks', 'task_cancelled_by_name',
 'Name of the person who cancelled the task',
 'User may say: "who cancelled", "cancelled by".'),

('data_staging', 'stg_asset_tasks', 'task_cancelled_by_email',
 'Email of the person who cancelled the task',
 'User may say: "canceller email".'),

-- ============================================================
-- stg_user_priorities (no column-level metadata yet)
-- ============================================================
('data_staging', 'stg_user_priorities', 'task_did',
 'Immutable task identifier',
 'Links to stg_asset_tasks.task_did.'),

('data_staging', 'stg_user_priorities', 'asset_did',
 'Immutable asset identifier (foreign key)',
 'Join to stg_assets.asset_did.'),

('data_staging', 'stg_user_priorities', 'org_did',
 'Organization identifier (foreign key)',
 'Join to stg_organizations.org_did.'),

('data_staging', 'stg_user_priorities', 'project_did',
 'Project identifier (foreign key)',
 'Join to stg_projects.project_did.'),

('data_staging', 'stg_user_priorities', 'task_name',
 'Type of work being performed',
 'Same task types as stg_asset_tasks (AAT, RET, Sweeps, etc.). User may say: "task type", "work type".'),

('data_staging', 'stg_user_priorities', 'task_name_clean',
 'Normalized/cleaned version of task_name',
 'Use this for grouping and aggregation instead of task_name.'),

('data_staging', 'stg_user_priorities', 'milestone',
 'Project milestone this task belongs to',
 'User may say: "milestone", "phase", "stage".'),

('data_staging', 'stg_user_priorities', 'status',
 'Current workflow status of this priority item',
 'Similar to task_status in stg_asset_tasks. User may say: "status", "state".'),

('data_staging', 'stg_user_priorities', 'calendar_status',
 'Scheduling status on the calendar',
 'User may say: "calendar status", "schedule status".'),

('data_staging', 'stg_user_priorities', 'assigned_to',
 'Name of the technician assigned to this task',
 'User may say: "assigned to", "technician", "tech", "worker".'),

('data_staging', 'stg_user_priorities', 'scheduled',
 'Date the task is scheduled for',
 'User may say: "scheduled date", "when is it planned", "work date".'),

('data_staging', 'stg_user_priorities', 'scheduled_by',
 'Name of the person who scheduled this task',
 'User may say: "who scheduled", "scheduled by".'),

('data_staging', 'stg_user_priorities', 'display_date',
 'Date shown in the priority queue UI',
 'May differ from scheduled date. Used for display/sorting purposes.'),

('data_staging', 'stg_user_priorities', 'duration',
 'Estimated task duration in minutes',
 'User may say: "estimated time", "how long", "duration".'),

('data_staging', 'stg_user_priorities', 'pin_type',
 'Priority pin type (e.g., pinned, unpinned)',
 'Controls priority ordering in the queue.'),

('data_staging', 'stg_user_priorities', 'submitted_by',
 'Name of the person who submitted the task',
 'User may say: "who submitted", "submitted by".'),

('data_staging', 'stg_user_priorities', 'submitted_on',
 'Date the task was submitted for review',
 'User may say: "submission date", "when was it submitted".'),

('data_staging', 'stg_user_priorities', 'approved_by',
 'Name of the person who approved the task',
 'User may say: "who approved", "approved by", "reviewer".'),

('data_staging', 'stg_user_priorities', 'approved_on',
 'Date the task was approved',
 'User may say: "approval date", "when was it approved", "completion date".'),

('data_staging', 'stg_user_priorities', 'rejected_by',
 'Name of the person who rejected the task',
 'User may say: "who rejected", "rejected by".'),

('data_staging', 'stg_user_priorities', 'rejected_on',
 'Date the task was rejected',
 'User may say: "rejection date", "when was it rejected".'),

('data_staging', 'stg_user_priorities', 'cancelled_by',
 'Name of the person who cancelled the task',
 'User may say: "who cancelled", "cancelled by".'),

('data_staging', 'stg_user_priorities', 'cancelled_on',
 'Date the task was cancelled',
 'User may say: "cancellation date", "when was it cancelled".'),

('data_staging', 'stg_user_priorities', 'organization',
 'Denormalized organization name',
 'Copied for convenience. User may say: "client", "organization", "company".'),

('data_staging', 'stg_user_priorities', 'project',
 'Denormalized project name',
 'Copied for convenience. User may say: "project", "contract".'),

('data_staging', 'stg_user_priorities', 'asset_id',
 'Human-readable site code (can change)',
 'Same as stg_assets.asset_id. User may say: "site ID", "site code".'),

('data_staging', 'stg_user_priorities', 'asset_name',
 'Site name or address (can change)',
 'Same as stg_assets.asset_name. User may say: "site name", "tower name", "location".'),

-- ============================================================
-- stg_timer_activities (already has: asset_did, duration_min, site_vs_user_km,
--   start_time, user_name)
-- ============================================================
('data_staging', 'stg_timer_activities', 'project',
 'Project name for this timer entry',
 'Matches project_name in stg_projects. User may say: "project", "contract", "which project".'),

('data_staging', 'stg_timer_activities', 'project_number',
 'TS contract number extracted from project name',
 'Integer (e.g., 17 for TS17). User may say: "TS number", "contract number".'),

('data_staging', 'stg_timer_activities', 'project_did',
 'Project identifier (foreign key)',
 'Join to stg_projects.project_did.'),

('data_staging', 'stg_timer_activities', 'site_name',
 'Name/address of the site visited',
 'From timer API. May not exactly match stg_assets.asset_name. User may say: "site name", "location", "where".'),

('data_staging', 'stg_timer_activities', 'site_id',
 'Site code from the timer entry',
 'From timer API. Maps to stg_assets.asset_id. User may say: "site ID", "site code".'),

('data_staging', 'stg_timer_activities', 'task',
 'Type of work performed during this time entry',
 'Same task types as stg_asset_tasks. User may say: "task", "work type", "what were they doing".'),

('data_staging', 'stg_timer_activities', 'task_clean',
 'Normalized/cleaned version of task name',
 'Use this for grouping and aggregation instead of task.'),

('data_staging', 'stg_timer_activities', 'site_lat',
 'Latitude of the cell tower site',
 'GPS coordinate. User may say: "site latitude", "tower coordinates".'),

('data_staging', 'stg_timer_activities', 'site_long',
 'Longitude of the cell tower site',
 'GPS coordinate. User may say: "site longitude", "tower coordinates".'),

('data_staging', 'stg_timer_activities', 'user_lat',
 'Latitude of the technician when clocking in',
 'GPS coordinate from device. User may say: "user location", "tech coordinates", "where was the tech".'),

('data_staging', 'stg_timer_activities', 'user_long',
 'Longitude of the technician when clocking in',
 'GPS coordinate from device. User may say: "user location", "tech coordinates".'),

('data_staging', 'stg_timer_activities', 'user_accuracy_m',
 'GPS accuracy of the technician location in meters',
 'Lower is better. High values (>100m) may indicate poor GPS signal. User may say: "GPS accuracy", "location precision".'),

('data_staging', 'stg_timer_activities', 'end_time',
 'Clock-out timestamp',
 'Timezone is America/New_York. User may say: "clock-out time", "end time", "when did they finish".'),

('data_staging', 'stg_timer_activities', 'user_email',
 'Email of the technician',
 'User may say: "tech email", "worker email".'),

('data_staging', 'stg_timer_activities', 'user_role',
 'Role of the technician (e.g., field tech, crew lead)',
 'User may say: "role", "position", "job title".'),

('data_staging', 'stg_timer_activities', 'run_date',
 'Date of the pipeline run that loaded this row',
 'Used for incremental loading — only fetch rows newer than max run_date.'),

('data_staging', 'stg_timer_activities', 'start_date',
 'Date portion of start_time (derived)',
 'Convenience column for date-based filtering. User may say: "work date", "which day".'),

('data_staging', 'stg_timer_activities', 'end_date',
 'Date portion of end_time (derived)',
 'Convenience column. May differ from start_date for overnight shifts.'),

-- ============================================================
-- stg_qa_form (already has: aat_issues, asset_did, crew_lead, project, requirement_status)
-- ============================================================
('data_staging', 'stg_qa_form', 'form_name',
 'Name of the QA form template',
 'Identifies which checklist template was used. User may say: "form type", "which form", "checklist name".'),

('data_staging', 'stg_qa_form', 'form_id',
 'Unique identifier for this form submission',
 'One form_id can have multiple rows (one per requirement). User may say: "form ID", "submission ID".'),

('data_staging', 'stg_qa_form', 'project_number',
 'TS contract number extracted from project name',
 'Integer (e.g., 17 for TS17). User may say: "TS number", "contract number".'),

('data_staging', 'stg_qa_form', 'site_name',
 'Name/address of the site inspected',
 'From form submission. Maps to stg_assets.asset_name. User may say: "site name", "location".'),

('data_staging', 'stg_qa_form', 'site_id',
 'Site code from the form submission',
 'From form submission. Maps to stg_assets.asset_id. User may say: "site ID", "site code".'),

('data_staging', 'stg_qa_form', 'task',
 'Type of work being inspected',
 'Same task types as stg_asset_tasks. User may say: "task", "work type", "what was inspected".'),

('data_staging', 'stg_qa_form', 'task_clean',
 'Normalized/cleaned version of task name',
 'Use this for grouping and aggregation instead of task.'),

('data_staging', 'stg_qa_form', 'requirement',
 'Specific QA checklist requirement text',
 'The actual inspection item being checked. User may say: "requirement", "checklist item", "what was checked".'),

('data_staging', 'stg_qa_form', 'live_review_performed',
 'Whether a live review was performed on-site',
 'Yes/No text field. User may say: "live review", "on-site review", "was it reviewed live".'),

('data_staging', 'stg_qa_form', 'swift_used_for_photos',
 'Whether the Swift app was used for taking photos',
 'Yes/No text field. User may say: "used Swift for photos", "photo app".'),

('data_staging', 'stg_qa_form', 'construction_manager',
 'Name of the construction manager overseeing the work',
 'User may say: "CM", "construction manager", "manager", "who managed".'),

('data_staging', 'stg_qa_form', 'subcontractor',
 'Name of the subcontractor company performing the work',
 'User may say: "sub", "subcontractor", "contractor", "which company".'),

('data_staging', 'stg_qa_form', 'aat',
 'Technician name for AAT (Antenna Alignment Test)',
 'NULL if AAT was not part of this form. User may say: "AAT tech", "who did the antenna alignment".'),

('data_staging', 'stg_qa_form', 'ret',
 'Technician name for RET (Remote Electrical Tilt)',
 'NULL if RET was not part of this form. User may say: "RET tech", "who did the tilt".'),

('data_staging', 'stg_qa_form', 'sweeps',
 'Technician name for sweep testing',
 'NULL if sweeps were not part of this form. User may say: "sweep tech", "who did the sweeps".'),

('data_staging', 'stg_qa_form', 'pim',
 'Technician name for PIM (Passive Intermodulation) testing',
 'NULL if PIM was not part of this form. User may say: "PIM tech", "who did the PIM test".'),

('data_staging', 'stg_qa_form', 'fiber',
 'Technician name for fiber optic work',
 'NULL if fiber was not part of this form. User may say: "fiber tech", "who did the fiber".'),

('data_staging', 'stg_qa_form', 'pictures',
 'Technician name for site photography',
 'NULL if pictures were not part of this form. User may say: "photo tech", "who took pictures".'),

('data_staging', 'stg_qa_form', 'as_builts',
 'Technician name for as-built documentation',
 'NULL if as-builts were not part of this form. User may say: "as-built tech", "who did the as-builts".'),

('data_staging', 'stg_qa_form', 'rf_mitigation',
 'Technician name for RF mitigation work',
 'NULL if RF mitigation was not part of this form.'),

('data_staging', 'stg_qa_form', 'pmi',
 'Technician name for PMI (Preventive Maintenance Inspection)',
 'NULL if PMI was not part of this form.'),

('data_staging', 'stg_qa_form', 'power_testing',
 'Technician name for power testing',
 'NULL if power testing was not part of this form.'),

('data_staging', 'stg_qa_form', 'connectivity_testing',
 'Technician name for connectivity testing',
 'NULL if connectivity testing was not part of this form.'),

('data_staging', 'stg_qa_form', 'optical_power_testing',
 'Technician name for optical power testing',
 'NULL if optical power testing was not part of this form.'),

('data_staging', 'stg_qa_form', 'restoration',
 'Technician name for site restoration work',
 'NULL if restoration was not part of this form.'),

-- ============================================================
-- stg_ar_aging (no column-level metadata yet)
-- ============================================================
('data_staging', 'stg_ar_aging', 'as_of_date',
 'Date of the aging report snapshot',
 'The report date from QuickBooks. User may say: "report date", "as of", "snapshot date".'),

('data_staging', 'stg_ar_aging', 'email_received_date',
 'Date the report email was received',
 'When the automated email arrived. User may say: "email date", "received date".'),

('data_staging', 'stg_ar_aging', 'aging_bucket',
 'Aging category (Current, 1-30, 31-60, 61-90, 91+)',
 'How overdue the invoice is. User may say: "aging bucket", "how old", "overdue category", "days past due".'),

('data_staging', 'stg_ar_aging', 'date',
 'Transaction/invoice date',
 'Text field from QuickBooks. User may say: "invoice date", "transaction date".'),

('data_staging', 'stg_ar_aging', 'transaction_type',
 'Type of transaction (Invoice, Payment, Credit Memo, etc.)',
 'QuickBooks transaction type. User may say: "type", "transaction type", "invoice or payment".'),

('data_staging', 'stg_ar_aging', 'num',
 'Invoice or transaction number',
 'QuickBooks reference number. User may say: "invoice number", "invoice #", "transaction number".'),

('data_staging', 'stg_ar_aging', 'customer',
 'Customer name from QuickBooks',
 'The company or person being billed. User may say: "customer", "client", "who owes", "billed to".'),

('data_staging', 'stg_ar_aging', 'location',
 'QuickBooks location/class for the transaction',
 'User may say: "location", "class", "department".'),

('data_staging', 'stg_ar_aging', 'due_date',
 'Payment due date for the invoice',
 'Text field. User may say: "due date", "when is it due", "payment deadline".'),

('data_staging', 'stg_ar_aging', 'amount',
 'Original transaction amount in dollars',
 'Full invoice amount. User may say: "amount", "invoice amount", "how much".'),

('data_staging', 'stg_ar_aging', 'open_balance',
 'Remaining unpaid balance in dollars',
 'Amount still owed. User may say: "open balance", "outstanding", "unpaid", "balance due", "how much is owed".'),

('data_staging', 'stg_ar_aging', 'past_due',
 'Amount past due in dollars',
 'Portion of open_balance that is overdue. User may say: "past due", "overdue amount", "late balance".'),

('data_staging', 'stg_ar_aging', 'po_number',
 'Purchase order number',
 'Customer PO reference. User may say: "PO", "PO number", "purchase order".'),

-- ============================================================
-- stg_sales_detail (no column-level metadata yet)
-- ============================================================
('data_staging', 'stg_sales_detail', 'as_of_date',
 'Date of the sales report',
 'The report date from QuickBooks. User may say: "report date", "as of date".'),

('data_staging', 'stg_sales_detail', 'email_received_date',
 'Date the report email was received',
 'When the automated email arrived.'),

('data_staging', 'stg_sales_detail', 'date',
 'Transaction date',
 'Text field from QuickBooks. User may say: "sale date", "transaction date".'),

('data_staging', 'stg_sales_detail', 'transaction_type',
 'Type of transaction (Invoice, Sales Receipt, etc.)',
 'QuickBooks transaction type. User may say: "type", "transaction type".'),

('data_staging', 'stg_sales_detail', 'num',
 'Invoice or transaction number',
 'QuickBooks reference number. User may say: "invoice number", "transaction number".'),

('data_staging', 'stg_sales_detail', 'customer',
 'Customer name from QuickBooks',
 'The company or person purchasing. User may say: "customer", "client", "buyer".'),

('data_staging', 'stg_sales_detail', 'memo_description',
 'Line item description or memo',
 'Details about what was sold/billed. User may say: "description", "memo", "what was sold", "line item".'),

('data_staging', 'stg_sales_detail', 'qty',
 'Quantity of items/units sold',
 'User may say: "quantity", "how many", "units".'),

('data_staging', 'stg_sales_detail', 'sales_price',
 'Unit price per item in dollars',
 'Price per unit. User may say: "price", "unit price", "rate", "how much each".'),

('data_staging', 'stg_sales_detail', 'amount',
 'Total line item amount in dollars (qty x sales_price)',
 'User may say: "amount", "total", "line total", "how much".'),

('data_staging', 'stg_sales_detail', 'balance',
 'Running balance for this transaction',
 'User may say: "balance", "remaining".'),

('data_staging', 'stg_sales_detail', 'po_number',
 'Purchase order number',
 'Customer PO reference. User may say: "PO", "PO number", "purchase order".'),

('data_staging', 'stg_sales_detail', 'service_date',
 'Date the service was performed',
 'Text field. May differ from invoice date. User may say: "service date", "work date", "when was it done".')

ON CONFLICT DO NOTHING;
