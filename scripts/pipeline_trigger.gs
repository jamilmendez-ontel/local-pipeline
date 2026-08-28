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
 *      - triggerLightPipelines()   → Daily, ~1:15 AM EST (Timer only now; create
 *                                    via setupTimerTrigger(), not the editor UI)
 *      - triggerForms()            → Daily, 12:17 AM EST (QA Forms — own trigger)
 *      - triggerOpenItemsData()    → Daily, 02:00 AM EST (Open Items Report Data)
 *      - triggerPrioritiesDaily()  → Daily, 12:13 AM EST (full run: refresh + export + Drive)
 *      - triggerPriorities()       → Every 10 min (DB refresh only) — or run
 *                                    setupPriorityRefreshTrigger() once to create it
 *      - checkForRevenueReports()  → Every 5 min (Gmail watcher → gmail-revenue-report;
 *                                    create via setupRevenueWatchTrigger())
 *      - triggerScheduleFeedAudit() → Every 15 min (Swift schedule feed audit;
 *                                    create via setupScheduleFeedAuditTrigger())
 *      - triggerHolidayFeedWatch() → Mondays 6 PM EST (PH holiday proclamation
 *                                    watch vs reference.ref_holidays; create via
 *                                    setupHolidayFeedWatchTrigger())
 *   4. The GITHUB_TOKEN script property is already set from gmail_trigger.gs
 *
 * Schedules (EST):
 *   10:13 PM  — Orgs & Projects
 *   ~1:15 AM  — Timer (moved from 12:09 AM on 2026-08-06: its
 *               rebuild_timer_clean() calls were colliding with the Asset
 *               Tasks transform, Priorities full run and QA Forms load in the
 *               12:01-1:00 AM window; disk I/O contention pushed the rebuild
 *               from a ~50-70s baseline to 242s, near its 300s
 *               statement_timeout. 1:15 AM sits in the lull before the 2:00 AM
 *               GC/Open Items wave.)
 *   12:17 AM  — QA Forms
 *   12:13 AM  — User Priorities FULL run (refresh + Excel export + shared-Drive upload)
 *   every 10m — User Priorities DB refresh only (extract + transform; no export)
 *   every 5m  — Gmail watch for unread "Daily Revenue Report" emails →
 *               gmail-revenue-report (AR aging + sales; downstream: the
 *               Daily Finance report + COP invoice forecast chains)
 *   12:01 AM  — Asset Tasks (post-local-batch-retirement; fires dispatch_downstream=true
 *               so downstream workflows run at end-of-pipeline)
 *   02:00 AM  — Asset Tasks GC (parallel pipeline for ~294 non-Ontel GC orgs,
 *               fires after the Ontel pipeline completes)
 *   02:00 AM  — Open Items Report Data (targeted_asset_tasks + _task_requirements;
 *               run after User Priorities is fresh. Create via own trigger.)
 *   5:00 AM & 5:00 PM — Calendar Events (twice daily; was 6 AM/6 PM, before that 12:30 AM once daily.
 *               Create via setupCalendarEventsTriggers())
 *   Sat 6:00 PM — Asset Tasks INC weekly full-walk sweep (= Sunday ~6 AM PHT;
 *               heals assign/reschedule drift the hourly shadow walk can't see.
 *               Create via setupAssetTasksIncFullWalkTrigger())
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
 * Schedule: ~1:15 AM ET daily. Create/refresh the trigger by running
 * setupTimerTrigger() once from the Apps Script editor.
 *
 * Moved from 12:09 AM on 2026-08-06: the run's two rebuild_timer_clean()
 * calls (~15 and ~45 min in) landed on top of the Asset Tasks transform
 * (12:01 AM start), the Priorities full run (12:13 AM) and the QA Forms load
 * (12:17 AM). Under that combined disk I/O load the rebuild degraded from a
 * ~50-70s baseline to 242s, close to its 300s statement_timeout (caught by
 * pipeline_health_watcher.py's 120s alarm). 1:15 AM puts both rebuild calls
 * in the 1:00-2:00 AM lull, after the midnight wave and before the 2:00 AM
 * GC/Open Items wave.
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
 * Target time (project timezone, ET) for the daily Timer run.
 * nearMinute() is a +/-15 min hint, so the trigger fires ~1:00-1:30 AM.
 */
var TIMER_HOUR = 1;
var TIMER_MINUTE = 15;

/**
 * Idempotently (re)create the daily time-driven trigger for
 * triggerLightPipelines() at ~1:15 AM ET. RUN THIS ONCE from the Apps Script
 * editor after deploying this file; it replaces the old editor-created
 * 12:09 AM trigger (deleted first by handler name, so re-running is safe,
 * e.g. after editing TIMER_HOUR/TIMER_MINUTE).
 *
 * NOTE: atHour() fires in the project's time zone. This assumes the project is
 * set to America/New_York (ET) like the rest of these schedules; verify via
 * File > Project Settings > Time zone before relying on the 1:15 AM time.
 */
function setupTimerTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerLightPipelines') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerLightPipelines')
    .timeBased()
    .everyDays(1)
    .atHour(TIMER_HOUR)
    .nearMinute(TIMER_MINUTE)
    .create();

  Logger.log('Created triggerLightPipelines trigger at ~' +
             TIMER_HOUR + ':' + (TIMER_MINUTE < 10 ? '0' : '') + TIMER_MINUTE +
             ' ET daily.');
}

