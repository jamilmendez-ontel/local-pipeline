-- 104_quote_send_identities.sql
-- In-app "send as" masks. Shared (admin-managed, owner_email NULL) + personal (per-user).
CREATE TABLE IF NOT EXISTS data_staging.stg_quote_send_identities (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL,
  email       text NOT NULL,
  scope       text NOT NULL CHECK (scope IN ('shared','personal')),
  owner_email text,
  created_by  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_send_identities_shared_email
  ON data_staging.stg_quote_send_identities (email) WHERE scope = 'shared';
CREATE UNIQUE INDEX IF NOT EXISTS uq_send_identities_personal_owner_email
  ON data_staging.stg_quote_send_identities (owner_email, email) WHERE scope = 'personal';

-- Seed the one current shared mask (was in QUOTE_SEND_AS_IDENTITIES).
INSERT INTO data_staging.stg_quote_send_identities (name, email, scope, owner_email, created_by)
VALUES ('Accounting | Ontel', 'accounting@ontel.co', 'shared', NULL, 'migration')
ON CONFLICT DO NOTHING;
