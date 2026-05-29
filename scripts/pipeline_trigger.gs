/**
 * Pipeline Triggers for GitHub Actions — Swift API Pipelines
 *
 * Fires repository_dispatch events on a schedule to trigger individual
 * pipeline workflows with zero delay (unlike GHA cron which can lag 1-2 hours).
 *
 * Setup:
 *   1. Open the existing Apps Script project under jamil.mendez@nanoninth.com
 *      (same project as gmail_trigger.gs — reuses GITHUB_TOKEN)
 *   2. Paste these functions into a new file (e.g. pipeline_trigger.gs)
 *   3. Create time-driven triggers in Apps Script:
 *      - triggerOrgs()           → Daily, 10:13 PM EST
 *      - triggerLightPipelines() → Daily, 12:09 AM EST
 *   4. The GITHUB_TOKEN script property is already set from gmail_trigger.gs
 *
 * Schedules (EST):
 *   10:13 PM  — Orgs & Projects
 *   12:09 AM  — Timer
 *   12:13 AM  — User Priorities
 *   12:17 AM  — QA Forms
 *   01:30 AM  — Asset Tasks (shakedown phase: dispatch_downstream=false, runs AFTER
 *               local batch completes ~01:10 ET; Task 6 retires local batch and
 *               moves this to 12:01 AM with dispatch_downstream=true)
 *   02:00 AM  — Asset Tasks GC (parallel pipeline for ~294 non-Ontel GC orgs,
 *               fires after the Ontel pipeline completes)
 *
 * Note: triggerLightPipelines() fires timer at :09, priorities at :13, forms at :17
 * by using Utilities.sleep() for staggering. Apps Script has a 6-min execution limit
 * so the 8-min total stagger fits within one invocation.
 */

var REPO = 'jamilmendez-ontel/local-pipeline';

/**
 * Trigger orgs pipeline — schedule this at 10:13 PM EST daily.
 */
function triggerOrgs() {
  fireDispatch_('pipeline-orgs');
}

/**
 * Trigger all light pipelines with staggered timing.
 * Schedule this at 12:09 AM EST daily.
 *
 * 12:09 AM — Timer fires immediately
 * 12:13 AM — User Priorities (4 min delay)
 * 12:17 AM — QA Forms (8 min delay from start)
 * 12:21 AM — Timer Discrepancies (12 min delay from start)
 *
 * Note: Apps Script time-driven triggers have ±1 min jitter, but the
 * relative spacing between dispatches is exact.
 */
function triggerLightPipelines() {
  // Timer — fires immediately
  fireDispatch_('pipeline-timer');
  Logger.log('Waiting 4 minutes before triggering priorities...');

  // User Priorities — 4 min after timer
  Utilities.sleep(4 * 60 * 1000);
  fireDispatch_('pipeline-priorities');
  Logger.log('Waiting 4 minutes before triggering forms...');

  // QA Forms — 4 min after priorities (8 min after timer)
  Utilities.sleep(4 * 60 * 1000);
  fireDispatch_('pipeline-forms');
  Logger.log('Waiting 4 minutes before triggering timer discrepancies...');

  // Timer Discrepancies — 4 min after forms (12 min after timer)
  Utilities.sleep(4 * 60 * 1000);
  fireDispatch_('pipeline-timer-discrepancies');
}

/**
 * Trigger calendar leave pipeline.
 * Schedule this at 12:30 AM EST daily (separate trigger).
 */
function triggerCalendarLeave() {
  fireDispatch_('pipeline-calendar-leave');
}

/**
 * Trigger asset_tasks pipeline (the big nightly: ~52 min extract + ~15 min downstream).
 *
 * SHAKEDOWN PHASE (current):
 *   Schedule this at 01:30 AM EST daily — runs AFTER the still-active local batch
 *   completes (local kicks off at 00:01, finishes ~01:10). dispatch_downstream is
 *   passed false so the local batch keeps owning the end-of-night dispatches for
 *   pipeline-asset-tasks-export, pipeline-timer-discrepancies, and date-validator-daily.
 *   This lets us compare GHA's output vs the local batch's for ~1 week before retiring.
 *
 * POST-TASK-6 (after local batch retires):
 *   - Change the time-driven trigger to 12:01 AM EST
 *   - Edit fireDispatchWithPayload_ call below to pass {dispatch_downstream: true}
 *     (or just call fireDispatch_('pipeline-asset-tasks') since true is the default).
 */
function triggerAssetTasks() {
  fireDispatch_('pipeline-asset-tasks');
}

/**
 * Trigger GC asset_tasks pipeline (the parallel ~294-org pipeline for all
 * non-Ontel General Contractors).
 *
 * Schedule daily at 02:00 AM EST — well after the Ontel pipeline finishes
 * (~01:00 ET post-Task-6 cutover) so we avoid Swift API rate-limit
 * collisions and DB pool contention.
 *
 * GC pipeline writes to separate _gc tables (raw_asset_tasks_gc,
 * stg_asset_tasks_gc, stg_assets_gc) and refreshes its own MVs
 * (mv_project_summary_gc, mv_technician_stats_gc, mv_daily_completion_gc).
 * No downstream dispatches in v1 — no export or validator emails.
 */
function triggerAssetTasksGC() {
  fireDispatch_('pipeline-asset-tasks-gc');
}

/**
 * Like fireDispatch_ but allows passing a client_payload — required when the
 * receiving workflow's `on: repository_dispatch` reads inputs via
 * github.event.client_payload.* (which is how we gate dispatch_downstream).
 */
function fireDispatchWithPayload_(eventType, clientPayload) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  var url = 'https://api.github.com/repos/' + REPO + '/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: eventType,
      client_payload: clientPayload || {}
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('Dispatched ' + eventType + ' with payload ' + JSON.stringify(clientPayload) + ' successfully');
  } else {
    Logger.log('ERROR dispatching ' + eventType + ': HTTP ' + code + ' — ' + response.getContentText());
  }
}

/**
 * Fire a repository_dispatch event. Reuses the GITHUB_TOKEN from Script Properties.
 */
function fireDispatch_(eventType) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  var url = 'https://api.github.com/repos/' + REPO + '/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: eventType
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('Dispatched ' + eventType + ' successfully');
  } else {
    Logger.log('ERROR dispatching ' + eventType + ': HTTP ' + code + ' — ' + response.getContentText());
  }
}

/**
 * Test function — manually trigger all pipelines to verify setup.
 * Run this once after setup to confirm everything works.
 */
function testAllDispatches() {
  fireDispatch_('pipeline-orgs');
  fireDispatch_('pipeline-timer');
  fireDispatch_('pipeline-priorities');
  fireDispatch_('pipeline-forms');
  fireDispatch_('pipeline-timer-discrepancies');
  fireDispatch_('pipeline-calendar-leave');
  Logger.log('All 6 dispatches fired — check GitHub Actions.');
}
