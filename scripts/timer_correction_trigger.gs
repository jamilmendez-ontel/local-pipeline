/**
 * Timer Correction Apply Trigger — Google Apps Script
 *
 * Batches Timer Correction form submissions into a single repository_dispatch
 * (`timer-correction-apply`) to GitHub Actions. The apply workflow reads the
 * correction, stores it, and rebuilds stg_timer_activities_clean.
 *
 * BATCHING (2026-09-04 rewrite, after the OntelDB crash loop):
 *   Every dispatch runs a full TRUNCATE + reload of the clean table (~60 s,
 *   ~1 GB of WAL). With 15-23 dispatches a day that write load drained the
 *   disk IO budget of the Micro compute tier. This script therefore batches:
 *
 *   - A window opens on the first form submit (either form) and closes
 *     DEBOUNCE_MINUTES after the LAST submit, capped at MAX_WINDOW_MINUTES
 *     after the first. A member working through their list gets ONE run and
 *     ONE confirmation email covering everything they clicked.
 *   - Both the correction and removal scripts share the window through the
 *     "Timer Dispatch Coordination" sheet (COORDINATION_SHEET_ID):
 *       A1 = fire-at time (empty = no window open)
 *       B1 = retry attempt counter
 *       C1 = window start (first submit)
 *       D1 = firing lock (set while a poller is calling GitHub)
 *   - onFormSubmit ONLY writes cells. It never creates triggers, so the two
 *     Apps Script projects (which cannot see each other's triggers) can never
 *     race or double-fire.
 *   - pollPendingDispatch() runs on a 5-minute time trigger and fires the
 *     dispatch once A1 is due. Install it in ONE project only (this one). The
 *     D1 lock makes an accidental second poller harmless.
 *
 *   Expected timing: dispatch 10-15 min after the last click (debounce + poll
 *   granularity), confirmation email ~4 min after that; never more than
 *   ~55 min after the first click.
 *
 * Setup (whole-file paste, see feedback rule "Apps Script: replace whole file"):
 *   1. Open the Timer Correction response spreadsheet
 *   2. Extensions > Apps Script; replace the ENTIRE file with this one
 *   3. Script Property GITHUB_TOKEN = <same PAT as other pipeline triggers>
 *   4. COORDINATION_SHEET_ID below must be IDENTICAL in timer_removal_trigger.gs
 *      (a mismatch is exactly the bug that produced two parallel windows)
 *   5. Triggers:
 *        - onFormSubmit: From spreadsheet, On form submit (keep existing)
 *        - pollPendingDispatch: Time-driven, Minutes timer, Every 5 minutes (NEW)
 *        - DELETE any leftover firePendingCorrectionDispatch trigger
 *   6. Run pollPendingDispatch once manually to grant the Drive scope
 *   7. Test: submit one correction and one removal within a minute; expect
 *      exactly ONE "Timer Correction: Apply" run 10-15 min after the second.
 */

// Shared with timer_removal_trigger.gs — both scripts MUST use the same ID.
var COORDINATION_SHEET_ID = '1l1L8YfZZryRLlaGaQKVU2fLGU5PfOYf6vWHb8ejMkc8';

// Window closes this many minutes after the LAST submit...
var DEBOUNCE_MINUTES = 10;
// ...but never later than this many minutes after the FIRST submit.
var MAX_WINDOW_MINUTES = 45;

// Retry policy for failed dispatches: the batch is NOT lost on a single failed
// GitHub call — the poller retries every RETRY_MINUTES up to MAX_RETRY_ATTEMPTS
// (B1 counts attempts), then gives up and leaves the batch to the nightly
// Pipeline: Timer apply.
var RETRY_MINUTES = 5;
var MAX_RETRY_ATTEMPTS = 6;

// A poller that set D1 more than this many minutes ago is assumed dead.
var FIRING_LOCK_MINUTES = 4;

// Only the project with POLLER_HOST = true actually fires dispatches. This is
// the correction project. The removal project carries the same code with
// POLLER_HOST = false, so an accidental poller install there is a no-op.
var POLLER_HOST = true;

var LOG_LABEL = 'Correction';


function _coordSheet() {
  return SpreadsheetApp.openById(COORDINATION_SHEET_ID).getSheets()[0];
}

function _asDate(v) {
  return (v instanceof Date && !isNaN(v.getTime())) ? v : null;
}

function _minutesFrom(d, minutes) {
  return new Date(d.getTime() + minutes * 60 * 1000);
}


/**
 * On form submit: open or extend the shared batching window. Cell writes only.
 */
