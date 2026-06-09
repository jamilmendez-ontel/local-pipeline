-- 077_v_quote_directory.sql
-- Expose the Quote Directory to the webapp via an analytics view (analytics is
-- PostgREST-exposed; reference is not). Read-only browse of the 210 directory rows.
CREATE OR REPLACE VIEW analytics.v_quote_directory AS
SELECT gc, carrier, market, project, recipient, cc
FROM reference.ref_quote_directory
ORDER BY gc, carrier, market, project;
GRANT SELECT ON analytics.v_quote_directory TO anon, authenticated, service_role;
