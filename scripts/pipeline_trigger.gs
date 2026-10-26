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
 *   3. Run setupScheduler() once (since 2026-10-26). It deletes the old
 *      one-per-job daily/weekly triggers and creates ONE runScheduler()
 *      trigger every 5 minutes, which fires each job in SCHEDULE (bottom of
 *      this file) at its time, in its own time zone, once per day.
 *      Apps Script allows only 20 triggers per script; the old
 *      one-trigger-per-job layout hit that limit on 2026-10-26
 *      (setupEmployeeSyncTrigger: "This script has too many triggers").
 *      Only the high-frequency jobs keep their own triggers:
 *      - triggerPriorities()        → every 10 min (setupPriorityRefreshTrigger())
 *      - checkForRevenueReports()   → every 5 min (setupRevenueWatchTrigger())
 *      - triggerScheduleFeedAudit() → every 15 min (setupScheduleFeedAuditTrigger())
 *      Total: 4 triggers. To change a time, edit SCHEDULE and paste the
 *      whole file; no trigger change is needed.
 *   4. The GITHUB_TOKEN script property is already set from gmail_trigger.gs
 *
 * Schedules (ET unless marked; SCHEDULE at the bottom is the source of truth):
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
 *   12:01 AM  — Asset Tasks (post-local-batch-retirement; downstream=all
 *               so export + Fri/Mon weeklies run at end-of-pipeline. The
 *               validator is NOT fired by this run since 2026-09-08.)
 *   6:00 PM PHT (6 AM EDT / 5 AM EST) — Gmail Package Scraper
 *               (triggerPackageScrape → gmail-scraper package-scrape-daily).
 *               On success the scrape workflow fires pipeline-asset-tasks with
 *               downstream=validator (Asset Tasks SECOND run), which fires only
 *               date-validator-daily at its end. Chain: scraper -> asset tasks
 *               -> date-validator, since 2026-09-08. Skipped Sat/Sun ET by the
 *               scrape workflow, not by this trigger.
 *   02:00 AM  — Open Items Report Data (targeted_asset_tasks + _task_requirements;
 *               run after User Priorities is fresh. Create via own trigger.)
 *   5:00 AM & 5:00 PM — Calendar Events (twice daily; was 6 AM/6 PM, before that 12:30 AM once daily.
 *               SCHEDULE: calendar_am / calendar_pm)
 *   Sat 6:00 PM — Asset Tasks INC weekly full-walk sweep (= Sunday ~6 AM PHT;
 *               heals assign/reschedule drift the hourly shadow walk can't see.
 *               SCHEDULE: asset_tasks_inc_full_walk)
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
  return fireDispatch_('pipeline-orgs');
}

/**
 * Trigger the daily Timer pipeline.
 * Schedule: ~1:15 AM ET daily. Create/refresh the trigger by running
 * SCHEDULE (job 'timer') at the bottom of this file.
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
  return fireDispatch_('pipeline-timer');
}

/**
 * Timer EMAILS run, twice a day on the EASTERN clock (since 2026-10-01):
 *   ~18:30 ET  triggerTimerEmails()        -> window (today, first):  12 AM to 6 PM ET
 *   ~09:00 ET  triggerTimerEmailsSecond()  -> window (yesterday, second): 6 PM to 12 AM ET
 * Both fire the same 'pipeline-timer-emails' dispatch; the Python side picks
 * the last CLOSED window by the clock, so the trigger only has to land after
 * the window closes. Pinned to America/New_York on purpose: the windows are
 * Eastern wall-clock, so a Manila-pinned trigger would fire BEFORE 6 PM ET
 * in winter and the run would pick the wrong window. (2026-09-28 to
 * 2026-09-30 this was one 06:30 Asia/Manila send on a 06:00 PHT shift day;
 * before that ~6:00 AM ET on the ET calendar date.)
 *
 * Minute 30 / minute 0 with +/-15 min jitter keeps both runs after their
 * window close (18:15-18:45, 08:45-09:15). Late stops that reach Swift after
 * a send are picked up in-thread by a later run's --resend.
 *
 * This is run 2 of 2 for timer data: it re-extracts, applies corrections,
 * rebuilds the clean table, then sends the member-facing emails
 * (--remind / --send / --resend). Run 1 (triggerLightPipelines, ~1:15 AM
 * ET / 13:15 PHT) is data + Excel exports only since 2026-08-28.
 *
 * DIFFERENT dispatch type from 'pipeline-timer' on purpose: the two runs must
 * never be fired by the same trigger.
 */