function onFormSubmit(e) {
  if (e && e.namedValues) {
    Logger.log(LOG_LABEL + ' form response received: ' + JSON.stringify(e.namedValues));
  }

  var sheet;
  try {
    sheet = _coordSheet();
  } catch (err) {
    Logger.log('ERROR: Failed to open coordination sheet: ' + err);
    return;
  }

  var now = new Date();
  var fireAt = _asDate(sheet.getRange('A1').getValue());
  var windowStart = _asDate(sheet.getRange('C1').getValue());

  if (fireAt && fireAt > now) {
    // Window open: push the close-time out to now + DEBOUNCE, bounded by the cap.
    if (!windowStart) {
      windowStart = now;
      sheet.getRange('C1').setValue(windowStart);
    }
    var cap = _minutesFrom(windowStart, MAX_WINDOW_MINUTES);
    var candidate = _minutesFrom(now, DEBOUNCE_MINUTES);
    var newFireAt = candidate < cap ? candidate : cap;
    if (newFireAt > fireAt) {
      sheet.getRange('A1').setValue(newFireAt);
      Logger.log('Window extended: fires at ' + newFireAt.toString()
               + ' (started ' + windowStart.toString() + ', cap ' + cap.toString() + ')');
    } else {
      Logger.log('Batched into open window firing at ' + fireAt.toString()
               + ' (cap reached or already later)');
    }
    return;
  }

  // No window open (or a stale past value): open a new one.
  var newFire = _minutesFrom(now, DEBOUNCE_MINUTES);
  sheet.getRange('C1').setValue(now);
  sheet.getRange('A1').setValue(newFire);
  sheet.getRange('B1').clearContent();
  Logger.log('Window opened: fires at ' + newFire.toString()
           + ' (' + DEBOUNCE_MINUTES + ' min after last submit, max '
           + MAX_WINDOW_MINUTES + ' min after first)');
}


/**
 * Time-driven poller (every 5 minutes, ONE project only). Fires the dispatch
 * once the shared window is due; retries bounded on failure.
 *
 * Two guards against a double dispatch:
 *   - LockService script lock: two overlapping runs of THIS project (e.g. a
 *     duplicated time trigger after a redeploy) serialize; the second sees
 *     A1 already cleared and does nothing.
 *   - D1 sheet lock: covers a poller in the OTHER project (cross-project
 *     locks do not exist), see below.
 */
function pollPendingDispatch() {
  if (!POLLER_HOST) {
    Logger.log('POLLER_HOST is false in this project — poller is a no-op here');
    return;
  }
  var scriptLock = LockService.getScriptLock();
  if (!scriptLock.tryLock(10000)) {
    Logger.log('Another poller run holds the script lock — skipping');
    return;
  }
  try {
    _pollPendingDispatchLocked();
  } finally {
    scriptLock.releaseLock();
  }
}

function _pollPendingDispatchLocked() {
  var sheet;
  try {
    sheet = _coordSheet();
  } catch (err) {
    Logger.log('ERROR: Failed to open coordination sheet: ' + err);
    return;
  }

  var now = new Date();
  var fireAt = _asDate(sheet.getRange('A1').getValue());
  if (!fireAt) {
    return; // nothing pending
  }
  if (fireAt > now) {
    Logger.log('Window open, fires at ' + fireAt.toString());
    return;
  }

  // Firing lock: a second poller (other project, or overlapping run) skips
  // while a dispatch call is in flight.
  var lock = _asDate(sheet.getRange('D1').getValue());
  if (lock && now < _minutesFrom(lock, FIRING_LOCK_MINUTES)) {
    Logger.log('Another poller is firing (lock set ' + lock.toString() + ') — skipping');
    return;
  }
  sheet.getRange('D1').setValue(now);

  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    sheet.getRange('D1').clearContent();
    return;
  }

  var success = false;
  try {
    success = fireCorrectionDispatch(token);
  } catch (err) {
    Logger.log('ERROR: dispatch threw: ' + err);
  }

  if (success) {
    var startedAt = String(sheet.getRange('C1').getValue());
    sheet.getRange('A1:D1').clearContent();
    Logger.log('Correction apply workflow triggered (batched window from ' + startedAt + ')');
    return;
  }

  var attempts = Number(sheet.getRange('B1').getValue()) || 0;
  if (attempts >= MAX_RETRY_ATTEMPTS) {
    Logger.log('ERROR: dispatch failed after ' + attempts + ' retries — giving up; '
             + 'the nightly Pipeline: Timer apply will pick this batch up.');
    sheet.getRange('A1:D1').clearContent();
    return;
  }
  sheet.getRange('A1').setValue(_minutesFrom(now, RETRY_MINUTES));
  sheet.getRange('B1').setValue(attempts + 1);
  sheet.getRange('D1').clearContent();
  Logger.log('Dispatch failed — retry ' + (attempts + 1) + '/' + MAX_RETRY_ATTEMPTS
           + ' in ' + RETRY_MINUTES + ' min');
}


/**
 * Cutover shim: a one-shot trigger scheduled by the PREVIOUS version of this
 * script may still fire once after the paste. Route it to the poller.
 */
function firePendingCorrectionDispatch() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'firePendingCorrectionDispatch') {
      ScriptApp.deleteTrigger(t);
    }
  });
  pollPendingDispatch();
}


function fireCorrectionDispatch(token) {
  var url = 'https://api.github.com/repos/jamilmendez-ontel/local-pipeline/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: 'timer-correction-apply'
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
 * Manual test function — fires immediately, bypassing the window.
 */
function testDispatch() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set');
    return;
  }
  fireCorrectionDispatch(token);
}
