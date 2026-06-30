# Proposal: Migrate Asset Task Pipeline to GitHub Actions

## Problem

The asset task pipeline is the only remaining pipeline that runs on Jamil's personal PC. This has caused repeated failures and manual recovery work:

**April 10 — Internet outage killed the pipeline mid-run.** ISP lost connectivity at ~00:58 AM during the asset tasks transform. Extraction had completed (2.4M rows) but the transform failed with `ConnectionAbortedError` + DNS failure. Left 2.1M orphaned raw rows with stuck `status=running`. Required manual recovery: re-run transforms, mark stuck run as failed, delete orphaned rows, re-run all exports.

**April 11 — ISP routing to Supabase broken for a second night.** ConvergeICT couldn't reach AWS Asia-Pacific (Singapore). All local pipelines failed with `getaddrinfo failed`. GHA pipelines ran fine in the cloud. Root cause: Cloudflare WARP was turned off. Required manual recovery: re-enable WARP, re-run asset tasks (57 min), timer discrepancies, calendar leave, backfill, analytics, and exports.

**April 15 — Swift API instability caused cascade failure.** API returned heavy 503 errors. Three consecutive runs failed the safety check before a 4th succeeded on its own at 02:15 AM. Each failed run left orphaned rows that inflated the next run's baseline, compounding the failures. The pipeline ran for nearly 2 hours of retries with no human oversight.

**April 15 (same night) — Export dispatch succeeded but GHA export failed.** The local batch dispatched the GHA export workflow, but it failed because the asset tasks extraction had reported failure. Required manual intervention to trigger the export after the 4th run finally succeeded.

If the PC is off, asleep, or has no internet, there is no pipeline run and no notification — it fails silently.

## Solution

Move the asset task pipeline to GitHub Actions and convert the database table to PostgreSQL partitioning.

**GitHub Actions** eliminates the dependency on the local PC. The pipeline runs on GitHub's infrastructure on a nightly cron schedule — same as our other 11 pipelines.

**Table partitioning** splits the single 2.4M-row table into per-project partitions (~350-430K rows each). This provides:

- **Partial failure resilience** — if one project fails, the other 6 keep their fresh data (the April 15 scenario, where all data was thrown away because the combined total failed the safety check, cannot happen)
- **Per-project recovery** — re-run just the failed project instead of all 7
- **No connection timeout risk** — index rebuilds on 350K rows take seconds instead of 5+ minutes on 2.4M rows (this caused a timeout failure on Feb 15)
- **Auto-detection of new projects** — when a new TS project is added, the pipeline automatically creates a new partition with no manual work

## What Changes

- One new GitHub Actions workflow file (nightly cron at 12:01 AM ET)
- Database migration: `raw_asset_tasks` converted to a partitioned table (one-time, zero impact on downstream queries)
- Local batch file (`scheduled_main_pipeline.bat`) retired after 1-week parallel run

## What Doesn't Change

- Same Python code, same extraction logic, same transforms
- All other 11 GHA workflows unchanged
- Dashboards, reports, and exports work the same way
- All credentials already stored as GitHub Secrets

## Cost

**$9.60/month** — GitHub Actions usage overage on the Free plan.

Our current 11 workflows use ~1,800 minutes/month of the 2,000-minute free allowance. Adding the asset task pipeline adds ~1,800 minutes/month. The overage of ~1,600 minutes at $0.006/minute = $9.60/month. No subscription change needed.

## Timeline

- Implementation: 1-2 days
- Parallel run (local + GHA): 1 week
- Retire local pipeline: after parallel run confirmed stable

## Risk

Low. Same code, same database, same credentials. Both local and GHA pipelines run in parallel during the transition period. The local pipeline is only disabled after GHA is confirmed stable.