function triggerTimerEmails() {
  return fireDispatch_('pipeline-timer-emails');
}

function triggerTimerEmailsSecond() {
  return fireDispatch_('pipeline-timer-emails');
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
  return fireDispatch_('pipeline-forms');
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
  return fireDispatch_('pipeline-open-items-data');
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
  return fireDispatchWithPayload_('pipeline-priorities', { run_export: true });
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
 * SCHEDULE (calendar_am / calendar_pm).
 */
function triggerCalendarEvents() {
  return fireDispatch_('pipeline-calendar-events');
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
  return fireDispatch_('pipeline-asset-tasks');
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
 * plan's quiet-window slot. SCHEDULE: asset_tasks_inc_full_walk.
 * Run cost: ~2-3h walk + one ~6-9 GB drift audit (dispatched runs always
 * audit); hourly inc runs queue behind it (concurrency group,
 * cancel-in-progress: false), and the audit evidence persists to
 * pipeline.inc_audit_results.
 *
 * strict_audit stays 'false': this is a heal sweep, not the nightly gate.
 */
function triggerAssetTasksIncFullWalk() {
  return fireDispatchWithPayload_('pipeline-asset-tasks-inc', {
    mode: 'full-walk',
    strict_audit: 'false'
  });
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
 * (orphaned-trigger gotcha), so it is safe
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
 * Scheduled by SCHEDULE (holiday_feed_watch).
 */
function triggerHolidayFeedWatch() {
  return fireDispatch_('holiday-feed-watch');
}

/**
 * Daily duplicate-asset watch (duplicate-asset-watch.yml): Swift assets created
 * twice (same project, path and name). Emails the groups created in the last
 * 26 h; Mondays ET the full list. No GHA cron backstop on purpose: the watcher
 * keeps no state, so a second run the same day would send a second email; the
 * Monday full list catches anything a missed day skipped.
 */
function triggerDuplicateAssetWatch() {
  return fireDispatch_('duplicate-asset-watch');
}

/**
 * Daily HR schedule-changes sheet sync (sync-schedule-changes.yml) at 8 PM PHT.
 * Scheduled by SCHEDULE (schedule_changes, 20:00 Asia/Manila). Until
 * 2026-10-26 this needed two ET triggers ("DST twins") and a Manila-hour
 * check; the scheduler's per-job time zone replaced both.
 */
function triggerScheduleChangesSync() {
  return fireDispatch_('sync-schedule-changes');
}

/**
 * Daily HR employee-roster sheet sync (sync-employees.yml -> reference.ref_employees)
 * at 7 PM PHT, one hour BEFORE the schedule-changes sync (which resolves emp_ids
 * against ref_employees). Until 2026-09-16 the roster only synced when someone
 * pressed "Sync to Supabase" in the HR sheet (4 runs ever), so resignations
 * reached the DB weeks late. Scheduled by SCHEDULE (employee_sync, 19:00
 * Asia/Manila). The workflow's cron backstop is 23:00 UTC (7 AM PHT), 12 h later.
 */
function triggerEmployeeSync() {
  return fireDispatch_('sync-employees');
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
      event_type: eventType,
      client_payload: clientPayload || {}
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('Dispatched ' + eventType + ' with payload ' + JSON.stringify(clientPayload) + ' successfully');
    return true;
  }
  Logger.log('ERROR dispatching ' + eventType + ': HTTP ' + code + ' — ' + response.getContentText());
  return false;
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
 * first (orphaned-trigger gotcha), so it is
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

// ============================================================================
// GMAIL PACKAGE SCRAPER (gmail-scraper repo) — moved here 2026-09-08
// ============================================================================
/**
 * Fires repository_dispatch "package-scrape-daily" on the gmail-scraper repo
 * once a day at 6 PM Asia/Manila (= 6 AM EDT / 5 AM EST, same ET day).
 * On success, gmail-scraper's scrape.yml fires local-pipeline's
 * pipeline-asset-tasks with downstream=validator, whose end-of-run dispatch
 * fires date-validator-daily. This is the scraper -> asset tasks ->
 * date-validator chain that lands before the 9 PM PHT shift.
 *
 * Why it lives here: this project deploys as ONE file (whole-file paste).
 * The scraper trigger used to be a separate package_scraper_trigger.gs file
 * (now a retirement stub in gmail-scraper/scripts); a redeploy that pasted
 * only pipeline_trigger.gs would have lost it, exactly as gmail_trigger.gs
 * was lost on 2026-08-07. Until 2026-09-08 the trigger was a manual
 * "Midnight to 1am" ET day timer (~12:41 AM ET).
 *
 * Contract:
 *   - Daily-once guard via Script Property LAST_DISPATCH_DATE (ET date).
 *   - 3 in-process attempts, then an alert email to PACKAGE_SCRAPE_ALERT_EMAIL.
 *   - Uses the same GITHUB_TOKEN script property as every other trigger here;
 *     that PAT must have contents:read+write on BOTH gmail-scraper and
 *     local-pipeline (the scrape workflow's own COP_DISPATCH_PAT secret needs
 *     local-pipeline too, for the chain dispatch).
 *   - Scheduled by SCHEDULE (package_scrape, 18:00 Asia/Manila).
 */

var PACKAGE_SCRAPE_ALERT_EMAIL = 'jamil.mendez@ontel.co';

function triggerPackageScrape() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  // Daily-once guard: skip if we already dispatched today (ET).
  var todayEt = Utilities.formatDate(new Date(), 'America/New_York', 'yyyy-MM-dd');
  var lastDispatched = props.getProperty('LAST_DISPATCH_DATE');
  if (lastDispatched === todayEt) {
    Logger.log('Already dispatched today (' + todayEt + ') — skipping');
    return;
  }

  // One firing per day now (6 PM PHT), so "retry tomorrow" is not a retry.
  // Retry in-process, then page the operator on final failure (the realistic
  // cause is the 90-day PAT expiring).
  var MAX_ATTEMPTS = 3;
  var RETRY_SLEEP_MS = 30000;
  var success = false;
  for (var attempt = 1; attempt <= MAX_ATTEMPTS && !success; attempt++) {
    success = firePackageScrapeDispatch_(token);
    if (!success && attempt < MAX_ATTEMPTS) {
      Logger.log('Dispatch attempt ' + attempt + ' failed; retrying in ' + (RETRY_SLEEP_MS / 1000) + 's');
      Utilities.sleep(RETRY_SLEEP_MS);
    }
  }
  if (success) {
    props.setProperty('LAST_DISPATCH_DATE', todayEt);
    Logger.log('Dispatched package-scrape-daily for ' + todayEt);
  } else {
    Logger.log('Dispatch failed after ' + MAX_ATTEMPTS + ' attempts; alerting operator');
    try {
      MailApp.sendEmail(
        PACKAGE_SCRAPE_ALERT_EMAIL,
        '[gmail-scraper] daily dispatch FAILED ' + todayEt,
        'triggerPackageScrape (pipeline_trigger.gs) could not fire package-scrape-daily after ' +
        MAX_ATTEMPTS + ' attempts.\n\n' +
        'Consequence: no scrape today, so the asset-tasks -> date-validator chain ' +
        'will not run and no validator email goes out.\n\n' +
        'Likely cause: GITHUB_TOKEN (fine-grained PAT, 90-day expiry) expired or lost ' +
        'contents:write on jamilmendez-ontel/gmail-scraper.\n\n' +
        'Recovery: fix the Script Property, then run scrape.yml manually with ' +
        'fire_chain=true (Actions > Gmail Package Scraper > Run workflow).'
      );
    } catch (e) {
      Logger.log('Alert email failed: ' + e);
    }
  }
}

function firePackageScrapeDispatch_(token) {
  var url = 'https://api.github.com/repos/jamilmendez-ontel/gmail-scraper/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: 'package-scrape-daily'
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('repository_dispatch fired successfully');
    return true;
  } else {
    Logger.log('repository_dispatch failed: HTTP ' + code + ' — ' + response.getContentText());
    return false;
  }
}

/**
 * Run this ONCE after creating the PAT to schedule a calendar reminder
 * 5 days before the 90-day expiry.
 */
function schedulePackageScraperTokenReminder() {
  var EXPIRY_DAYS = 90;
  var REMINDER_DAYS_BEFORE = 5;

  var reminderDate = new Date();
  reminderDate.setDate(reminderDate.getDate() + EXPIRY_DAYS - REMINDER_DAYS_BEFORE);

  var event = CalendarApp.getDefaultCalendar().createAllDayEvent(
    'Rotate GitHub PAT — gmail-scraper trigger',
    reminderDate,
    {
      description:
        'The fine-grained GitHub PAT for the gmail-scraper repo expires in 5 days.\n\n' +
        'This PAT is used by package_scraper_trigger.gs (Apps Script).\n\n' +
        'Steps:\n' +
        '1. Go to https://github.com/settings/tokens and generate a new 90-day PAT\n' +
        '   - Repository access: gmail-scraper AND local-pipeline\n' +
        '   - Permissions: Contents → Read and Write\n' +
        '   - Expiration: 90 days\n' +
        '2. Update GITHUB_TOKEN in this Apps Script project (Project Settings > Script Properties)\n' +
        '3. Run schedulePackageScraperTokenReminder() again to set the next reminder'
    }
  );

  event.addEmailReminder(0);
  event.addEmailReminder(24 * 60);

  Logger.log('Rotation reminder created for ' + reminderDate.toDateString());
}

// ============================================================================
// SCHEDULER: one 5-minute trigger fires every daily and weekly job (2026-10-26)
// ============================================================================
/**
 * Why: Apps Script allows 20 triggers per script. One trigger per job (plus
 * two "DST twin" triggers for each Manila-time job) reached 20 on 2026-10-26
 * and setupEmployeeSyncTrigger() failed with "This script has too many
 * triggers". Now ONE runScheduler() trigger runs every 5 minutes and fires
 * the jobs below. Only the high-frequency jobs (priorities, revenue watch,
 * schedule feed audit) keep their own triggers, so a scheduler bug can never
 * stop them too.
 *
 * How a tick decides, per job, on the clock of the job's own time zone:
 *   NOT_TODAY  weekday not in `days`
 *   DONE       already succeeded on this local date
 *   LATER      before `at`
 *   DUE        at or after `at` and less than `graceMin` (default 60) past it
 *   MISSED     more than `graceMin` past `at`: skipped for today, so nothing
 *              fires hours late (timer emails must run soon after their window)
 * A job that returns false (dispatch failed) stays DUE and retries on the next
 * tick inside its grace window. State: Script Property SCHED_LAST_<id> = the
 * local date of the last success. `at` + graceMin must stay before midnight.
 *
 * Time zones are explicit per job, so the Manila jobs no longer need two
 * triggers to follow US daylight saving.
 *
 * To change a time: edit SCHEDULE, paste the whole file. No trigger change.
 * To see what each job will do now: run previewSchedule().
 */
var SCHED_TZ_ET = 'America/New_York';
var SCHED_TZ_PH = 'Asia/Manila';
var SCHED_PROP_PREFIX = 'SCHED_LAST_';
var SCHED_TICK_MINUTES = 5;
var SCHED_DEFAULT_GRACE_MIN = 60;

// days: 1 = Monday ... 7 = Sunday, in the job's time zone. Omit for every day.
var SCHEDULE = [
  { id: 'asset_tasks',               tz: SCHED_TZ_ET, at: '00:01', fn: triggerAssetTasks },
  { id: 'priorities_daily',          tz: SCHED_TZ_ET, at: '00:13', fn: triggerPrioritiesDaily },
  { id: 'forms',                     tz: SCHED_TZ_ET, at: '00:17', fn: triggerForms },
  { id: 'timer',                     tz: SCHED_TZ_ET, at: '01:15', fn: triggerLightPipelines },
  { id: 'open_items_data',           tz: SCHED_TZ_ET, at: '02:00', fn: triggerOpenItemsData },
  { id: 'calendar_am',               tz: SCHED_TZ_ET, at: '05:00', fn: triggerCalendarEvents },
  { id: 'duplicate_asset_watch',     tz: SCHED_TZ_ET, at: '09:00', fn: triggerDuplicateAssetWatch },
  { id: 'timer_emails_second',       tz: SCHED_TZ_ET, at: '09:00', fn: triggerTimerEmailsSecond },
  { id: 'calendar_pm',               tz: SCHED_TZ_ET, at: '17:00', fn: triggerCalendarEvents },
  { id: 'holiday_feed_watch',        tz: SCHED_TZ_ET, at: '18:00', days: [1], fn: triggerHolidayFeedWatch },
  { id: 'asset_tasks_inc_full_walk', tz: SCHED_TZ_ET, at: '18:00', days: [6], fn: triggerAssetTasksIncFullWalk },
  { id: 'timer_emails_first',        tz: SCHED_TZ_ET, at: '18:30', fn: triggerTimerEmails },
  { id: 'orgs',                      tz: SCHED_TZ_ET, at: '22:13', fn: triggerOrgs },
  { id: 'package_scrape',            tz: SCHED_TZ_PH, at: '18:00', fn: triggerPackageScrape },
  { id: 'employee_sync',             tz: SCHED_TZ_PH, at: '19:00', fn: triggerEmployeeSync },
  { id: 'schedule_changes',          tz: SCHED_TZ_PH, at: '20:00', fn: triggerScheduleChangesSync }
];

// Old handlers whose time triggers setupScheduler() deletes (renamed or retired).
var SCHED_LEGACY_HANDLERS = ['triggerAssetTasksGC', 'triggerCalendarLeave'];

/** The 5-minute tick. Created by setupScheduler(); never schedule it by hand. */
function runScheduler() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    Logger.log('runScheduler: previous tick still running; skipping this one.');
    return;
  }
  try {
    var props = PropertiesService.getScriptProperties();
    var now = new Date();
    for (var i = 0; i < SCHEDULE.length; i++) {
      var job = SCHEDULE[i];
      var st = schedJobState_(job, now, props.getProperty(SCHED_PROP_PREFIX + job.id));
      if (st.state !== 'DUE') continue;
      var ok = false;
      try {
        ok = job.fn() !== false; // handlers return false only when the dispatch failed
      } catch (e) {
        Logger.log('runScheduler: ' + job.id + ' threw: ' + e);
      }
      if (ok) {
        props.setProperty(SCHED_PROP_PREFIX + job.id, st.localDate);
        Logger.log('runScheduler: fired ' + job.id + ' (' + job.at + ' ' + job.tz + ', ' + st.localDate + ')');
      } else {
        Logger.log('runScheduler: ' + job.id + ' failed; retrying on the next tick inside its grace window.');
      }
    }
  } finally {
    lock.releaseLock();
  }
}

