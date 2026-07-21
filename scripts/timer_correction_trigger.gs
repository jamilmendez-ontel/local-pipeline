/**
 * Timer Correction Apply Trigger — Google Apps Script
 *
 * Fires a repository_dispatch event to GitHub Actions when a tech submits
 * a duration correction via the Timer Correction Google Form. This triggers
 * the apply workflow which reads the correction, stores it, and rebuilds
 * stg_timer_activities_clean with the corrected duration.
 *
 * DEBOUNCE: Multiple form submissions from the same tech (correction OR removal,
 * across either form) are batched into a single dispatch 10 minutes after the
 * first submission in the window. Both the correction and removal scripts
 * coordinate via a shared Google Sheet cell (A1 of COORDINATION_SHEET_ID).
 * Whichever form fires first "owns" the debounce window; the other skips
 * because A1 is a future timestamp.
 *
 * Setup:
 *   1. Open the Timer Correction response spreadsheet
 *   2. Extensions > Apps Script
 *   3. Paste this script
 *   4. Add Script Property: GITHUB_TOKEN = <same PAT as other pipeline triggers>
 *   5. Fill in COORDINATION_SHEET_ID below (see header comment)
 *   6. Set up trigger: Run > Triggers > Add Trigger
 *      - Function: onFormSubmit
 *      - Event source: From spreadsheet
 *      - Event type: On form submit
 *   7. Run onFormSubmit once manually to grant the Drive scope (needed to
 *      open the coordination sheet). Approve the permission prompt.
 */

// Shared with timer_removal_trigger.gs — both scripts must use the same ID.
// Create a new Google Sheet titled "Timer Dispatch Coordination" and paste its ID here.
var COORDINATION_SHEET_ID = '1l1L8YfZZryRLlaGaQKVU2fLGU5PfOYf6vWHb8ejMkc8';

var DEBOUNCE_MINUTES = 10;

// Retry policy for failed dispatches: the debounced batch is NOT lost on a
// single failed GitHub call — the fire function retries every RETRY_MINUTES
// up to MAX_RETRY_ATTEMPTS (B1 of the coordination sheet counts attempts),
// then gives up and leaves the batch to the nightly Pipeline: Timer apply.
var RETRY_MINUTES = 5;
var MAX_RETRY_ATTEMPTS = 6;


function onFormSubmit(e) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  // Log what was submitted
  if (e && e.namedValues) {
    Logger.log('Correction form response received: ' + JSON.stringify(e.namedValues));
  }

  // Check the shared coordination cell: if a dispatch is already scheduled in
  // the future, this submission will be picked up by that run — skip.
  var now = new Date();
  var cell;
  try {
    cell = SpreadsheetApp.openById(COORDINATION_SHEET_ID).getSheets()[0].getRange('A1');
  } catch (err) {
    Logger.log('ERROR: Failed to open coordination sheet: ' + err);
    return;
  }

  var scheduledAt = cell.getValue();
  if (scheduledAt instanceof Date && scheduledAt > now) {
    Logger.log('Dispatch already scheduled for ' + scheduledAt.toString()
             + ' — this submission will be batched.');
    return;
  }

  // Schedule dispatch DEBOUNCE_MINUTES from now
  var fireAt = new Date(now.getTime() + DEBOUNCE_MINUTES * 60 * 1000);
  cell.setValue(fireAt);
  ScriptApp.newTrigger('firePendingCorrectionDispatch')
    .timeBased()
    .at(fireAt)
    .create();
  Logger.log('Dispatch scheduled for ' + fireAt.toString() + ' (' + DEBOUNCE_MINUTES + '-min debounce)');
}


/**
 * Triggered by the scheduled time-based trigger created in onFormSubmit.
 * Deletes its own trigger, fires the repository_dispatch, and clears the
 * shared coordination cell ONLY on success (failures retry, bounded).
 */
function firePendingCorrectionDispatch() {
  // Delete our own scheduled trigger(s) first — there should only be one
  var triggers = ScriptApp.getProjectTriggers();
  triggers.forEach(function (t) {
    if (t.getHandlerFunction() === 'firePendingCorrectionDispatch') {
      ScriptApp.deleteTrigger(t);
    }
  });

  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set');
    return;
  }

  // Fire FIRST; only clear the coordination cell on success. The old order
  // (clear, then fire) would silently lose the whole debounced batch if the
  // single dispatch call failed (non-204 or a thrown fetch) — the responses
  // would sit unprocessed until the nightly apply. Latent fragility found in
  // review 2026-07-21, not an observed incident. On failure, schedule a
  // bounded retry instead.
  var success = false;
  try {
    success = fireCorrectionDispatch(token);
  } catch (err) {
    Logger.log('ERROR: dispatch threw: ' + err);
  }

  var sheet;
  try {
    sheet = SpreadsheetApp.openById(COORDINATION_SHEET_ID).getSheets()[0];
  } catch (err) {
    Logger.log('WARNING: Failed to open coordination sheet: ' + err);
    return;
  }

  if (success) {
    sheet.getRange('A1').clearContent();
    sheet.getRange('B1').clearContent();
    Logger.log('Correction apply workflow triggered successfully (debounced batch)');
    return;
  }

  var attempts = Number(sheet.getRange('B1').getValue()) || 0;
  if (attempts >= MAX_RETRY_ATTEMPTS) {
    Logger.log('ERROR: dispatch failed after ' + attempts + ' retries — giving up; '
             + 'the nightly Pipeline: Timer apply will pick this batch up.');
    sheet.getRange('A1').clearContent();
    sheet.getRange('B1').clearContent();
    return;
  }
  var retryAt = new Date(new Date().getTime() + RETRY_MINUTES * 60 * 1000);
  sheet.getRange('A1').setValue(retryAt);
  sheet.getRange('B1').setValue(attempts + 1);
  ScriptApp.newTrigger('firePendingCorrectionDispatch').timeBased().at(retryAt).create();
  Logger.log('Dispatch failed — retry ' + (attempts + 1) + '/' + MAX_RETRY_ATTEMPTS
           + ' scheduled for ' + retryAt.toString());
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
 * Manual test function — run this to verify the dispatch works (bypasses debounce).
 */
function testDispatch() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set');
    return;
  }
  fireCorrectionDispatch(token);
}
