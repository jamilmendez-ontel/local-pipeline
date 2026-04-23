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
var COORDINATION_SHEET_ID = 'PASTE_SHARED_SHEET_ID_HERE';

var DEBOUNCE_MINUTES = 10;


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
 * Clears the shared coordination cell, deletes its own trigger, and fires
 * the repository_dispatch.
 */
function firePendingCorrectionDispatch() {
  // Clear the shared coordination cell so the next submission can schedule a new window
  try {
    SpreadsheetApp.openById(COORDINATION_SHEET_ID).getSheets()[0].getRange('A1').clearContent();
  } catch (err) {
    Logger.log('WARNING: Failed to clear coordination cell: ' + err);
  }

  // Delete our own scheduled trigger(s) — there should only be one
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

  var success = fireCorrectionDispatch(token);
  if (success) {
    Logger.log('Correction apply workflow triggered successfully (debounced batch)');
  }
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