/** Pure state check for one job at instant `now`. See the block comment above. */
function schedJobState_(job, now, lastDate) {
  var localDate = Utilities.formatDate(now, job.tz, 'yyyy-MM-dd');
  var hm = Utilities.formatDate(now, job.tz, 'HH:mm').split(':');
  var nowMin = parseInt(hm[0], 10) * 60 + parseInt(hm[1], 10);
  var at = job.at.split(':');
  var atMin = parseInt(at[0], 10) * 60 + parseInt(at[1], 10);
  var grace = job.graceMin || SCHED_DEFAULT_GRACE_MIN;
  var dow = parseInt(Utilities.formatDate(now, job.tz, 'u'), 10); // 1 = Monday
  var state;
  if (job.days && job.days.indexOf(dow) === -1) state = 'NOT_TODAY';
  else if (lastDate === localDate) state = 'DONE';
  else if (nowMin < atMin) state = 'LATER';
  else if (nowMin < atMin + grace) state = 'DUE';
  else state = 'MISSED';
  return { state: state, localDate: localDate };
}

function validateSchedule_() {
  var seen = {};
  for (var i = 0; i < SCHEDULE.length; i++) {
    var j = SCHEDULE[i];
    if (seen[j.id]) throw new Error('SCHEDULE: duplicate id ' + j.id);
    seen[j.id] = true;
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(j.at)) throw new Error('SCHEDULE: bad time for ' + j.id + ': ' + j.at);
    if (typeof j.fn !== 'function') throw new Error('SCHEDULE: ' + j.id + ' has no handler function');
    var p = j.at.split(':');
    if (parseInt(p[0], 10) * 60 + parseInt(p[1], 10) + (j.graceMin || SCHED_DEFAULT_GRACE_MIN) > 24 * 60) {
      throw new Error('SCHEDULE: ' + j.id + ' grace window crosses midnight');
    }
  }
}

