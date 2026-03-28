-- Track extraction progress per project for resume-on-failure capability.
-- When a network drop interrupts extraction mid-project, the pipeline can
-- resume from the last saved cursor instead of re-extracting from page 1.

CREATE TABLE IF NOT EXISTS pipeline.extraction_progress (
    run_id      UUID    NOT NULL,
    project_did TEXT    NOT NULL,
    rows_written INTEGER NOT NULL DEFAULT 0,
    after_ap    TEXT,
    after_id    TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, project_did)
);
