-- Migration 029: Complete metadata coverage
-- Fills all remaining gaps in agent.schema_metadata
-- All INSERTs use ON CONFLICT DO UPDATE for idempotency

-- ============================================================
-- SECTION A: stg_qa_form — 53 missing columns
-- ============================================================

-- A1: Pipeline internals (3)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_qa_form', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_qa_form', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_qa_form', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- A2: Issue detail fields — discipline *_issues and *_other_issues (30)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_qa_form', 'aat_other_issues',
 'Additional issue details for AAT beyond standard checklist items',
 'Free-text field for non-standard AAT problems found during QA inspection.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'ret_issues',
 'Issues found during RET (Remote Electrical Tilt) inspection',
 'Identifies problems with antenna tilt settings during QA review.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'ret_other_issues',
 'Additional issue details for RET beyond standard checklist items',
 'Free-text field for non-standard RET problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'sweeps_issues',
 'Issues found during sweeps inspection',
 'Identifies problems with RF sweep test results during QA review.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'sweeps_other_issues',
 'Additional issue details for sweeps beyond standard checklist items',
 'Free-text field for non-standard sweep test problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'pim_issues',
 'Issues found during PIM (Passive Intermodulation) inspection',
 'Identifies PIM test failures or concerns during QA review.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'pim_other_issues',
 'Additional issue details for PIM beyond standard checklist items',
 'Free-text field for non-standard PIM problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'fiber_issues',
 'Issues found during fiber inspection',
 'Identifies fiber optic cable or connection problems during QA review.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'fiber_other_issues',
 'Additional issue details for fiber beyond standard checklist items',
 'Free-text field for non-standard fiber problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'pictures_issues',
 'Issues found during pictures/documentation review',
 'Identifies missing or inadequate photographic documentation.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'pictures_other_issues',
 'Additional issue details for pictures beyond standard checklist items',
 'Free-text field for non-standard photo documentation problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'as_builts_issues',
 'Issues found during as-built documentation review',
 'Identifies problems with as-built drawings or documentation.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'as_builts_other_issues',
 'Additional issue details for as-builts beyond standard checklist items',
 'Free-text field for non-standard as-built documentation problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'rf_mitigation_issues',
 'Issues found during RF mitigation inspection',
 'Identifies RF safety or mitigation compliance problems.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'rf_mitigation_other_issues',
 'Additional issue details for RF mitigation beyond standard checklist items',
 'Free-text field for non-standard RF mitigation problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'pmi_issues',
 'Issues found during PMI (Pre-Modification Inspection) review',
 'Identifies problems found during pre-modification structural inspection.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'pmi_other_issues',
 'Additional issue details for PMI beyond standard checklist items',
 'Free-text field for non-standard PMI problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'power_testing_issues',
 'Issues found during power testing inspection',
 'Identifies electrical power test failures or concerns.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'power_testing_other_issues',
 'Additional issue details for power testing beyond standard checklist items',
 'Free-text field for non-standard power testing problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'connectivity_testing_issues',
 'Issues found during connectivity testing inspection',
 'Identifies network connectivity test failures or concerns.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'connectivity_testing_other_issues',
 'Additional issue details for connectivity testing beyond standard checklist items',
 'Free-text field for non-standard connectivity testing problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'optical_power_testing_other_issues',
 'Additional issue details for optical power testing beyond standard checklist items',
 'Free-text field for non-standard optical power test problems. No corresponding _issues column exists.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'na_checklist',
 'N/A checklist inspection status',
 'Indicates items marked as not-applicable during QA review.',
 'TEXT. Pass/Fail/N-A status for not-applicable checklist items.'),

('data_staging', 'stg_qa_form', 'na_checklist_issues',
 'Issues found with N/A checklist items',
 'Identifies problems with items that were marked not-applicable.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'na_checklist_other_issues',
 'Additional issue details for N/A checklist beyond standard items',
 'Free-text field for non-standard N/A checklist problems.',
 'TEXT. NULL if no additional issues.'),