/**
 * RUN THIS ONCE from the Apps Script editor after pasting this file, between
 * 10 AM and 5 PM ET (no job is due then; it refuses otherwise, so an old
 * trigger and the scheduler can never both fire the same job today).
 * Deletes every time trigger bound to a SCHEDULE handler, the legacy handlers
 * and runScheduler itself, then creates the single 5-minute runScheduler
 * trigger. Safe to re-run.
 */
function setupScheduler() {
  validateSchedule_();
  var now = new Date();
  var props = PropertiesService.getScriptProperties();
  var due = [];
  for (var i = 0; i < SCHEDULE.length; i++) {
    var st = schedJobState_(SCHEDULE[i], now, props.getProperty(SCHED_PROP_PREFIX + SCHEDULE[i].id));
    if (st.state === 'DUE') due.push(SCHEDULE[i].id);
  }
  if (due.length) {
    throw new Error('setupScheduler: these jobs are due right now: ' + due.join(', ') +
                    '. Run it again between 10 AM and 5 PM ET.');
  }

  var handlers = { runScheduler: true };
  for (var k = 0; k < SCHEDULE.length; k++) handlers[SCHEDULE[k].fn.name] = true;
  for (var m = 0; m < SCHED_LEGACY_HANDLERS.length; m++) handlers[SCHED_LEGACY_HANDLERS[m]] = true;

  var existing = ScriptApp.getProjectTriggers();
  var deleted = 0;
  for (var t = 0; t < existing.length; t++) {
    if (handlers[existing[t].getHandlerFunction()]) {
      ScriptApp.deleteTrigger(existing[t]);
      deleted++;
    }
  }

  ScriptApp.newTrigger('runScheduler')
    .timeBased()
    .everyMinutes(SCHED_TICK_MINUTES)
    .create();

  var left = ScriptApp.getProjectTriggers().map(function (x) { return x.getHandlerFunction(); });
  Logger.log('setupScheduler: deleted ' + deleted + ' trigger(s); created runScheduler every ' +
             SCHED_TICK_MINUTES + ' min. Triggers now (' + left.length + '): ' + left.join(', '));
}

/** Read-only: log every job's state right now, and the project's triggers. Fires nothing. */
function previewSchedule() {
  validateSchedule_();
  var now = new Date();
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < SCHEDULE.length; i++) {
    var j = SCHEDULE[i];
    var last = props.getProperty(SCHED_PROP_PREFIX + j.id);
    var st = schedJobState_(j, now, last);
    Logger.log(j.id + ': ' + st.state + ' (at ' + j.at + ' ' + j.tz +
               (j.days ? ', days ' + j.days.join('/') : '') + '; last success ' + (last || 'never') + ')');
  }
  var left = ScriptApp.getProjectTriggers().map(function (x) { return x.getHandlerFunction(); });
  Logger.log('Triggers (' + left.length + ' of 20): ' + left.join(', '));
}
