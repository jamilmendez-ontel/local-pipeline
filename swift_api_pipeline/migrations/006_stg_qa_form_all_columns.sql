-- migrations/006_stg_qa_form_all_columns.sql
-- Add all missing columns to stg_qa_form

-- Construction & Personnel
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS construction_manager TEXT;
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS subcontractor TEXT;

-- RCM
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS rcm_approval TEXT;

-- Completeness
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS completeness_of_files TEXT;

-- Sector & Photos
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS sector_photos TEXT;
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS powershift_photos TEXT;

-- RET additional
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS ret_values TEXT;
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS ret_visibility TEXT;

-- Serials & Labels
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS serials TEXT;
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS font_size_of_labels TEXT;
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS labels_sector_tape TEXT;

-- Smart Level
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS smart_level TEXT;

-- Calibration
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS calibration_details TEXT;

-- General Ground
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS general_ground TEXT;

-- Conditional Pass
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS conditional_pass TEXT;

-- Other landlord photos
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS other_landlord_photos TEXT;

-- Signed PMI Report
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS signed_pmi_report TEXT;

-- Material Packing
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS material_packing_signed_pmi TEXT;

-- Supports
ALTER TABLE stg_qa_form ADD COLUMN IF NOT EXISTS supports TEXT;
