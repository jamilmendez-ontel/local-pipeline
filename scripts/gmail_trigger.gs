/**
 * RETIRED 2026-08-14 — DO NOT DEPLOY THIS FILE.
 *
 * The Gmail revenue watcher (checkForRevenueReports) now lives in
 * scripts/pipeline_trigger.gs, merged into the same committed file as every
 * other trigger function in the nanoninth Apps Script project.
 *
 * Why: this project's Apps Script deployment dropped this file around
 * 2026-08-07 (only pipeline_trigger.gs was pasted). The every-5-min trigger
 * kept firing into "Script function not found" and the Daily Revenue Report →
 * gmail-pipeline → Daily Finance chain went silently stale for a week
 * (Aug 7–14). Same failure mode as the 2026-06-22 Open Items outage: a
 * trigger-bound function that isn't in the single deployed source file gets
 * lost on redeploy.
 *
 * Rule: the nanoninth Apps Script project is deployed as ONE file —
 * pipeline_trigger.gs, whole-file paste. Every trigger-bound function must
 * live there.
 */