('data_staging', 'stg_qa_form', 'landlord_tower_owner',
 'Landlord/tower owner inspection status',
 'QA discipline covering landlord and tower owner requirements compliance.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'landlord_tower_owner_issues',
 'Issues found during landlord/tower owner inspection',
 'Identifies landlord or tower owner compliance problems.',
 'TEXT. NULL if no issues.'),

('data_staging', 'stg_qa_form', 'permits',
 'Permits inspection status',
 'QA check for required construction or modification permits.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'additional_documents',
 'Additional documents inspection status',
 'QA check for supplemental documentation requirements.',
 'TEXT. Pass/Fail/N-A status.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- A3: PMI sub-fields (6) — pmi_vendor, pmi_others_vendor, pmi_mount_modification_required, pmi_report_received
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_qa_form', 'pmi_vendor',
 'PMI vendor name',
 'Vendor responsible for the Pre-Modification Inspection.',
 'TEXT. Name of the PMI vendor company.'),

('data_staging', 'stg_qa_form', 'pmi_others_vendor',
 'Other PMI vendor details',
 'Additional vendor information when primary vendor field is insufficient.',
 'TEXT. NULL if not applicable.'),

('data_staging', 'stg_qa_form', 'pmi_mount_modification_required',
 'Whether mount modification is required per PMI',
 'Indicates if the PMI inspection determined mount modifications are needed.',
 'TEXT. Yes/No/N-A.'),

('data_staging', 'stg_qa_form', 'pmi_report_received',
 'Whether PMI report has been received',
 'Tracks receipt of the formal PMI report document.',
 'TEXT. Yes/No status.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- A4: Migration 006 additional QA fields (17)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_qa_form', 'rcm_approval',
 'RCM (Regional Construction Manager) approval status',
 'Indicates whether the Regional Construction Manager has approved this QA form.',
 'TEXT. Approval status value.'),

('data_staging', 'stg_qa_form', 'completeness_of_files',
 'Whether documentation files are complete',
 'QA check that all required files and documentation have been submitted.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'sector_photos',
 'Sector antenna photos status',
 'QA check for required sector antenna photographs.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'powershift_photos',
 'Power shift photos status',
 'QA check for required power shift photographs.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'ret_values',
 'RET tilt values recorded',
 'Recorded Remote Electrical Tilt values from antenna inspection.',
 'TEXT. Tilt angle values or status.'),

('data_staging', 'stg_qa_form', 'ret_visibility',
 'RET visibility status',
 'Whether RET settings are visible/accessible during inspection.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'serials',
 'Equipment serial numbers recorded',
 'QA check that equipment serial numbers have been properly documented.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'font_size_of_labels',
 'Label font size compliance',
 'QA check that labels meet required font size standards.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'labels_sector_tape',
 'Sector tape labeling status',
 'QA check for proper sector tape labeling on equipment.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'smart_level',
 'Smart level measurement values',
 'Recorded smart level tool measurements for antenna alignment.',
 'TEXT. Measurement values or Pass/Fail status.'),

('data_staging', 'stg_qa_form', 'calibration_details',
 'Equipment calibration information',
 'QA check that measurement equipment calibration is current and documented.',
 'TEXT. Calibration status or details.'),

('data_staging', 'stg_qa_form', 'general_ground',
 'General ground-level inspection status',
 'QA inspection of ground-level site conditions and equipment.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'conditional_pass',
 'Whether this was a conditional pass',
 'Indicates the QA form received a conditional pass requiring follow-up action.',
 'TEXT. Yes/No or descriptive text.'),

('data_staging', 'stg_qa_form', 'other_landlord_photos',
 'Landlord/tower owner related photos',
 'QA check for photographs required by landlord or tower owner.',
 'TEXT. Pass/Fail/N-A status.'),

('data_staging', 'stg_qa_form', 'signed_pmi_report',
 'Whether PMI report was signed',
 'QA check that the PMI report has proper signatures.',
 'TEXT. Yes/No status.'),

('data_staging', 'stg_qa_form', 'material_packing_signed_pmi',
 'Material packing and signed PMI status',
 'Combined QA check for material packing list and signed PMI documentation.',
 'TEXT. Status value.'),