/**
 * Target time (project timezone, ET) for the daily Timer EMAILS run:
 * ~6:00 AM ET = 18:00 PHT (19:00 PHT while ET is on standard time), the
 * members' shift start. This is run 2 of 2 for timer data: it re-extracts so
 * stops that reached Swift late (hours after the shift; 3 cases in Aug 2026)
 * are settled, then sends the member-facing emails (--remind / --send /
 * --resend). Run 1 (triggerLightPipelines, ~1:15 AM ET) is data + Excel
 * exports only since 2026-08-28.
 *
 * DIFFERENT dispatch type from 'pipeline-timer' on purpose: the two runs must
 * never be fired by the same trigger.
 */
var TIMER_EMAILS_HOUR = 6;
var TIMER_EMAILS_MINUTE = 0;

function triggerTimerEmails() {
  fireDispatch_('pipeline-timer-emails');
}

/**
 * Idempotently (re)create the daily time-driven trigger for
 * triggerTimerEmails() at ~6:00 AM ET. RUN THIS ONCE from the Apps Script
 * editor after deploying this file (and again after editing
 * TIMER_EMAILS_HOUR/MINUTE); it deletes any existing trigger on the same
 * handler first, so re-running is safe.
 *
 * NOTE: atHour() fires in the project's time zone (America/New_York), so the
 * PHT arrival time shifts by an hour with US DST. Accepted 2026-08-28.
 */
function setupTimerEmailsTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerTimerEmails') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerTimerEmails')
    .timeBased()
    .everyDays(1)
    .atHour(TIMER_EMAILS_HOUR)
    .nearMinute(TIMER_EMAILS_MINUTE)
    .create();

  Logger.log('Created triggerTimerEmails trigger at ~' +
             TIMER_EMAILS_HOUR + ':' + (TIMER_EMAILS_MINUTE < 10 ? '0' : '') + TIMER_EMAILS_MINUTE +
             ' ET daily (18:00 PHT in summer).');
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
 * training/other). Runs TWICE daily at 5:00 AM and 5:00 PM ET. Create the
 * triggers by running setupCalendarEventsTriggers() once.
 */
function triggerCalendarEvents() {
  fireDispatch_('pipeline-calendar-events');
}

/**
 * Run hours (project timezone, ET) for the twice-daily Calendar Events runs.
 * Apps Script daily triggers fire within a ~1-hour window around the given hour.
 */
var CALENDAR_EVENTS_HOURS = [5, 17];

