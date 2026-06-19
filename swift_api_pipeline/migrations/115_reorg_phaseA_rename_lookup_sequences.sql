-- ═══════════════════════════════════════════════════════════════════════════════
-- 115 — OntelDB reorg Phase A tidy-up: rename owned sequences to match renamed tables
-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 113 renamed the lookup tables (customer_name_lookup -> ref_customer_names,
-- carrier_group_lookup -> ref_carrier_groups). Postgres does NOT auto-rename a table's
-- OWNED sequence, so the SERIAL id sequences kept their old base names (they did move
-- into the reference schema with their tables). The column defaults still worked (bound
-- by OID), but the names were stale vs the rest of reference.ref_*_id_seq. Rename for
-- consistency. Metadata-only; the id-column defaults rebind automatically by OID.
-- Found during post-reorg verification.
BEGIN;
ALTER SEQUENCE reference.customer_name_lookup_id_seq RENAME TO ref_customer_names_id_seq;
ALTER SEQUENCE reference.carrier_group_lookup_id_seq RENAME TO ref_carrier_groups_id_seq;
COMMIT;