('data_staging', 'stg_qa_form', 'supports',
 'Structural support inspection status',
 'QA inspection of structural support elements at the site.',
 'TEXT. Pass/Fail/N-A status.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();


-- ============================================================
-- SECTION B: Analytics views — column-level metadata
-- ============================================================

-- B1: v_asset_tasks (19 columns — updates existing or inserts new)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('analytics', 'v_asset_tasks', 'task_did',
 'Unique task identifier from Swift API',
 'Primary key for joining tasks. Use this to uniquely identify an asset task.',
 'TEXT. Sourced from stg_asset_tasks.task_did.'),

('analytics', 'v_asset_tasks', 'task_name_clean',
 'Cleaned task type name',
 'Standardized task name with trailing numbers removed. Use for grouping by task type.',
 'TEXT. Derived from task_name via regex cleanup.'),

('analytics', 'v_asset_tasks', 'task_status',
 'Current workflow status of the task',
 'Filter or group by status. Values: Pending, In Progress, Submitted, Approved, Rejected, Cancelled.',
 'TEXT. Sourced from stg_asset_tasks.task_status.'),

('analytics', 'v_asset_tasks', 'task_scheduled',
 'Scheduled date for the task',
 'When the task is planned to occur. Use for scheduling and timeline analysis.',
 'TIMESTAMPTZ. Sourced from stg_asset_tasks.task_scheduled.'),

('analytics', 'v_asset_tasks', 'task_approved_on',
 'Date the task was approved',
 'When a reviewer approved the completed task. NULL if not yet approved.',
 'TIMESTAMPTZ. Sourced from stg_asset_tasks.task_approved_on.'),

('analytics', 'v_asset_tasks', 'task_submitted_on',
 'Date the task was submitted for review',
 'When the field technician submitted the task. NULL if not yet submitted.',
 'TIMESTAMPTZ. Sourced from stg_asset_tasks.task_submitted_on.'),

('analytics', 'v_asset_tasks', 'task_cancelled_on',
 'Date the task was cancelled',
 'When the task was cancelled. NULL if not cancelled.',
 'TIMESTAMPTZ. Sourced from stg_asset_tasks.task_cancelled_on.'),

('analytics', 'v_asset_tasks', 'task_assigned_to_name',
 'Name of the person assigned to the task',
 'Field technician or team member responsible for completing the task.',
 'TEXT. Resolved from stg_asset_tasks.task_assigned_to_name.'),

('analytics', 'v_asset_tasks', 'task_assigned_to_email',
 'Email of the person assigned to the task',
 'Contact email for the assigned technician.',
 'TEXT. Resolved from stg_asset_tasks.task_assigned_to_email.'),

('analytics', 'v_asset_tasks', 'task_submitted_by_name',
 'Name of the person who submitted the task',
 'Who submitted the completed task for review.',
 'TEXT. Resolved from stg_asset_tasks.task_submitted_by_name.'),

('analytics', 'v_asset_tasks', 'task_approved_by_name',
 'Name of the person who approved the task',
 'Who reviewed and approved the task.',
 'TEXT. Resolved from stg_asset_tasks.task_approved_by_name.'),

('analytics', 'v_asset_tasks', 'task_cancelled_by_name',
 'Name of the person who cancelled the task',
 'Who cancelled the task. NULL if not cancelled.',
 'TEXT. Resolved from stg_asset_tasks.task_cancelled_by_name.'),

('analytics', 'v_asset_tasks', 'asset_did',
 'Unique asset identifier from Swift API',
 'Links task to its parent asset. Join key to stg_assets.',
 'TEXT. Sourced from stg_asset_tasks.asset_did.'),

('analytics', 'v_asset_tasks', 'asset_id',
 'Human-readable asset/site ID (e.g., FA number)',
 'The site identifier used in the field. Often an FA number.',
 'TEXT. Sourced from stg_assets.asset_id.'),

('analytics', 'v_asset_tasks', 'asset_name',
 'Human-readable asset/site name',
 'The site name as displayed in Swift. Use for user-facing labels.',
 'TEXT. Sourced from stg_assets.asset_name.'),

('analytics', 'v_asset_tasks', 'project_did',
 'Unique project identifier from Swift API',
 'Links task to its parent project. Join key to stg_projects.',
 'TEXT. Sourced from stg_asset_tasks.project_did.'),

('analytics', 'v_asset_tasks', 'project_name',
 'Human-readable project name',
 'The project this task belongs to. Use for filtering by project.',
 'TEXT. Resolved from stg_projects.project_name.'),

('analytics', 'v_asset_tasks', 'org_name',
 'Organization name',
 'The carrier/organization that owns this project. Top-level grouping.',
 'TEXT. Resolved from stg_organizations.org_name.'),

('analytics', 'v_asset_tasks', 'carrier_group',
 'Normalized carrier group name',
 'Carrier brand grouping (e.g., T-Mobile, AT&T). Use for carrier-level aggregation.',
 'TEXT. Resolved via carrier_group_lookup table.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- B2: v_timer_activities (20 columns)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('analytics', 'v_timer_activities', 'task_clean',
 'Cleaned task type name',
 'Standardized task name for grouping timer entries by work type.',
 'TEXT. Sourced from stg_timer_activities.task_clean.'),

('analytics', 'v_timer_activities', 'start_time',
 'Timer start timestamp',
 'When the technician started working on this activity.',
 'TIMESTAMPTZ. Sourced from stg_timer_activities.start_time.'),

('analytics', 'v_timer_activities', 'end_time',
 'Timer end timestamp',
 'When the technician stopped working on this activity.',
 'TIMESTAMPTZ. Sourced from stg_timer_activities.end_time.'),

('analytics', 'v_timer_activities', 'duration_min',
 'Duration in minutes',
 'How long the activity lasted. Use for time tracking and productivity analysis.',
 'NUMERIC. Calculated from end_time - start_time.'),

('analytics', 'v_timer_activities', 'user_name',
 'Technician name',
 'Name of the field technician who logged this timer entry.',
 'TEXT. Sourced from stg_timer_activities.user_name.'),

('analytics', 'v_timer_activities', 'user_email',
 'Technician email',
 'Email of the field technician.',
 'TEXT. Sourced from stg_timer_activities.user_email.'),

('analytics', 'v_timer_activities', 'user_role',
 'Technician role',
 'Role of the user in Swift (e.g., Technician, Lead).',
 'TEXT. Sourced from stg_timer_activities.user_role.'),

('analytics', 'v_timer_activities', 'site_lat',
 'Site latitude',
 'GPS latitude of the cell site.',
 'NUMERIC. Sourced from stg_timer_activities.site_lat.'),

('analytics', 'v_timer_activities', 'site_long',
 'Site longitude',
 'GPS longitude of the cell site.',
 'NUMERIC. Sourced from stg_timer_activities.site_long.'),

('analytics', 'v_timer_activities', 'user_lat',
 'User latitude at check-in',
 'GPS latitude of the technician when starting the timer.',
 'NUMERIC. Sourced from stg_timer_activities.user_lat.'),

('analytics', 'v_timer_activities', 'user_long',
 'User longitude at check-in',
 'GPS longitude of the technician when starting the timer.',
 'NUMERIC. Sourced from stg_timer_activities.user_long.'),

('analytics', 'v_timer_activities', 'site_vs_user_km',
 'Distance between site and user in kilometers',
 'How far the technician was from the site when checking in. Use for proximity analysis.',
 'NUMERIC. Calculated via Haversine formula.'),

('analytics', 'v_timer_activities', 'user_accuracy_m',
 'GPS accuracy of user position in meters',
 'Accuracy of the technician GPS reading. Higher values = less precise.',
 'NUMERIC. Sourced from stg_timer_activities.user_accuracy_m.'),

('analytics', 'v_timer_activities', 'start_date',
 'Date portion of start_time',
 'Calendar date the timer started. Use for daily aggregation.',
 'DATE. Derived from start_time.'),

('analytics', 'v_timer_activities', 'end_date',
 'Date portion of end_time',
 'Calendar date the timer ended.',
 'DATE. Derived from end_time.'),

('analytics', 'v_timer_activities', 'asset_did',
 'Unique asset identifier from Swift API',
 'Links timer entry to its parent asset. Backfilled via asset_did process.',
 'TEXT. Resolved via stg_assets join. NULL for admin/overhead time with no site.'),

('analytics', 'v_timer_activities', 'asset_id',
 'Human-readable asset/site ID',
 'The site identifier (e.g., FA number) for the timer entry.',
 'TEXT. Resolved from stg_assets.asset_id.'),

('analytics', 'v_timer_activities', 'asset_name',
 'Human-readable asset/site name',
 'The site name as displayed in Swift.',
 'TEXT. Resolved from stg_assets.asset_name.'),

('analytics', 'v_timer_activities', 'project_did',
 'Unique project identifier from Swift API',
 'Links timer entry to its parent project.',
 'TEXT. Resolved from stg_timer_activities.project_did.'),

('analytics', 'v_timer_activities', 'project_name',
 'Human-readable project name',
 'The project this timer entry belongs to.',
 'TEXT. Resolved from stg_projects.project_name.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- B3: v_qa_forms (18 missing columns)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('analytics', 'v_qa_forms', 'form_name',
 'QA form template name',
 'Name of the QA form template used. Identifies the type of QA inspection.',
 'TEXT. Sourced from stg_qa_form.form_name.'),

('analytics', 'v_qa_forms', 'form_id',
 'Unique form instance identifier',
 'Identifies a specific filled-out QA form instance.',
 'TEXT. Sourced from stg_qa_form.form_id.'),

('analytics', 'v_qa_forms', 'task_clean',
 'Cleaned task type name',
 'Standardized task name associated with this QA form. Use for grouping.',
 'TEXT. Sourced from stg_qa_form.task_clean.'),

('analytics', 'v_qa_forms', 'requirement',
 'QA requirement name',
 'The specific QA requirement being evaluated in this form row.',
 'TEXT. Sourced from stg_qa_form.requirement.'),

('analytics', 'v_qa_forms', 'crew_lead',
 'Crew lead name',
 'Name of the crew lead responsible for the work being inspected.',
 'TEXT. Sourced from stg_qa_form.crew_lead.'),

('analytics', 'v_qa_forms', 'construction_manager',
 'Construction manager name',
 'Construction manager overseeing the project/site.',
 'TEXT. Sourced from stg_qa_form.construction_manager.'),

('analytics', 'v_qa_forms', 'subcontractor',
 'Subcontractor company name',
 'Subcontractor performing the work being inspected.',
 'TEXT. Sourced from stg_qa_form.subcontractor.'),

('analytics', 'v_qa_forms', 'site_id',
 'Original site ID from QA form',
 'Site identifier as entered on the QA form. May not match stg_assets exactly.',
 'TEXT. Sourced from stg_qa_form.site_id.'),

('analytics', 'v_qa_forms', 'site_name',
 'Original site name from QA form',
 'Site name as entered on the QA form.',
 'TEXT. Sourced from stg_qa_form.site_name.'),

('analytics', 'v_qa_forms', 'asset_did',
 'Resolved asset identifier',
 'Links QA form to stg_assets. Backfilled via asset_did matching process.',
 'TEXT. NULL if asset could not be matched (~4% of forms).'),

('analytics', 'v_qa_forms', 'project_name',
 'Human-readable project name',
 'The project this QA form belongs to.',
 'TEXT. Resolved from stg_projects.project_name.'),

('analytics', 'v_qa_forms', 'aat',
 'AAT (Antenna Alignment Test) discipline status',
 'Pass/Fail/N-A result for antenna alignment test QA discipline.',
 'TEXT. Sourced from stg_qa_form.aat.'),

('analytics', 'v_qa_forms', 'ret',
 'RET (Remote Electrical Tilt) discipline status',
 'Pass/Fail/N-A result for RET QA discipline.',
 'TEXT. Sourced from stg_qa_form.ret.'),

('analytics', 'v_qa_forms', 'sweeps',
 'Sweeps discipline status',
 'Pass/Fail/N-A result for RF sweep test QA discipline.',
 'TEXT. Sourced from stg_qa_form.sweeps.'),

('analytics', 'v_qa_forms', 'pim',
 'PIM (Passive Intermodulation) discipline status',
 'Pass/Fail/N-A result for PIM test QA discipline.',
 'TEXT. Sourced from stg_qa_form.pim.'),

('analytics', 'v_qa_forms', 'fiber',
 'Fiber discipline status',
 'Pass/Fail/N-A result for fiber inspection QA discipline.',
 'TEXT. Sourced from stg_qa_form.fiber.'),

('analytics', 'v_qa_forms', 'pictures',
 'Pictures discipline status',
 'Pass/Fail/N-A result for photographic documentation QA discipline.',
 'TEXT. Sourced from stg_qa_form.pictures.'),

('analytics', 'v_qa_forms', 'as_builts',
 'As-builts discipline status',
 'Pass/Fail/N-A result for as-built documentation QA discipline.',
 'TEXT. Sourced from stg_qa_form.as_builts.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- B4: v_user_priorities (25 columns — all missing)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('analytics', 'v_user_priorities', 'task_did',
 'Unique task identifier from Swift API',
 'Primary key for joining user priority tasks. Links to stg_asset_tasks.',
 'TEXT. Sourced from stg_user_priorities.task_did.'),

('analytics', 'v_user_priorities', 'task_name_clean',
 'Cleaned task type name',
 'Standardized task name. Use for grouping priorities by task type.',
 'TEXT. Sourced from stg_user_priorities.task_name_clean.'),

('analytics', 'v_user_priorities', 'status',
 'Current workflow status',
 'Task status in the priority queue. Values: Pending, In Progress, Submitted, Approved, Rejected, Cancelled.',
 'TEXT. Sourced from stg_user_priorities.status.'),

('analytics', 'v_user_priorities', 'milestone',
 'Milestone name',
 'Project milestone this priority task is associated with.',
 'TEXT. Sourced from stg_user_priorities.milestone.'),

('analytics', 'v_user_priorities', 'calendar_status',
 'Calendar scheduling status',
 'Whether this task appears on the scheduling calendar and its status there.',
 'TEXT. Sourced from stg_user_priorities.calendar_status.'),

('analytics', 'v_user_priorities', 'assigned_to',
 'Person assigned to the task',
 'Name of the field technician or team member assigned.',
 'TEXT. Sourced from stg_user_priorities.assigned_to.'),

('analytics', 'v_user_priorities', 'scheduled',
 'Scheduled date',
 'When this priority task is scheduled to be performed.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.scheduled.'),

('analytics', 'v_user_priorities', 'scheduled_by',
 'Person who scheduled the task',
 'Who assigned the schedule date for this task.',
 'TEXT. Sourced from stg_user_priorities.scheduled_by.'),

('analytics', 'v_user_priorities', 'display_date',
 'Display date for UI',
 'Date used for display/sorting in the priority view.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.display_date.'),

('analytics', 'v_user_priorities', 'duration',
 'Expected task duration',
 'Planned duration for this task.',
 'TEXT. Sourced from stg_user_priorities.duration.'),

('analytics', 'v_user_priorities', 'pin_type',
 'Pin type indicator',
 'Type of pin/marker used in the priority view (e.g., location pin type).',
 'TEXT. Sourced from stg_user_priorities.pin_type.'),

('analytics', 'v_user_priorities', 'submitted_by',
 'Person who submitted the task',
 'Who submitted this task for review.',
 'TEXT. Sourced from stg_user_priorities.submitted_by.'),

('analytics', 'v_user_priorities', 'submitted_on',
 'Date task was submitted',
 'When the task was submitted for review.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.submitted_on.'),

('analytics', 'v_user_priorities', 'approved_by',
 'Person who approved the task',
 'Who approved the completed task.',
 'TEXT. Sourced from stg_user_priorities.approved_by.'),

('analytics', 'v_user_priorities', 'approved_on',
 'Date task was approved',
 'When the task was approved.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.approved_on.'),

('analytics', 'v_user_priorities', 'rejected_by',
 'Person who rejected the task',
 'Who rejected the task submission.',
 'TEXT. Sourced from stg_user_priorities.rejected_by.'),

('analytics', 'v_user_priorities', 'rejected_on',
 'Date task was rejected',
 'When the task was rejected.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.rejected_on.'),

('analytics', 'v_user_priorities', 'cancelled_by',
 'Person who cancelled the task',
 'Who cancelled this task.',
 'TEXT. Sourced from stg_user_priorities.cancelled_by.'),

('analytics', 'v_user_priorities', 'cancelled_on',
 'Date task was cancelled',
 'When the task was cancelled.',
 'TIMESTAMPTZ. Sourced from stg_user_priorities.cancelled_on.'),

('analytics', 'v_user_priorities', 'asset_did',
 'Unique asset identifier from Swift API',
 'Links priority task to its parent asset.',
 'TEXT. Resolved via stg_assets join.'),

('analytics', 'v_user_priorities', 'asset_id',
 'Human-readable asset/site ID',
 'The site identifier (e.g., FA number).',
 'TEXT. Resolved from stg_assets.asset_id.'),

('analytics', 'v_user_priorities', 'asset_name',
 'Human-readable asset/site name',
 'The site name as displayed in Swift.',
 'TEXT. Resolved from stg_assets.asset_name.'),

('analytics', 'v_user_priorities', 'project_did',
 'Unique project identifier from Swift API',
 'Links priority task to its parent project.',
 'TEXT. Resolved from stg_user_priorities.project_did.'),

('analytics', 'v_user_priorities', 'project_name',
 'Human-readable project name',
 'The project this priority task belongs to.',
 'TEXT. Resolved from stg_projects.project_name.'),

('analytics', 'v_user_priorities', 'org_name',
 'Organization name',
 'The carrier/organization that owns this project.',
 'TEXT. Resolved from stg_organizations.org_name.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();


-- ============================================================
-- SECTION C: Pipeline internals across staging tables (23)
-- ============================================================

-- C1: stg_asset_tasks pipeline internals (8)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_asset_tasks', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_asset_tasks', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_asset_tasks', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.'),

('data_staging', 'stg_asset_tasks', 'task_approved_by_did',
 'Swift DID of the person who approved the task',
 'Pipeline internal. Not useful for business queries. Use task_approved_by_name instead.',
 'TEXT. Internal Swift user DID.'),

('data_staging', 'stg_asset_tasks', 'task_assigned_to_collection',
 'Raw assignment collection from Swift API',
 'Pipeline internal. Not useful for business queries. Use task_assigned_to_name instead.',
 'TEXT. Raw API collection reference.'),

('data_staging', 'stg_asset_tasks', 'task_assigned_to_did',
 'Swift DID of the person assigned to the task',
 'Pipeline internal. Not useful for business queries. Use task_assigned_to_name instead.',
 'TEXT. Internal Swift user DID.'),

('data_staging', 'stg_asset_tasks', 'task_cancelled_by_did',
 'Swift DID of the person who cancelled the task',
 'Pipeline internal. Not useful for business queries. Use task_cancelled_by_name instead.',
 'TEXT. Internal Swift user DID.'),

('data_staging', 'stg_asset_tasks', 'task_submitted_by_did',
 'Swift DID of the person who submitted the task',
 'Pipeline internal. Not useful for business queries. Use task_submitted_by_name instead.',
 'TEXT. Internal Swift user DID.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C2: stg_assets pipeline internals (3)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_assets', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_assets', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_assets', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C3: stg_organizations pipeline internals (5)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_organizations', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_organizations', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_organizations', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.'),

('data_staging', 'stg_organizations', 'created_by_id',
 'Swift user ID who created this organization',
 'Pipeline internal. Not useful for business queries.',
 'TEXT. Internal Swift user ID.'),

('data_staging', 'stg_organizations', 'poc_id',
 'Point of contact Swift user ID',
 'Pipeline internal. Not useful for business queries. Use poc_name or poc_email instead.',
 'TEXT. Internal Swift user ID.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C4: stg_projects pipeline internals (4)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_projects', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_projects', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_projects', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.'),

('data_staging', 'stg_projects', 'created_by_id',
 'Swift user ID who created this project',
 'Pipeline internal. Not useful for business queries.',
 'TEXT. Internal Swift user ID.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C5: stg_user_priorities pipeline internals (3)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_user_priorities', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_user_priorities', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_user_priorities', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C6: stg_timer_activities pipeline internals (3)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_timer_activities', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY.'),

('data_staging', 'stg_timer_activities', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_timer_activities', 'loaded_at',
 'Timestamp when row was loaded into staging',
 'Pipeline internal. Not useful for business queries.',
 'TIMESTAMPTZ set by the pipeline loader.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();