/**
 * Idempotently (re)create the twice-daily time-driven triggers for
 * triggerCalendarEvents() at 5:00 AM and 5:00 PM ET. RUN THIS ONCE from the
 * Apps Script editor after deploying this file. It first deletes ALL existing
 * triggers bound to triggerCalendarEvents AND to the legacy triggerCalendarLeave
 * (this function was renamed; the old time triggers are still bound to the old
 * name and MUST be cleared, or they fail silently with "Script function not
 * found"). Safe to re-run (e.g. after editing CALENDAR_EVENTS_HOURS).
 *
 * NOTE: atHour() fires in the project's time zone. This assumes the project is
 * set to America/New_York (ET) like the rest of these schedules — verify via
 * File > Project Settings > Time zone before relying on the 5 AM / 5 PM times.
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
 * Weekly full-walk sweep for the incremental asset-tasks SHADOW pipeline
 * (pipeline-asset-tasks-inc.yml). Re-fetches every asset + task in the pilot
 * projects through the guarded upserts, healing the change class the
 * lastUpdated-pruned hourly walk can never see: task assign/reschedule bumps
 * the TASK's lastUpdated but NOT the parent asset-project's, so the
 * asset-level prune skips the asset until a submit/approve touches it
 * (2026-08-06 doctrine-gate drift root cause). This is the plan's
 * "Sunday 06:00 PHT ghost sweep" that was never wired — the workflow had
 * ZERO repository_dispatch runs before 2026-08-06.
 *
 * Schedule: Saturday 6 PM ET = Sunday 6 AM PHT (7 AM in US winter), the
 * plan's quiet-window slot. Create via setupAssetTasksIncFullWalkTrigger().
 * Run cost: ~2-3h walk + one ~6-9 GB drift audit (dispatched runs always
 * audit); hourly inc runs queue behind it (concurrency group,
 * cancel-in-progress: false), and the audit evidence persists to
 * pipeline.inc_audit_results.
 *
 * strict_audit stays 'false': this is a heal sweep, not the nightly gate.
 */
function triggerAssetTasksIncFullWalk() {
  fireDispatchWithPayload_('pipeline-asset-tasks-inc', {
    mode: 'full-walk',
    strict_audit: 'false'
  });
}

/**
 * Idempotently (re)create the weekly Saturday 6 PM ET trigger for
 * triggerAssetTasksIncFullWalk(). RUN THIS ONCE from the Apps Script editor
 * after deploying this file. Safe to re-run: deletes any existing triggers
 * bound to the handler first (orphaned-trigger gotcha — see
 * setupCalendarEventsTriggers).
 */
function setupAssetTasksIncFullWalkTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerAssetTasksIncFullWalk') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerAssetTasksIncFullWalk')
    .timeBased()
    .everyWeeks(1)
    .onWeekDay(ScriptApp.WeekDay.SATURDAY)
    .atHour(18)
    .create();

  Logger.log('Created weekly triggerAssetTasksIncFullWalk trigger: ' +
             'Saturdays ~6 PM ET (Sunday morning PHT full-walk sweep).');
}

/**
 * Trigger the Swift schedule feed audit (incremental mode) every 15 minutes.
 *
 * Alarm-ASAP request (Jamil 2026-08-20, after the TENASKA - Horvath miss):
 * hourly GHA cron drifts 20-40 min under load, so a member's bad schedule
 * could sit unalerted for ~2 hours. Apps Script dispatch fires with zero
 * delay; combined with the audit's in-run recheck (which replaced the
 * skip-to-next-run grace) worst case member-notice lag is now ~20 min.
 *
 * The hourly GHA cron in schedule-feed-audit.yml stays as a backstop (the
 * audit's overlap guard makes double-fires harmless: the second run exits).
 * Create the trigger by running setupScheduleFeedAuditTrigger() once.
 */
function triggerScheduleFeedAudit() {
  fireDispatch_('schedule-feed-audit');
}

