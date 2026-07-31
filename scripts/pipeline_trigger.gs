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
 *      - triggerOrgs()             → Daily, 10:13 PM EST
 *      - triggerLightPipelines()   → Daily, 12:09 AM EST (Timer only now)
 *      - triggerForms()            → Daily, 12:17 AM EST (QA Forms — own trigger)
 *      - triggerOpenItemsData()    → Daily, 02:00 AM EST (Open Items Report Data)
 *      - triggerPrioritiesDaily()  → Daily, 12:13 AM EST (full run: refresh + export + Drive)
 *      - triggerPriorities()       → Every 10 min (DB refresh only) — or run
 *                                    setupPriorityRefreshTrigger() once to create it
 *   4. The GITHUB_TOKEN script property is already set from gmail_trigger.gs
 *
 * Schedules (EST):
 *   10:13 PM  — Orgs & Projects
 *   12:09 AM  — Timer
 *   12:17 AM  — QA Forms
 *   12:13 AM  — User Priorities FULL run (refresh + Excel export + shared-Drive upload)
 *   every 10m — User Priorities DB refresh only (extract + transform; no export)
 *   12:01 AM  — Asset Tasks (post-local-batch-retirement; fires dispatch_downstream=true
 *               so downstream workflows run at end-of-pipeline)
 *   02:00 AM  — Asset Tasks GC (parallel pipeline for ~294 non-Ontel GC orgs,
 *               fires after the Ontel pipeline completes)
 *   02:00 AM  — Open Items Report Data (targeted_asset_tasks + _task_requirements;
 *               run after User Priorities is fresh. Create via own trigger.)
 *   6:00 AM & 6:00 PM — Calendar Events (twice daily; was 12:30 AM once daily.
 *               Create via setupCalendarEventsTriggers())
 *
 * Note: QA Forms used to be staggered inside triggerLightPipelines() via an
 * 8-min Utilities.sleep() after Timer. That was fragile — if the trigger
 * execution ended before the sleep completed, the forms dispatch was silently
 * dropped (this happened after the 2026-06-22 redeploy: Timer kept firing but
 * QA Forms stopped from 2026-06-23 onward). QA Forms now has its own dedicated
 * time-driven trigger (triggerForms, 12:17 AM EST), so each pipeline fires from
 * its own short execution with no in-process sleep.
 */

var REPO = 'jamilmendez-ontel/local-pipeline';

/**
 * Trigger orgs pipeline — schedule this at 10:13 PM EST daily.
 */
function triggerOrgs() {
  fireDispatch_('pipeline-orgs');
}

/**
 * Trigger the daily Timer pipeline.
 * Schedule this at 12:09 AM EST daily.
 *
 * NOTE: This used to also fire QA Forms after an 8-min Utilities.sleep() and
 * User Priorities before that. Both have been split out:
 *   - QA Forms      → triggerForms() on its own 12:17 AM trigger
 *   - User Priorities → triggerPriorities() (every 10 min) + triggerPrioritiesDaily()
 * There is no longer any in-process sleep here, so the execution is short and
 * cannot silently drop a later dispatch.
 */
function triggerLightPipelines() {
  fireDispatch_('pipeline-timer');
}

/**
 * Trigger the daily QA Forms pipeline.
 * Schedule this at 12:17 AM EST daily (its own time-driven trigger).
 *
 * Previously fired from inside triggerLightPipelines() via an 8-min sleep after
 * Timer; that staggering was fragile and stopped firing after 2026-06-22. Each
 * pipeline now has its own dedicated trigger.
 */
function triggerForms() {
  fireDispatch_('pipeline-forms');
}

/**
 * Trigger the Open Items Report Data pipeline (targeted_asset_tasks +
 * targeted_task_requirements -> stg_targeted_asset_tasks /
 * stg_targeted_task_requirements). Schedule this at ~02:00 AM EST daily on its
 * own time-driven trigger.
 *
 * Run it AFTER User Priorities is fresh: targeted_task_requirements reads
 * stg_user_priorities to decide which task_dids to fetch requirements for.
 * Priorities refreshes every 10 min (triggerPriorities), so any time around
 * 02:00 AM is safe.
 *
 * ROOT CAUSE of the 6-22 outage — DO NOT REMOVE THIS FUNCTION FROM SOURCE:
 * this function previously existed ONLY in the deployed Apps Script editor and
 * was never committed here. When the project was re-pasted from committed source
 * on 2026-06-22 the function vanished, its time trigger had nothing to bind to,
 * and OIR silently went stale from 2026-06-23. The standalone-timer pattern is
 * fine (Orgs/Timer/Calendar all use it and never broke) — the ONLY reason OIR
 * broke is that its function wasn't in source. Keep it committed and a redeploy
 * can never lose it again. Re-create the time trigger once in the Apps Script UI.
 */
function triggerOpenItemsData() {
  fireDispatch_('pipeline-open-items-data');
}

