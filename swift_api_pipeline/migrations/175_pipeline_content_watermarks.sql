-- 175: content watermark for snapshot-shaped pipelines. The User Priorities
-- pipeline full delete+reloads raw AND staging (~23.5k rows) every 5 minutes
-- whether or not the Swift snapshot changed (31.5M lifetime inserts / 31.4M
-- deletes for 11.7k live rows; autovacuum on both tables every cycle), a main
-- remaining driver of the Supabase Disk IO budget depletion (2026-07-09).
-- The pipeline now hashes the fetched snapshot and skips the entire load,
-- transform, Excel export, and Drive upload when the hash matches the last
-- loaded one. Cadence is untouched (night-shift consumer needs 5-minute
-- freshness): a real change still lands within one run.
--
-- Force a rebuild despite an unchanged snapshot (e.g. after a transform code
-- change) by deleting the row:
--   delete from pipeline.content_watermarks where pipeline_name = 'user_priorities';
-- or by setting USER_PRIORITIES_FORCE=1 in the workflow env for one run.

create table pipeline.content_watermarks (
  pipeline_name text primary key,
  content_hash  text        not null,
  updated_at    timestamptz not null default now()
);

alter table pipeline.content_watermarks enable row level security;

comment on table pipeline.content_watermarks is
  'Last-loaded content hash per snapshot-shaped pipeline; lets a run skip all writes when the source snapshot is unchanged. Delete a row to force the next run to load.';