/**
 * Cadence (minutes) for the schedule feed audit trigger.
 * Apps Script minute-based triggers allow 1, 5, 10, 15, or 30.
 */
var SCHEDULE_AUDIT_MINUTES = 15;

/**
 * Idempotently (re)create the every-15-minute time-driven trigger for
 * triggerScheduleFeedAudit(). RUN THIS ONCE from the Apps Script editor after
 * deploying this file. Deletes existing triggers bound to the handler first
 * (orphaned-trigger gotcha — see setupCalendarEventsTriggers), so it is safe
 * to re-run after changing SCHEDULE_AUDIT_MINUTES.
 */
function setupScheduleFeedAuditTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerScheduleFeedAudit') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerScheduleFeedAudit')
    .timeBased()
    .everyMinutes(SCHEDULE_AUDIT_MINUTES)
    .create();

  Logger.log('Created triggerScheduleFeedAudit trigger: every ' +
             SCHEDULE_AUDIT_MINUTES + ' minutes.');
}

/**
 * Holiday feed watch (weekly): fires holiday-feed-watch, which runs
 * swift_api_pipeline/holiday_feed_watcher.py (Official Gazette + Nager.Date
 * vs reference.ref_holidays; emails Jamil proposed SQL, never edits the table).
 * The weekly GHA cron in holiday-feed-watch.yml is the backstop; the watermark
 * in pipeline.holiday_watch_runs makes a double fire a no-op.
 * Create the trigger by running setupHolidayFeedWatchTrigger() once.
 */
function triggerHolidayFeedWatch() {
  fireDispatch_('holiday-feed-watch');
}

/**
 * Idempotently (re)create the weekly trigger for triggerHolidayFeedWatch():
 * Mondays, 6 PM script time (EST) = Tuesday 7 AM PHT. RUN THIS ONCE from the
 * Apps Script editor after deploying this file. Deletes existing triggers
 * bound to the handler first (orphaned-trigger gotcha).
 */
function setupHolidayFeedWatchTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerHolidayFeedWatch') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerHolidayFeedWatch')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY)
    .atHour(18)
    .create();

  Logger.log('Created triggerHolidayFeedWatch trigger: Mondays 6 PM.');
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
 * Returns true on success (HTTP 204), false otherwise — callers that need
 * transactional behavior (e.g. checkForRevenueReports' mark-as-read) rely on it.
 */
function fireDispatch_(eventType) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return false;
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
    return true;
  } else {
    Logger.log('ERROR dispatching ' + eventType + ': HTTP ' + code + ' — ' + response.getContentText());
    return false;
  }
}

/**
 * Gmail watcher: unread "Daily Revenue Report" emails → gmail-revenue-report.
 *
 * Runs every 5 minutes on a time-driven trigger. When the daily revenue email
 * (AR Aging Detail + Sales by Product/Service attachments) lands, fires ONE
 * repository_dispatch so gmail-pipeline.yml loads aging + sales; the pipeline's
 * SUCCESS email then triggers the Daily Finance report + COP invoice forecast
 * chains from the ontel.co Apps Script project.
 *
 * ROOT CAUSE of the 2026-08-07→14 outage — DO NOT REMOVE THIS FUNCTION FROM
 * SOURCE: this function previously lived only in gmail_trigger.gs, and a
 * redeploy of this project dropped that file. The 5-min trigger kept firing
 * into "Script function not found" and the whole revenue/finance chain went
 * silently stale for a week (same failure mode as the 2026-06-22 OIR outage
 * documented at triggerOpenItemsData). It now lives HERE, in the same
 * committed file as every other trigger function, so a whole-file redeploy
 * can never lose it. gmail_trigger.gs is retired.
 *
 * Failure semantics: if the dispatch fails, threads stay UNREAD so the next
 * 5-min tick retries. markRead only happens after a 204.
 */
var GMAIL_REVENUE_QUERY = 'subject:"Daily Revenue Report" has:attachment is:unread';

