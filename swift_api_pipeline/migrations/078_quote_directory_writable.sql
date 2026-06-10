-- 078_quote_directory_writable.sql
-- Make the Quote Directory editable from the webapp. Supabase
-- reference.ref_quote_directory is now the SOURCE OF TRUTH (add/edit/remove in the
-- app persist here via the service role). The xlsx loader was the initial seed
-- only; do NOT truncate-reload it anymore or it would wipe app edits. (Future:
-- a MERGE sync from the Google Sheet instead of overwrite.)
GRANT INSERT, UPDATE, DELETE ON reference.ref_quote_directory TO service_role;
GRANT USAGE, SELECT ON SEQUENCE reference.ref_quote_directory_id_seq TO service_role;
