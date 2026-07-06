-- 154_sheet_sync_state.sql
-- Watermark for change-gated Google Sheet syncs. The roster sync runs daily but only
-- actually syncs when the HR sheet's Drive modifiedTime advanced past what we last
-- synced (and edits have settled), so an unchanged sheet is a cheap no-op and we don't
-- act on a mid-edit state. One row per sheet_id.

CREATE TABLE IF NOT EXISTS pipeline.sheet_sync_state (
    sheet_id           TEXT PRIMARY KEY,
    last_modified_time TIMESTAMPTZ,   -- Drive modifiedTime at the last successful sync
    last_synced_at     TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE pipeline.sheet_sync_state IS 'Per-sheet watermark: last Drive modifiedTime synced, for change-gated roster sync.';
