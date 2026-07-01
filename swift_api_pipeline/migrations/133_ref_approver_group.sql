-- 133: reference.ref_approver_group — maps each Swift daily-report approver group
-- (data_staging.stg_daily_reports.assigned_approver) to a clean display label and a
-- carrier_group for employee headcount. Feeds analytics.approver_scorecard (migration 134).
-- include=false hides junk / duplicate / low-signal labels from the scorecard.

CREATE TABLE IF NOT EXISTS reference.ref_approver_group (
  group_label   text PRIMARY KEY,
  display_label text NOT NULL,
  carrier_group text,          -- matches reference.ref_employees.carrier_group; NULL = no headcount
  include       boolean NOT NULL DEFAULT true,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reference.ref_approver_group ENABLE ROW LEVEL SECURITY;
-- deny-all: no policies => only owner / service_role (BYPASSRLS) can read. anon/authenticated
-- already lack USAGE on reference; this is belt-and-suspenders.

INSERT INTO reference.ref_approver_group (group_label, display_label, carrier_group, include) VALUES
  ('Daily Report Approvers - CG1',                                   'CG1 · Verizon',                       'CG1 - Verizon',   true),
  ('Daily Report Approvers - CG2',                                   'CG2 · AT&T / DISH',                   'CG2 - AT&T/DISH', true),
  ('Daily Report Approvers - CG3',                                   'CG3 · TMO / USCC',                    'CG3 - TMO/USCC',  true),
  ('Daily Report Approvers - Quality and Process Improvement (QPI)', 'Quality & Process Improvement (QPI)', 'QPI',             true),
  ('Daily Report Approvers - Tools And Automation',                  'Tools & Automation',                  'Tools&Auto',      true),
  ('Daily Report Approvers - Accounting',                            'Accounting',                          'Accounting',      true),
  ('Daily Report Approvers - Data Analysis and Reporting',           'Data Analysis & Reporting',           'DA',              true),
  ('Daily Report Approvers - Research and Development',              'Research & Development',              'Research',        true),
  ('Daily Report Approvers - HR and Administration',                 'HR & Administration',                 'HR',              true),
  ('Daily Report Approvers - Software Development',                  'Software Development',                NULL,              true),
  ('Daily Report Approvers - All Project Associates (PASs / PCs)',   'All Project Associates (PASs / PCs)', NULL,              true),
  ('(no approver assigned)',                                         '(No approver assigned)',              NULL,              true),
  -- stray / duplicate / low-signal labels: retained for traceability, hidden from the scorecard
  ('Quality and Process Improvement (QPI)',                          'QPI (stray label)',                  NULL,              false),
  ('CG3 - TMO/USSC Team',                                            'CG3 (stray label)',                  NULL,              false),
  ('CG2 - AT&T/DISH Team',                                           'CG2 (stray label)',                  NULL,              false),
  ('VZ2 - CG1 - Verizon Team',                                       'VZ2 (stray label)',                  NULL,              false),
  ('Daily Report Approvers - Epsilon Cluster',                       'Epsilon Cluster (stray)',            NULL,              false),
  ('Delta Cluster  Leads',                                           'Delta Cluster Leads (stray)',        NULL,              false)
ON CONFLICT (group_label) DO UPDATE
  SET display_label = EXCLUDED.display_label,
      carrier_group = EXCLUDED.carrier_group,
      include       = EXCLUDED.include,
      updated_at    = now();

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('reference','ref_approver_group',
   'Maps each Swift daily-report approver group (assigned_approver) to a clean display label and a carrier_group for headcount; include=false hides junk/duplicate labels.',
   'Feeds the HR approver scorecard: friendly group names, employee counts, and suppression of stray/duplicate Swift labels.',
   ARRAY['data_staging.stg_daily_reports','reference.ref_employees','analytics.approver_scorecard'])
ON CONFLICT DO NOTHING;
