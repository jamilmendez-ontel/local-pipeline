-- 153_ref_employees_hr_fields.sql
-- Add HR fields sourced from Ms. Orv's authoritative "Active Employee Information" roster
-- (Google Sheet 1zYiSJ5dERJaFMCto9saTg_O76BFOl75adUkubt4qSFk). These are facts we did not
-- previously capture and cannot derive from other columns:
--   regularization_date   <- "Date Regularized"
--   immediate_supervisor  <- "Immediate Supervisor" (free-text, may list several names)
--   shift_time_in_pht     <- "Time In (PHT)"  (kept as text, e.g. '9:00 PM')
--   shift_time_out_pht    <- "Time Out (PHT)" (kept as text, e.g. '6:00 AM')
-- Note: existing shift_schedule stores a single EST start time; the PHT in/out window is
-- separate authoritative info, so we keep both.

ALTER TABLE reference.ref_employees
  ADD COLUMN IF NOT EXISTS regularization_date  date,
  ADD COLUMN IF NOT EXISTS immediate_supervisor text,
  ADD COLUMN IF NOT EXISTS shift_time_in_pht    text,
  ADD COLUMN IF NOT EXISTS shift_time_out_pht   text;

COMMENT ON COLUMN reference.ref_employees.regularization_date  IS 'Date the employee was regularized (from HR roster "Date Regularized"); NULL if not yet regularized.';
COMMENT ON COLUMN reference.ref_employees.immediate_supervisor IS 'Immediate supervisor(s), free text from HR roster; may list multiple names.';
COMMENT ON COLUMN reference.ref_employees.shift_time_in_pht     IS 'Shift start time in PHT (text, e.g. "9:00 PM") from HR roster.';
COMMENT ON COLUMN reference.ref_employees.shift_time_out_pht    IS 'Shift end time in PHT (text, e.g. "6:00 AM") from HR roster.';