/**
 * Trigger the User Priorities DB refresh ONLY (extract + transform).
 * Schedule this on a time-driven trigger every 10 minutes (later 5 min).
 *
 * Fires with run_export=false so the workflow skips the Excel export and the
 * shared-Drive upload — those run once daily via triggerPrioritiesDaily().
 *
 * The workflow itself emails only on FAILURE (after its internal retry), so a
 * 10-min cadence produces no success-email noise.
 *
 * To create/refresh the 10-min trigger programmatically, run
 * setupPriorityRefreshTrigger() once (edit PRIORITY_REFRESH_MINUTES to 5 to
 * move from 10 → 5 min, then re-run it).
 */
function triggerPriorities() {
  fireDispatchWithPayload_('pipeline-priorities', { run_export: false });
}

/**
 * Trigger the daily User Priorities FULL run: DB refresh + Excel export +
 * shared-Drive upload. Schedule this once daily (e.g. 12:13 AM EST).
 *
 * Fires with run_export=true so the workflow runs the export + upload steps.
 */
function triggerPrioritiesDaily() {
  fireDispatchWithPayload_('pipeline-priorities', { run_export: true });
}

/**
 * Cadence (minutes) for the User Priorities DB-refresh trigger.
 * Apps Script minute-based triggers allow 1, 5, 10, 15, or 30.
 * Start at 10; change to 5 and re-run setupPriorityRefreshTrigger() to speed up.
 */
var PRIORITY_REFRESH_MINUTES = 10;

/**
 * Idempotently (re)create the every-N-minute time-driven trigger for
 * triggerPriorities(). Run this once from the Apps Script editor. Re-running it
 * deletes the old triggerPriorities trigger first, so it is safe to re-run
 * after changing PRIORITY_REFRESH_MINUTES (10 → 5).
 */
function setupPriorityRefreshTrigger() {
  // Remove any existing triggers bound to triggerPriorities to avoid duplicates.
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerPriorities') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerPriorities')
    .timeBased()
    .everyMinutes(PRIORITY_REFRESH_MINUTES)
    .create();

  Logger.log('Created triggerPriorities trigger: every ' +
             PRIORITY_REFRESH_MINUTES + ' minutes.');
}

/**
 * Trigger the Calendar Events pipeline (all kinds: leave/holiday/birthday/
 * training/other). Runs TWICE daily at 6:00 AM and 6:00 PM ET. Create the
 * triggers by running setupCalendarEventsTriggers() once.
 */
function triggerCalendarEvents() {
  fireDispatch_('pipeline-calendar-events');
}

/**
 * Run hours (project timezone, ET) for the twice-daily Calendar Events runs.
 * Apps Script daily triggers fire within a ~1-hour window around the given hour.
 */
var CALENDAR_EVENTS_HOURS = [6, 18];

/**
 * Idempotently (re)create the twice-daily time-driven triggers for
 * triggerCalendarEvents() at 6:00 AM and 6:00 PM ET. RUN THIS ONCE from the
 * Apps Script editor after deploying this file. It first deletes ALL existing
 * triggers bound to triggerCalendarEvents AND to the legacy triggerCalendarLeave
 * (this function was renamed; the old time triggers are still bound to the old
 * name and MUST be cleared, or they fail silently with "Script function not
 * found"). Safe to re-run (e.g. after editing CALENDAR_EVENTS_HOURS).
 *
 * NOTE: atHour() fires in the project's time zone. This assumes the project is
 * set to America/New_York (ET) like the rest of these schedules — verify via
 * File > Project Settings > Time zone before relying on the 6 AM / 6 PM times.
 */
function setupCalendarEventsTriggers() {
  // Remove existing triggers bound to the new name AND to the legacy
  // triggerCalendarLeave (renamed function — clear its now-orphaned triggers).
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    var fn = existing[i].getHandlerFunction();
    if (fn === 'triggerCalendarEvents' || fn === 'triggerCalendarLeave') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  for (var h = 0; h < CALENDAR_EVENTS_HOURS.length; h++) {
    ScriptApp.newTrigger('triggerCalendarEvents')
      .timeBased()
      .everyDays(1)
      .atHour(CALENDAR_EVENTS_HOURS[h])
      .create();
  }

  Logger.log('Created triggerCalendarEvents triggers at hours (ET): ' +
             CALENDAR_EVENTS_HOURS.join(', '));
}

/**
 * Trigger asset_tasks pipeline (the big nightly: ~30-40 min on GHA).
 *
 * Schedule: 12:01 AM EST daily (time-driven trigger in Apps Script editor).
 *
 * Fires downstream dispatches at end-of-run:
 *   - pipeline-asset-tasks-export (same-repo)
 *   - date-validator-daily (cross-repo, requires DATE_VALIDATOR_DISPATCH_PAT)
 *   - weekly-compliance-audit (cross-repo, Fridays only, same PAT; this
 *     dispatch is gated to UTC-Fridays, so the compliance email and Google
 *     Chat post both go out once a week on Friday)
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
  fireDispatch_('pipeline-open-items-data');
  fireDispatch_('pipeline-calendar-events');
  Logger.log('All 6 dispatches fired — check GitHub Actions.');
}