function checkForRevenueReports() {
  var threads = GmailApp.search(GMAIL_REVENUE_QUERY, 0, 10);

  if (threads.length === 0) {
    return;
  }

  var dispatched = false;

  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    Logger.log('Found unread revenue report: ' + thread.getFirstMessageSubject());

    // Fire dispatch only once per invocation (all unread emails trigger the same pipeline)
    if (!dispatched) {
      if (!fireDispatch_('gmail-revenue-report')) {
        Logger.log('ERROR: repository_dispatch failed — skipping mark-as-read (will retry next cycle)');
        return;
      }
      dispatched = true;
    }

    // Mark thread as read so it doesn't re-trigger
    thread.markRead();
  }

  Logger.log('Dispatched gmail-revenue-report event, marked ' + threads.length + ' thread(s) as read');
}

/**
 * Idempotently (re)create the every-5-minute time-driven trigger for
 * checkForRevenueReports(). Deletes any existing triggers bound to the handler
 * first (orphaned-trigger gotcha — see setupCalendarEventsTriggers), so it is
 * safe to re-run. If a working 5-min trigger already exists, you do NOT need
 * to run this.
 */
function setupRevenueWatchTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'checkForRevenueReports') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('checkForRevenueReports')
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log('Created checkForRevenueReports trigger: every 5 minutes.');
}

/**
 * Run this function ONCE after rotating the GITHUB_TOKEN PAT to schedule a
 * calendar reminder 5 days before the 90-day expiry (all-day event with email
 * reminders on the nanoninth.com default calendar). Carried over from the
 * retired gmail_trigger.gs.
 */
function scheduleTokenRotationReminder() {
  var EXPIRY_DAYS = 90;
  var REMINDER_DAYS_BEFORE = 5;

  var reminderDate = new Date();
  reminderDate.setDate(reminderDate.getDate() + EXPIRY_DAYS - REMINDER_DAYS_BEFORE);

  var event = CalendarApp.getDefaultCalendar().createAllDayEvent(
    'Rotate GitHub PAT for Pipeline Triggers',
    reminderDate,
    {
      description:
        'The fine-grained GitHub PAT (local-pipeline repo, contents:read+write) expires in 5 days.\n\n' +
        'This PAT is used by ALL pipeline triggers in this Apps Script project\n' +
        '(pipeline_trigger.gs — time-driven dispatches + the Gmail revenue watcher).\n\n' +
        'Steps:\n' +
        '1. Go to https://github.com/settings/tokens and generate a new 90-day PAT\n' +
        '   - Repository: local-pipeline only\n' +
        '   - Permissions: Contents → Read and Write\n' +
        '   - Expiration: 90 days\n' +
        '2. Update GITHUB_TOKEN in Apps Script project settings (Script Properties)\n' +
        '3. Run scheduleTokenRotationReminder() again to set the next reminder'
    }
  );

  event.addEmailReminder(0);       // At start of day
  event.addEmailReminder(24 * 60); // 1 day before

  Logger.log('Rotation reminder created for ' + reminderDate.toDateString());
}

/**
 * Test function — manually trigger all pipelines to verify setup.
 * Run this once after setup to confirm everything works.
 * Deliberately EXCLUDES triggerAssetTasksIncFullWalk: that dispatch starts a
 * ~2-3h full walk + a ~6-9 GB audit — fire it manually only when you mean it.
 * Also EXCLUDES triggerTimerEmails: that dispatch emails every member their
 * daily timer entries (and re-sends the day if it already went out).
 */
function testAllDispatches() {
  fireDispatch_('pipeline-orgs');
  fireDispatch_('pipeline-timer');
  fireDispatch_('pipeline-priorities');
  fireDispatch_('pipeline-forms');
  fireDispatch_('pipeline-open-items-data');
  fireDispatch_('pipeline-calendar-events');
  fireDispatch_('schedule-feed-audit');
  Logger.log('All 7 dispatches fired — check GitHub Actions.');
}
