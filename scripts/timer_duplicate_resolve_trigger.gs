/**
 * Timer Duplicate Resolve Trigger — Google Apps Script
 *
 * Fires a repository_dispatch event to GitHub Actions when a tech submits
 * a form response for the Timer Duplicate Review system. This triggers
 * the resolve workflow which reads the response, updates the review record,
 * and rebuilds stg_timer_activities_clean.
 *
 * Setup:
 *   1. Open the Timer Duplicate Review response spreadsheet:
 *      https://docs.google.com/spreadsheets/d/1e5W7613Rp41CWpLQnjwqFwSKdJnv9C51kSKeNWDkGT8
 *   2. Extensions > Apps Script
 *   3. Paste this script
 *   4. Add Script Property: GITHUB_TOKEN = <same PAT as other pipeline triggers>
 *      (fine-grained PAT with contents:read+write on local-pipeline)
 *   5. Set up trigger: Run > Triggers > Add Trigger
 *      - Function: onFormSubmit
 *      - Event source: From spreadsheet
 *      - Event type: On form submit
 */

function onFormSubmit(e) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  // Log what was submitted
  if (e && e.namedValues) {
    Logger.log('Form response received: ' + JSON.stringify(e.namedValues));
  }

  var success = fireResolveDispatch(token);
  if (success) {
    Logger.log('Resolve workflow triggered successfully');
  }
}

function fireResolveDispatch(token) {
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
      event_type: 'timer-duplicate-resolve'
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
 * Manual test function — run this to verify the dispatch works.
 */
function testDispatch() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set');
    return;
  }
  fireResolveDispatch(token);
}
