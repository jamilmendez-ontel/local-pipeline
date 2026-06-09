-- 075_ref_quote_directory.sql
-- Quote Directory: Recipient + CC per (GC, Carrier, Market, Project), maintained
-- in the legacy Google Sheet's "Quote Directory" tab. Loaded via
-- swift_api_pipeline/_load_quote_directory.py from the xlsx extract
-- (quote-automation/reference/quote-directory/). match_key = normalized
-- upper(gc|carrier|market|project) for joining to v_quote_review's effective
-- (override-aware) category values.
CREATE TABLE IF NOT EXISTS reference.ref_quote_directory (
  id         bigserial PRIMARY KEY,
  gc         text,
  carrier    text,
  market     text,
  project    text,
  textjoin   text,
  recipient  text,
  cc         text,
  match_key  text,
  loaded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ref_quote_directory_match_key_idx ON reference.ref_quote_directory (match_key);
GRANT SELECT ON reference.ref_quote_directory TO anon, authenticated, service_role;
