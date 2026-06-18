-- 106_oir_responder_tables.sql
-- Open Items Report email-responder persistence.
-- stg_oir_report_sends   : send registry — maps a reply thread to the report it came from.
-- stg_oir_reply_requests : audit log + confirm state for inbound replies.

CREATE TABLE IF NOT EXISTS data_staging.stg_oir_report_sends (
    id               bigserial PRIMARY KEY,
    send_date        date        NOT NULL,
    cadence          text        NOT NULL CHECK (cadence IN ('monday', 'friday')),
    gmail_thread_id  text        NOT NULL,
    gmail_message_id text        NOT NULL,
    groups           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    recipients       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_oir_sends_thread
    ON data_staging.stg_oir_report_sends (gmail_thread_id);

CREATE TABLE IF NOT EXISTS data_staging.stg_oir_reply_requests (
    id                bigserial PRIMARY KEY,
    gmail_thread_id   text        NOT NULL,
    gmail_message_id  text        NOT NULL UNIQUE,   -- dedupe key
    sender            text        NOT NULL,
    raw_text          text,
    classified_intent jsonb,
    action            text,
    confirm_status    text        NOT NULL DEFAULT 'none'
                      CHECK (confirm_status IN ('none', 'pending', 'confirmed', 'expired')),
    result            jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz
);
CREATE INDEX IF NOT EXISTS ix_oir_replies_thread
    ON data_staging.stg_oir_reply_requests (gmail_thread_id);
CREATE INDEX IF NOT EXISTS ix_oir_replies_pending
    ON data_staging.stg_oir_reply_requests (gmail_thread_id, sender)
    WHERE confirm_status = 'pending';
